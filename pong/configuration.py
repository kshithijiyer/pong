"""
Configuration is a non-trivial task.  We need to be able to merge configuration variables from
the CLI, from the environment, from a test environment file and from a config file (with precedence
in that order).  In order to make sense of this, we will have a Configuration pipeline.

exporter.cfg(.pylarion) -> ConfigMap -> YAMLConfigurator -> EnvConfigurator -> CLIConfigurator ->
TestEnvironmentConfigurator -> ConfigMap

This is the essence of a transform pipeline.  However, rather than mutate the ConfigMap as it passes
through each Configurator, it will return a new modified ConfigMap.  Note that each Configurator may
only touch some fields in the map.  By having the ConfigMap be immutable, we can look and see what
each Configurator in the pipeline transformed stage by stage.

Fundamentally the pipeline is a composition of functions.  Each of the Configurator classes defined
in this module have objects that act as functions.  The function takes as a single argument a
ConfigMap type, and returns a transformed version of that map.  ConfigMap is a PMap type from 
pyrsistent and is thus immutable.  If we mutated the map as it traversed the pipeline, debugging
any errors will be more difficult.
"""

from argparse import ArgumentParser
from pong.utils import *
from pong.logger import log
import shutil
import os
import sys
import logging

import yaml
from collections import Sequence

from toolz.functoolz import partial, compose
from pyrsistent import PRecord, field
import pyrsistent as pyr
from abc import ABCMeta, abstractmethod
from functools import wraps

try:
    import configparser
except ImportError as e:
    import ConfigParser as configparser


DEFAULT_LOG_LEVEL = logging.DEBUG


def fieldm():
    return field(mandatory=True)


def validate(invariant, fn):
    """
    Returns an invariant function that can be used by a PRecord field

    :param invariant: a string describing the invariant
    :param fn: the function that will test field value (takes field value, returns true or false)
    :return:
    """
    @wraps(fn)
    def inner(x):
        return fn(x), invariant
    return inner


def not_none(x):
    """
    Invariant for the PRecord such that the field value is not None

    :param x:
    :return:
    """
    return x is not None


def non_empty_string(x):
    """
    Invariant for a PREcord such that the field value is not an empty string
    :param x:
    :return:
    """
    result = False
    if isinstance(x, str) or isinstance(x, unicode):
        result = x.strip() != ""
    return result


def is_sequence(x):
    return True if isinstance(x, Sequence) and x else False


def sequence_vals_truthy(s):
    if not isinstance(s, Sequence):
        return False
    for x in s:
        if isinstance(x, str) and x.strip() == "":
            return False
        if not x:
            return False
    else:
        return True


def valid_distro(x):
    """
    Validates that arg is a Distro type, and has
    :param x:
    :return:
    """
    if not isinstance(x, Distro):
        return False

    result = True
    for required in ["arch", "variant"]:
        val = getattr(x, required)
        if not isinstance(val, str):
            result = False
        elif val.strip() == "":
            result = False
    return result


def start_configuration():
    """
    Creates the initial PMap that will be passed down the pipeline

    :return:
    """
    return pyr.m()


class FieldFactory(object):
    def __init__(self, parser=None):
        self.short_names = set()
        self.parser = parser if parser else ArgumentParser()

    def field_factory(self, *args, **kwargs):
        """
        This function does two things:  it forwards args to parser.add_arguments, and other args to field()

        There is one keyword collision for add_argument and field, which is 'type'.  If 'type' exists, we will
        use it for field

        :param long_name: The "--long-arg" name
        :param short_name:
        :param cli_help:
        :param parser:
        :param kwargs:
        :return:
        """
        short_name = None
        if len(args) == 2:
            long_name = args[1]
            short_name = args[0]
        elif len(args) == 1:
            long_name = args[0]

        if short_name is not None and short_name in self.short_names:
            log.warning("{0} already used.  Not setting {0} for {1}".format(short_name, long_name))
        else:
            self.short_names.add(short_name)

        if "short_name" in kwargs:
            kwargs.pop("short_name")

        parser_kwargs = {}
        for x in ["required", "nargs", "choices", "default", "help", "dest"]:
            if x in kwargs:
                parser_kwargs[x] = kwargs.pop(x)
        field_kwargs = kwargs

        if short_name is not None:
            self.parser.add_argument(short_name, long_name, **parser_kwargs)
        else:
            self.parser.add_argument(long_name, **parser_kwargs)

        # print "Option {}:".format(long_name), field_kwargs
        return field(**field_kwargs)


