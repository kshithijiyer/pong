"""
Takes a pong.parameters file or URL and uses it to pass in parameters.

PROJECT_ID= RHEL6
RESULT_PATH= <Jenkins URL>
ARTIFACT_ARCHIVE= test-output/testng-results.xml
BASE_QUERIES= rhsm.*.tests*
DISTRO= name:Red Hat Enterprise Linux,major:6,minor:8,variant:Server,arch:x86_64
TESTRUN_PREFIX= RHSM
TESTRUN_SUFFIX= Server x86_64
TESTRUN_TEMPLATE= RHSM RHEL-6 8
"""

import os
from functools import partial
from pong.parsing import download_url
from argparse import ArgumentParser
from ConfigParser import ConfigParser
from pong.exporter import Exporter
from pong.configuration import kickstart

parser = ArgumentParser()
parser.add_argument("-u", "--url")
args = parser.parse_args()

pong_params = download_url(args.url)
with open("section", "w") as sectioned:
    with open(pong_params, "r") as pp:
        sectioned.write("[default]\n")
        for line in pp.readlines():
            sectioned.write(line)


cfg = ConfigParser()
cfg.read(["section"])

cfgget = partial(cfg.get, "default")

keys = ["PROJECT_ID", "RESULT_PATH", "ARTIFACT_ARCHIVE", "BASE_QUERIES", "DISTRO",
        "TESTRUN_PREFIX", "TESTRUN_SUFFIX", "TESTRUN_TEMPLATE"]


def converter(name):
    if name == "BASE_QUERIES":
        name = "testcases_query"
    return "--" + name.replace("_", "-").lower()

cmd_args = map(converter, keys)

cmdline_args = zip(cmd_args, map(cfgget, keys))
arglist = []
for opts in cmdline_args:
    arglist.extend(opts)

#  We need to add the new requirements if they don't exist
arglist.extend(["--testcase-prefix", "RHSM-TC : "])
arglist.extend(["--requirement-prefix", "RHSM-REQ : "])
arglist.extend(["--requirements-query", "title:RHSM-REQ AND author.id:ci\-user"])


config_map = kickstart(args=arglist)
Exporter.export(config_map)