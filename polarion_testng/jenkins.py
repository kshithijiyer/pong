"""
Source controlled version for the post build step

1. Grab the ${TEST_ENVIRONMENT} variable
2. Parse what we need
"""

import os
import ConfigParser
from functools import partial
from pyrsistent import PRecord, field


class TestEnvironment(PRecord):
    """
    Immutable class type that defines what we need from the test environment file
    """
    distro_arch = field(mandatory=True)
    distro_variant = field(mandatory=True)
    upstream_workspace = field()
    upstream_slave = field()
    rhelx = field()
    rhely = field()
    upstream_job_name = field(mandatory=True)
    upstream_build_number = field(mandatory=True)
    results_path = field(mandatory=True)
    project_id = field(mandatory=True)


def get_test_environment(test_env, artifact_archive=None):
    """
    Grabs the needed environment variables from the ${TEST_ENVIRONMENT} file
    :param artifact_archive:
    :param jenkins_url:
    :param test_env:
    :return:
    """
    # Values to pull from the test_env file
    keys = ["DISTRO_ARCH", "DISTRO_VARIANT", "UPSTREAM_WORKSPACE", "UPSTREAM_SLAVE", "RHELX", "RHELY",
            "UPSTREAM_JOB_NAME", "UPSTREAM_BUILD_NUMBER"]
    cfg = ConfigParser.ConfigParser()
    parsed = cfg.read([os.path.expanduser(test_env)])
    if not parsed:
        raise Exception("Could not find test environment file {}".format(test_env))

    get = partial(cfg.get, "test_environment")
    t_env = {v.lower(): get(v) for v in keys}

    # project_id and results_path will be determined based on the above info
    if "7" in t_env["rhelx"]:
        t_env["project_id"] = "RedHatEnterpriseLinux7"
    elif "6" in t_env["rhelx"]:
        t_env["project_id"] = "RHEL6"
    else:
        raise Exception("Unknown project type")

    # Get the testng-results.xml path
    if artifact_archive is None:
        "test-output/testng-results.xml"
    r_path = "{}/artifact/{}"
    t_env["results_path"] = r_path.format(t_env["build_url"], artifact_archive)
    return TestEnvironment(**t_env)