class Configurator(object):
    """
    Base class that all the other derived Configurator types must implement.  Every
    Configurator type is callable and thus implements __call__.  We do this to ensure
    that the object can be composed with other Configurator via toolz.functoolz.compose"""
    
    __metaclass__ = ABCMeta

    def __init__(self):
        self._original_map = None
        self.record = None

    @property
    def original_map(self):
        return self._original_map

    @original_map.setter
    def original_map(self, omap):
        if self._original_map is None:
            self._original_map = omap
        else:
            log.error(u'Can not set original-map')

    @abstractmethod
    def __call__(self, config_map):
        """Takes in a PMap and returns a transformed map"""
        pass

    def __iter__(self):
        for x in dir(self):
            if not x.startswith("_") and not callable(x):
                yield x, getattr(self, x)


class Distro(PRecord):
    arch = field()
    variant = field()
    name = field()
    major = field()
    minor = field()


class ConfigRecord(PRecord):
    """
    This is the master record which will be passed in at the beginning of the pipeline.
    After all Configurator types have been run through the pipeline, an object of this type will be
    returned.  In other words, the Configurators build up immutable PMaps which are merged
    to create a new PMap, and at the end, finalize() is called to generate this map
    """
    distro = field(mandatory=True, type=Distro)
    artifact_archive = field()
    result_path = fieldm()
    project_id = fieldm()
    pylarion_path = fieldm()
    pylarion_user = field()
    pylarion_password = field()
    testrun_template = fieldm()
    testrun_prefix = fieldm()
    testrun_suffix = fieldm()
    testrun_base = field()
    testcases_query = fieldm()
    requirements_query = fieldm()
    environment_file = field()
    exporter_config = field()
    test_case_skips = field()
    requirement_prefix = field()
    testcase_prefix = field()
    testrun_jenkinsjobs = field()
    testrun_notes = field()
    testrun_assignee = field()
    testrun_plannedin = field()
    testrun_group_id = field()

    # These are "functions"
    update_run = field()
    set_project = field()
    get_default_project_id = field()
    generate_only = field()
    get_latest_testrun = field()
    query_testcase = field()


class JenkinsRecord(PRecord):
    """
    Represents the information recorded by an upstream job that will be needed for
    the downstream job to run correctly.  Required for an automation run in jenkins but optional
    """
    distro = field(mandatory=True, type=Distro)
    result_path = fieldm()
    project_id = field()
    testrun_suffix = field()


class JenkinsConfigurator(Configurator):
    fields = ["DISTRO_ARCH", "DISTRO_VARIANT", "RHELX", "RHELY", "BUILD_URL", "COMPOSE_ID"]
    mapper = [("distro_arch", "arch"), ("distro_variant", "variant"), ("compose_id", "name"),
              ("rhelx", "major"), ("rhely", "minor")]

    def __init__(self, test_env_path):
        super(JenkinsConfigurator, self).__init__()
        self.file_path = test_env_path
        if not os.path.exists(self.file_path):
            raise Exception("The test environment file {} doesn't exist".format(self.file_path))

        cfgparser = ConfigParser.ConfigParser()
        cfgparser.read([self.file_path])
        get = partial(cfgparser.get, "test_environment")
        self.dict_args = dict([(k.lower(), get(k)) for k in self.fields if get(k) is not None])

        dict_keys = {"distro": self._make_distro()}
        if self.dict_args["rhelx"] == "6":
            dict_keys["project_id"] = "RHEL6"
        elif self.dict_args["rhelx"] == "7":
            dict_keys["project_id"] = "RedHatEnterpriseLinux7"
        else:
            log.error("Unknown project ID")

        dict_keys["result_path"] = self.dict_args["build_url"]
        dict_keys["testrun_suffix"] = self.dict_args["compose_id"]
        self.jenkins_record = JenkinsRecord(**dict_keys)

    def _make_distro(self):
        """
        Creates a Distro object

        :param cfgparser:
        :return:
        """
        mapped_dict = {}
        for orig, new in self.mapper:
            mapped_dict[new] = self.dict_args[orig]
        return Distro(**mapped_dict)

    def __call__(self, omap):
        self.original_map = omap
        updated = omap.update(self.jenkins_record)
        log.log(DEFAULT_LOG_LEVEL, "=================== {} ====================".format(self.__class__))
        dprint(updated)
        return updated


class OSEnvironmentRecord(PRecord):
    """
    Fields that can be obtained from the OS environment
    """
    distro = field(type=Distro)
    build_url = field()
    result_path = field()
    project_id = field()
    exporter_config = field()
    test_case_skips = field()


class OSEnvironmentConfigurator(Configurator):
    distro_keys = ['DISTRO_ARCH', 'DISTRO_VARIANT', 'DISTRO_MAJOR', 'DISTRO_MINOR']
    valid_keys = ['RESULT_PATH', 'PROJECT_ID', 'EXPORTER_CONFIG', "TEST_CASE_SKIPS"]

    def __call__(self, config_map):
        self.original_map = config_map
        updated = config_map.update(self._make_record())
        log.log(DEFAULT_LOG_LEVEL, "=================== {} ====================".format(self.__class__))
        dprint(updated)
        return updated

    def _make_record(self):
        env_keys, d_keys = self.get_valid_keys()
        if d_keys:
            env_keys["distro"] = Distro(**d_keys)
        rec = OSEnvironmentRecord(**env_keys)
        self.os_env_record = rec
        return self.os_env_record

    def get_valid_keys(self):
        """Returns a dictionary which maps Environment variable names to keys in the ConfigRecord"""
        env_keys = os.environ.keys()
        validk = {k.lower(): os.environ[k] for k in filter(lambda x: x in self.valid_keys, env_keys)}
        validd = {k.replace("DISTRO_", "").lower(): os.environ[k]
                  for k in filter(lambda y: y in self.distro_keys, env_keys)}

        return validk, validd


class CLIConfigRecord(PRecord):
    """
    Creates a class that does 2 things:
    - Creates all the argparse arguments that need to be parsed
    - Creates the PRecord invariants

    When adding new fields, it is usually not the right thing to add a default value here.  The reason being that
    the CLIConfigurator is run last, so the default value will actually override any earlier run Configurators types
    in the pipeline.
    """
    factory = FieldFactory()
    add_field = factory.field_factory
    distro = add_field("-d", "--distro",
                       invariant=validate("distro is a Distro type", valid_distro),
                       type=Distro,
                       help="Reads in the arch, variant, name, major and minor in the following form: "
                            " 'arch:x86_64,variant:Server,name:RedHatEnterpriseLinux-6.8,major:6,minor:8'"
                            " If used, must supply arch and variant")
    artifact_archive = add_field("-a", "--artifact-archive",
                                 default="test-output/testng-results.xml",
                                 help="Used when run from a jenkins job, the jenkins job should use the Post-build"
                                      " Actions -> Archive the artifacts -> Files to archive, and the value"
                                      " entered there for the testng-result.xml should be entered here")
    result_path = add_field("-r", "--result-path", type=str,
                            invariant=lambda x: ((x is not None, "result_path is not None"),
                                                 (x.strip() != "", "result_path is not empty string")),
                            help="Path or URL of testng-results.xml file to parse.  If --environment-file "
                                 "is also specified, this field is overridden")
    project_id = add_field("-p", "--project-id",
                           invariant=validate("project_id not empty", non_empty_string),
                           help="The Polarion project id.  Will override what is in .pylarion file",
                           mandatory=True)
    exporter_config = add_field("-c", "--exporter-config", default=os.path.expanduser("~/exporter.yml"),
                                help="Path to configuration file.  Initial default is ~/exporter.yml.  Will"
                                     "override EXPORTER_CONFIG env var")
    pylarion_path = add_field("-P", "--pylarion-path",
                              mandatory=True,
                              default=os.path.expanduser("~/.pylarion"),
                              help="Path to the .pylarion file (defaults to ~/.pylarion")
    pylarion_user = add_field("-u", "--user",
                              help="The username in Polarion to run test as (overrides .pylarion)",
                              dest="pylarion_user")
    pylarion_password = add_field("--password",
                                  help="The password to use for Polarion (overrides .pylarion)",
                                  dest="pylarion_password")
    testrun_template = add_field("-t", "--testrun-template",
                                 invariant=validate("testrun_template is not empty string", non_empty_string),
                                 help="The Polarion template name that the test run is based off of",
                                 mandatory=True)
    testrun_prefix = add_field("--testrun-prefix",
                               mandatory=True,
                               invariant=validate("testrun_prefix is not empty string", non_empty_string),
                               help="Part of the testrun id.  The testrun id is generated as: "
                                    "'{} {} {} {}'.format(prefix, base, suffix, unique")
    testrun_suffix = add_field("--testrun-suffix",
                               help="See testrun_prefix")
    testrun_base = add_field("--testrun-base",
                             help="See testrun_prefix.  Defaults to the <suite name=> from the testng-results.xml")
    testcases_query = add_field("-b", "--testcases-query",
                                mandatory=True,
                                nargs='*',
                                invariant=lambda x: ((x is not None, "testcases_query is not None"),
                                                     (is_sequence(x), "testcases_query is a sequence"),
                                                     (sequence_vals_truthy(x), "testcases_query values are truthy")),
                                help="A list of space separated strings that will be used for lucene based TestCase "
                                     "title searches For example, if all your test cases have rhel-<6|7>-test "
                                     "in them, then do: '-b rhel-6-test rhel-7-test'.  Another way to do this is like"
                                     " -b 'rhel-*-test'")
    requirements_query = add_field("--requirements-query",
                                   help="A lucene query string to query for existing Requirements in Polarion")
    environment_file = add_field("-e", "--environment-file",
                                 help="Path to an upstream jenkins job generated file.  This file will override"
                                      "the results_path even on the CLI")
    test_case_skips = add_field("-s", "--test-case-skips", default=False,
                                help="When True, if a test-method was skipped, add a TestRecord for it anyway"
                                     " By default this is False.")
    requirement_prefix = add_field("--requirement-prefix", mandatory=True,
                                   help="A string that will be prepended to the autogenerated Requirement title")
    testcase_prefix = add_field("--testcase-prefix",
                                help="A string that will be prepended to the autogenerated TestCase title")
    testrun_jenkinsjobs = add_field("--testrun-jenkinsjobs", default="",
                                     help="A URL for the jenkins job build that will be inserted into the "
                                          "{testrun-property:jenkinsjobs} custom field of a TestRun")
    testrun_notes = add_field("--testrun-notes", default="",
                              help="A string that will be added to the {testrun-property:notes} custom "
                                   "field of a TestRun")
    testrun_assignee = add_field("--testrun-assignee", default="",
                                 help="The user that executes a TestRun. If given, it will override the default from"
                                      "the TestRun Template value for Assignee")
    testrun_plannedin = add_field("--testrun-plannedin", default="",
                                  help="An approprate planned in value from the Polarion Plan (eg RHEL_7_3). If given"
                                       " it will override the value from the TestRun Template for Planned In")
    testrun_group_id = add_field("--testrun-group-id", default="",
                                help="Actually used as a build id (for example the package version)")

    # These are "functions"
    update_run = add_field("--update-run", default=False,
                           help="If given, the arg will be used to find and update an existing "
                                "Polarion TestRun with the testng-results.xml")
    set_project = add_field("--write-to-project", default=False, dest="set_project",
                            help="If project_id, user, or password are given, write to the pylarion_path")
    query_testcase = add_field("--query-testcase", default=False,
                               help="Find a testcase by title, and print out information")
    get_default_project_id = add_field("--get-default-project-id", default=False,
                                       help="Gets the .pylarion project id")
    generate_only = add_field("--generate-only", default=False,
                              help="Only create/update TestCases and Requirements based on the testng-results.xml")
    get_latest_testrun = add_field("--get-latest-testrun", default=False,
                                   help="The supplied arg should be a base string minus the unique identifier"
                                        " of a test run.  For example, if the testrun id is 'exporter testing 1'"
                                        "then the supplied arg will be 'exporter testing'.  A query will be performed"
                                        "to retrieve the title of the most recent run")

    @classmethod
    def parse_args(cls, args=""):
        if args and isinstance(args, str):
            return cls.factory.parser.parse_args(args.split())
        elif args and isinstance(args, type([])):
            return cls.factory.parser.parse_args(args)
        else:
            return cls.factory.parser.parse_args()


class CLIConfigurator(Configurator):
    def __init__(self, parser=ArgumentParser(), args="", jnk_cfg=None):
        super(CLIConfigurator, self).__init__()
        self.jnk_cfg = jnk_cfg
        self.parser = parser
        self.args = CLIConfigRecord.parse_args(args=args)
        self.dict_args = vars(self.args)
        self.reset_project_id = False
        self.pylarion_path = self.get_pylarion_path()
        self.original_project_id = get_default_project(pylarion_path=self.pylarion_path)
        self._project_id = None

    def _make_distro_record(self):
        """
        Parses the self.args.distro argument.
        :return:
        """
        if self.args.distro:
            distro = dict(kv.split(":") for kv in self.args.distro.split(","))
            required = ["arch", "variant"]
            valid = Distro._precord_fields.keys()
            for i in required:
                if i not in distro:
                    raise Exception("Must supply arch and variant if using --distro")
            invalid_keys = [k for k in distro.keys() if k not in valid]
            if invalid_keys:
                raise Exception("{} are invalid keys".format(invalid_keys))

            return Distro(**distro)

    def __call__(self, omap):
        self.original_map = omap
        log.log(DEFAULT_LOG_LEVEL, "------------------- BEFORE: {} -----------------------".format(self.__class__))
        dprint(omap)

        distro_record = self._make_distro_record()
        if self.args.distro:
            self.dict_args["distro"] = distro_record

        # Before we modify the map, let's see if an environment file was passed in
        art_path = "artifact/{}".format(self.args.artifact_archive)
        if self.args.environment_file is not None:
            if "result_path" in omap:
                artifact = omap["result_path"] + art_path
            else:
                artifact = ""
        else:
            artifact = self.dict_args["result_path"]
            if isinstance(artifact, str) and artifact.startswith("http"):
                if art_path not in artifact:
                    artifact += art_path
        self.dict_args["result_path"] = artifact

        # If requirement_prefix or testcase_prefix were not set, give defaults here.  We can't set them in the
        # argparse default=, otherwise, since CLIConfigurator will come last, it will override earlier Configurators
        prefixes = ["requirement_prefix", "testcase_prefix"]
        for p in prefixes:
            if p not in omap and getattr(self.args, p) is None:
                self.dict_args[p] = ""

        # Trim any args from self.dict_args that are None
        final_args = {k: v for k, v in self.dict_args.items() if v is not None}
        updated = omap.update(final_args)
        log.log(DEFAULT_LOG_LEVEL, "=================== {} ====================".format(self.__class__))
        dprint(updated)
        return updated

    # This doesn't belong to this class
    def get_pylarion_path(self, cfg_map=None):
        if cfg_map is None:
            pyl_path = self.args.pylarion_path or os.path.expanduser('~/.pylarion')
        else:
            pyl_path = cfg_map.pylarion_path
        return pyl_path

    # This doesn't belong to this class either
    def save_pylarion(self, cfg_map=None):
        if cfg_map is None:
            if self.args.set_project:
                self.reset_project_id = True

    @staticmethod
    def set_project_id(pylarion_path, project_id, backup=".bak"):
        """
        Writes the project_id to the pylarion file

        :param pylarion_path:
        :param project_id:
        :param backup:
        :return:
        """
        create_backup(pylarion_path, backup=backup)
        cparser = PylarionConfigurator.create_cfg_parser(path=pylarion_path)
        with open(pylarion_path, "w") as newpy:
            cparser.set("webservice", "default_project", project_id)
            cparser.write(newpy)


class YAMLRecord(PRecord):
    """Keys in the YAML config file that we care about"""
    pylarion_path = field()
    user = field()
    password = field()
    result_path = field()
    project_id = field()
    testrun_template = field()
    testrun_prefix = field()
    testrun_suffix = field()
    testrun_base = field()
    distro = field(type=Distro)
    base_queries = field()
    requirements_query = field()
    environment_file = field()
    build_url = field()
    requirement_prefix = field()
    testcase_prefix = field()


class YAMLConfigurator(Configurator):
    def __init__(self, cfg_path=None):
        super(YAMLConfigurator, self).__init__()
        self._set_config_path(cfg_path)

    def _set_config_path(self, cfg_path):
        if cfg_path is None:
            cfg_path = os.path.expanduser("~/exporter.yml")
        if not os.path.exists(cfg_path):
            try:
                self.cfg_path = os.environ["exporter_config"]
            except KeyError:
                self.cfg_path = None
                self.record = YAMLRecord()
        else:
            self.cfg_path = cfg_path

        if self.cfg_path is not None:
            with open(self.cfg_path, "r") as cfg:
                cfg_dict = yaml.load(cfg)

            # Filter out the key-vals where the value is a falsey value
            def trim_falseys(d):
                for k, v in d.items():
                    if isinstance(v, dict):
                        trim_falseys(v)
                    else:
                        if not bool(v):
                            d.pop(k)
            trim_falseys(cfg_dict)

            # If distro in final, create a Distro record
            if "distro" in cfg_dict:
                distro_dict = dict(zip(cfg_dict["distro"].keys(), cfg_dict["distro"].values()))
                # distro_dict = {k: v for k, v in cfg_dict["distro"].items()}
                cfg_dict["distro"] = Distro(**distro_dict)

            # Some of the YAML records are nested dicts. so we need to convert them
            keys = ["testrun_{}".format(k) for k in cfg_dict["testrun"].keys()]
            for k in keys:
                cfg_dict[k] = cfg_dict["testrun"][k.replace("testrun_", "")]
            cfg_dict.pop("testrun")
            self.record = YAMLRecord(**cfg_dict)

    def __call__(self, omap):
        """
        Merges the original map with our yaml config map
        :param omap:
        :return:
        """
        self.original_map = omap
        updated = omap.update(self.record)
        log.log(DEFAULT_LOG_LEVEL, "=================== {} ====================".format(self.__class__))
        dprint(updated)
        return updated


class PylarionRecord(PRecord):
    project_id = fieldm()
    pylarion_user = fieldm()


class PylarionConfigurator(Configurator):
    def __init__(self, path=os.path.expanduser("~/.pylarion")):
        super(PylarionConfigurator, self).__init__()
        self.path = path

        cfg = ConfigParser.ConfigParser()
        cfg.read(self.path)
        get = partial(cfg.get, "webservice")
        # pyl = {k: get(k) for k in ["user", "password", "default_project"]}
        # pyl["project_id"] = pyl.pop("default_project")
        pyl = {"project_id": get("default_project"),
               "pylarion_user": get("user")}
        self.pylarion_record = PylarionRecord(**pyl)

    def __call__(self, omap):
        self.original_map = omap
        updated = omap.update(self.pylarion_record)
        log.log(DEFAULT_LOG_LEVEL, "=================== {} ====================".format(self.__class__))
        dprint(updated)
        return updated

    @staticmethod
    def create_cfg_parser(path=None):
        """
        Creates a config parser to read in the .pylarion file

        :param path:
        :return:
        """
        if path is None:
            path = os.path.expanduser("~/.pylarion")
        cparser = configparser.ConfigParser()
        if not os.path.exists(path):
            raise Exception("{} does not exist".format(path))
        with open(path) as fp:
            cparser.readfp(fp)
        return cparser


def finalize(pipelined_map):
    return ConfigRecord(**pipelined_map)


##############################################################################
# helper functions
##############################################################################

def only_pretty_polarion(obj, field):
    result = False
    try:
        no_under = field.startswith(u'_')
        attrib = getattr(obj, field)
    
        result = (no_under and (attrib and (not callable(attrib))))
    except AttributeError as ae:
        result = False
    except TypeError as te:
        result = False
    return result


def print_kv(obj, field):
    print field, u'=', getattr(obj, field)

    
def query_testcase(query):
    for test in query_test_case(query):
        msg = ((test.work_item_id + u' ') + test.title)
        log.info(msg)

    
def get_default_projectid():
    log.info(get_default_project())

    
def get_latest_testrun(testrun_id):
    tr = get_latest_test_run(testrun_id)
    valid = partial(only_pretty_polarion, tr)
    fields = filter(valid, dir(tr))
    for attr in fields:
        print_kv(tr, attr)


def create_cfg_parser(path=None):
    cpath = (os.path.expanduser(u'~/.pylarion') if (path is None) else path)
    cparser = configparser.ConfigParser()
    if (not os.path.exists(cpath)):
        raise Exception(u'{} does not exist'.format(cpath))
    else:
        with open(cpath) as fp:
            cfg = cparser.readfp(fp)
            return cfg


def create_backup(orig, backup=None):
    """
    Creates a backup copy of original.  If backup is given it must be the full name, otherwise
    if backup is not given, the original file name will be appended with .bak

    :param orig:
    :param backup:
    :return:
    """
    backup_path = backup if backup else (orig + '.bak')
    return shutil.copy(orig, backup_path)


def dprint(m, log_lvl=DEFAULT_LOG_LEVEL):
    for k, v in m.items():
        kv = "{}={}".format(str(k), str(v))
        log.log(log_lvl, kv)


def kickstart(yaml_path=None, args=None):
    """
    Kicks everything off by creating the configuration function pipeline

    :return:
    """
    # Create the CLIConfigurator first, because it may override defaults (eg, it can override the
    # default location of pylarion_path or exporter_config, which are needed by PylarionConfigurator
    # and YAMLConfigurator)

    cli_cfg = CLIConfigurator()
    start_map = pyr.m()
    init_map = cli_cfg(start_map)
    pyl_path = init_map.get("pylarion_path")
    yaml_path = init_map.get("exporter_config")
    env_path = init_map.get("environment_file")

    pyl_cfg = PylarionConfigurator(path=pyl_path)
    env_cfg = OSEnvironmentConfigurator()
    yml_cfg = YAMLConfigurator(cfg_path=yaml_path)
    jnk_cfg = None
    if env_path:
        jnk_cfg = JenkinsConfigurator(env_path)
    cli_cfg = CLIConfigurator(args=args)

    if env_path:
        pipeline = compose(cli_cfg, jnk_cfg, yml_cfg, env_cfg, pyl_cfg)
    else:
        pipeline = compose(cli_cfg, yml_cfg, env_cfg, pyl_cfg)
    end_map = pipeline(start_map)

    log.log(DEFAULT_LOG_LEVEL, "================ end_map ===================")
    dprint(end_map)

    try:
        final = ConfigRecord(**end_map)
    except pyr._checked_types.InvariantException as ex:
        print ex
        if ex.missing_fields:
            log.error("Following fields not configured: " + str(ex.missing_fields))
        if False and  ex.invariant_errors:
            log.error("Invariants broken: " + str(ex.invariant_errors))
        log.error("Please correct the above and run again")
        sys.exit(1)
    log.log(logging.INFO, "================= final ====================")
    dprint(final, log_lvl=logging.INFO)
    log.log(logging.INFO, "============================================\n")

    result = {"pyl_cfg": pyl_cfg,
              "env_cfg": env_cfg,
              "yml_cfg": yml_cfg,
              "cli_cfg": cli_cfg,
              "config": final}
    return result


def cli_print(cfg_map):
    """
    Takes a configuration dict and returns a CLI friendly string

    :param cfg_map:
    :return:
    """
    def cli_ize(name, val):
        fmt = lambda n, f: "--{}='{}'".format(n.replace("_", "-"), f)

        distro = []
        if name == "distro":
            if isinstance(val, str):
                inside = re.compile(r"\((.+)\)").search(val).groups()[0]
                d = {k.strip(): v.replace("'", "") for k, v in map(lambda x: x.split("="), inside.split(","))}

                val = Distro(**d)

            distro.append("arch:{}".format(val.arch))
            distro.append("variant:{}".format(val.variant))
            for x in ["major", "minor", "name"]:
                if x in val:
                    distro.append("{}:{}".format(x, getattr(val, x)))
            return fmt(name, ",".join(distro))
        elif name == "pylarion_user":
            return fmt("user", val)
        elif name == "pylarion_password":
            return fmt("password", val)
        elif name == "testcases_query":
            mapper = partial(map, lambda q: '"{}"'.format(q))
            if isinstance(val, str):
                v = val.replace("['", "")
                v2 = v.replace("']", "")
                all = " ".join(mapper(v2.split()))
            else:
                all = " ".join(mapper(val))
            return "--testcases-query " + all
        else:
            return fmt(name, val)

    return " ".join(cli_ize(k, v) for k, v in cfg_map.items())


def extractor(text):
    """
    Takes a log text of the final, and converts it back into a dict.

    for example:

    1455641905.55-pong.logger-INFO: 	result_path=http://my-jenkins.com/job/some-job/23/artifact/test-output/testng-results.xml
    1455641905.55-pong.logger-INFO: 	query_testcase=False
    1455641905.55-pong.logger-INFO: 	get_default_project_id=False
    1455641905.55-pong.logger-INFO: 	pylarion_path=/home/jenkins/.pylarion
    1455641905.55-pong.logger-INFO: 	exporter_config=/home/jenkins/exporter.yml
    1455641905.55-pong.logger-INFO: 	update_run=False
    1455641905.55-pong.logger-INFO: 	testrun_prefix=RHSM
    1455641905.55-pong.logger-INFO: 	testrun_template=RHSM RHEL6-8
    1455641905.55-pong.logger-INFO: 	get_latest_testrun=False
    1455641905.56-pong.logger-INFO: 	set_project=False
    1455641905.56-pong.logger-INFO: 	testcases_query=['rhsm.*.tests*']
    1455641905.56-pong.logger-INFO: 	project_id=RHEL6
    1455641905.56-pong.logger-INFO: 	generate_only=False
    1455641905.56-pong.logger-INFO: 	testrun_suffix=Server x86_64 Run
    1455641905.56-pong.logger-INFO: 	artifact_archive=test-output/testng-results.xml
    1455641905.56-pong.logger-INFO: 	distro=Distro(major='6', arch='x86_64', variant='Server', name='Red Hat Enterprise Linux', minor='8')
    :param text:
    :return:
    """
    args = {}
    patt = re.compile(r".*INFO:\s+(\w+)=(.*)")
    for line in text.split("\n"):
        m = patt.search(line)
        if m:
            k , v = m.groups()
            args[k] = v
    return args


if __name__ == "__main__":
    result = kickstart()
    from pprint import pprint
    pprint(result)