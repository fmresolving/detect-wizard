import argparse
import atexit
import glob
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import traceback
import zipfile
from datetime import datetime
from math import trunc

import magic
from blackduck.HubRestApi import HubInstance

from detect_wizard_src.Actionable import Actionable
from detect_wizard_src.Configuration import Configuration, PropertyGroup, Property
from detect_wizard_src.PathTree import PathTree
from detect_wizard_src.file_size_util import b_to_gb, b_to_mb
from detect_wizard_src.TarExaminer import is_tar_docker

# Constants
advisor_version = "1.0-Beta"
detect_version = "6.5.0"
detect_url = "https://detect.synopsys.com/detect8.sh" # Fix change in URL by Synopsys

srcext_list = ['.R', '.actionscript', '.ada', '.adb', '.ads', '.aidl', '.as', '.asm', '.asp', \
               '.aspx', '.awk', '.bas', '.bat', '.bms', '.c', '.c++', '.cbl', '.cc', '.cfc', '.cfm', '.cgi', '.cls', \
               '.cpp', '.cpy', '.cs', '.cxx', '.el', '.erl', '.f', '.f77', '.f90', '.for', '.fpp', '.frm', '.fs', \
               '.g77', '.g90', '.go', '.groovy', '.h', '.hh', '.hpp', '.hrl', '.hxx', '.idl', '.java', '.js', '.jsp', \
               '.jws', '.l', '.lisp', '.lsp', '.lua', '.m', '.m4', '.mm', '.pas', '.php', '.php3', '.php4', '.pl', \
               '.pm', '.py', '.rb', '.rc', '.rexx', '.s', '.scala', '.scm', '.sh', '.sqb', '.sql', '.tcl', '.tk', \
               '.v', '.vb', '.vbs', '.vhd', '.vhdl', '.y']
binext_list = ['.dll', '.obj', '.o', '.a', '.lib', '.iso', '.qcow2', '.vmdk', '.vdi', \
               '.ova', '.nbi', '.vib', '.exe', '.img', '.bin', '.apk', '.aac', '.ipa', '.msi']
arcext_list = ['.zip', '.gz', '.tar', '.xz', '.lz', '.bz2', '.7z', '.rar', '.rar', \
               '.cpio', '.Z', '.lz4', '.lha', '.arj']
jarext_list = ['.jar', '.ear', '.war']
supported_zipext_list = ['.jar', '.ear', '.war', '.zip']
supported_tar_list = ['.tar', '.tar.gz', '.tar.bz']
dockerext_list = ['.tar', '.gz']
pkgext_list = ['.rpm', '.deb', '.dmg']
lic_list = ['LICENSE', 'LICENSE.txt', 'notice.txt', 'license.txt', 'license.html', 'NOTICE', 'NOTICE.txt']


# Sets Sig scan
sig_scan_actionable = Actionable("Signature Scan",
                                 {'sensitivity == 1':
                                      ("detect.tools.excluded: SIGNATURE_SCAN", "Signature Scan is DISABLED")},
                                 default_description="Signature Scan WILL NOT be skipped.")
indiv_file_match_actionable = Actionable("Individual File Match",
                                         {'sensitivity >= 4':
                                              ("detect.blackduck.signature.scanner.individual.file.matching: SOURCE",
                                               "Individual File Matching (SOURCE) is ENABLED")},
                                         default_description="Individual File Matching (SOURCE) is DISABLED")

binary_matching_actionable = Actionable("BDBA Binary Scan",
                                        {'sensitivity >= 4 and num_binaries > 1 and no_write == true':
                                             ("detect.binary.scan.file.path: ${bin_pack_name}",
                                              "${num_binaries} binaries found - but no_write==true..."),
                                         'sensitivity >= 4 and num_binaries > 1 and no_write == false':
                                             ("detect.binary.scan.file.path: ${bin_pack_name}",
                                              "${num_binaries} binaries found - loaded into zip archive - passed to BDBA."),
                                         'sensitivity >= 4 and num_binaries == 1':
                                             ("detect.binary.scan.file.path: ${bin_pack_name}",
                                              "One binary found, BDBA will be invoked.")},
                                        default_description="BDBA will NOT be invoked.")

file_snippet_match_actionable = Actionable("File Snippet Matching",
                                           {'sensitivity == 5 and scan_focus != "s"':
                                               (
                                                   "detect.blackduck.signature.scanner.snippet.matching: SNIPPET_MATCHING",
                                                   "File Snippet Matching set to ENABLED")},
                                           default_description="File Snippet Matching is DISABLED")

directory_dupes_actionable = Actionable("Directory Duplicate Ignore",
                                        {'sensitivity <= 2': (
                                            "bdignores added", "Duplicated directories WILL be ignored.")},
                                        default_description="Duplicated directories WILL NOT be ignored.")

binary_dupes_actionable = Actionable("File Duplicate Ignore",
                                     {'sensitivity <= 2': ("bdignores added", "Duplicated files WILL be ignored.")},
                                     default_description="Duplicated binaries WILL NOT be ignored.")

detector_search_depth_actionable = Actionable("Detector Search Depth",
                                              {'sensitivity == 1': (lambda: det_min_depth if not None else 0,
                                                                    "Detector search depth set to ${OUT}"),
                                               'sensitivity >= 2 and sensitivity <= 4': (
                                                   lambda: det_max_depth // 2 if det_max_depth and det_max_depth // 2 > 0 else 1,
                                                   "Detector search depth set to ${OUT}"),
                                               'sensitivity == 5': (lambda: det_max_depth if det_max_depth else 1,
                                                                    "Detector search depth set to ${OUT}")},
                                              default_description=None)

detector_exclusions_actionable = Actionable("Detector Search Exclusions",
                                            {'sensitivity <= 2': ("${detector_exclusions_func}",
                                                                  "Search exclusion patterns changed to favor small scan.."),
                                             'sensitivity >= 4': ("detect.detector.search.exclusion.defaults: false",
                                                                  "Search exclusion defaults DEACTIVATED.")},
                                            default_description="Search exclusion defaults are used.")

buildless_mode_actionable = Actionable("Buildless Mode", {
    'sensitivity <= 1': ("detect.detector.buildless: true", "Buildless mode WILL be used.")},
                                       default_description="Buildless mode will NOT be used.")

dev_dependencies_actionable = Actionable("Dev Dependencies", {
    'sensitivity > 2': ("detect.${cmd}.include.dev.dependencies: ${dev_dependency_param_pos}", "Dev dependencies WILL be used."),
    'sensitivity <= 2': ("detect.${cmd}.include.dev.dependencies: ${dev_dependency_param_neg}", "Dev dependencies WILL NOT be used.")
})

detect_docker_actionable = Actionable("Detect Docker TAR", {'sensitivity >= 3 and docker_tar_present == true':
                                                                ("detect.docker.tar: '${docker_tar}'",
                                                                 "Docker Layer detection WILL be used on ${docker_tar}.")},
                                      default_description="Docker Layer Detection will NOT be used.")

json_splitter_actionable = Actionable("Scanfile Splitter", {'sensitivity > 1 and scan_size >= 4.5':
                                                                (("blackduck.offline.mode: true",
                                                                  "detect.bom.aggregate.name: detect_advisor_run_{}".format(
                                                                      datetime.now())),
                                                                 "Scan (${scan_size}GB) will be split up to avoid reaching scanfile size limit of (5GB)")},
                                      default_description="Scan (${scan_size}GB) is within size limit (5GB) and will NOT be split.")

license_search_actionable = Actionable("License Search", {'scan_focus != "s"':
                                                              (
                                                                  ("detect.blackduck.signature.scanner.license.search: true",
                                                                   "detect.blackduck.signature.scanner.copyright.search: true"),
                                                               'License search WILL be used.')},
                                       default_description="License search will NOT be used. ")


dev_dependency_pkg_manager_defaults = {'packagist': True,
                               'npm': True,
                               'ruby': False}

ignored_files_and_directories = {'CVS', '.svn', '.hg', '.bzr', '__MACOSX',
                                 '.cvsignore', '.git', '.gitignore', '.gitattributes',
                                 '.gitmodules', '.hgignore', '.hgsub', '.hgsubstate', '.hgtags',
                                 '.bzrignore', 'vssver.scc', '.DS_Store', 'node_modules'}

detectors_file_dict = {
    'build.env': ['bitbake'],
    'cargo.toml': ['cargo'],
    'cargo.lock': ['cargo'],
    'compile_commands.json': ['clang'],
    'Podfile.lock': ['pod'],
    'environment.yml': ['conda'],
    'Makefile.PL': ['cpan'],
    'packrat.lock': ['rtools'],
    'Gopkg.lock': ['go'],
    'gogradle.lock': ['go'],
    'go.mod': ['go'],
    'vendor.json': ['go'],
    'vendor.conf': ['go'],
    'build.gradle': ['gradlew', 'gradle'],
    'build.gradle.kts': ['gradlew', 'gradle'],
    'rebar.config': ['rebar3'],
    'pom.xml': ['mvnw', 'mvn'],
    'pom.groovy': ['mvnw', 'mvn'],
    'node_modules': ['npm'],
    'package.json': ['npm'],
    'package-lock.json': ['npm'],
    'npm-shrinkwrap.json': ['npm'],
    'composer.lock': ['composer'],
    'composer.json': ['composer'],
    'package.xml': ['pear'],
    'pipfile': ['python', 'python3', 'pipenv'],
    'pipfile.lock': ['python', 'python3', 'pipenv'],
    'pyproject.toml': ['poetry'],
    'setup.py': ['python', 'python3', 'pip'],
    'requirements.txt': ['python', 'python3', 'pip'],
    'Gemfile.lock': ['gem'],
    'build.sbt': ['sbt'],
    'Package.swift': ['swift'],
    'yarn.lock': ['yarn'],
    'Makefile': ['clang'],
    'makefile': ['clang'],
    'GNUmakefile': ['clang'],
    'recipe-depends.dot': ['bitbake'],
    'task-depends.dot': ['bitbake']
}

detectors_ext_dict = {
    '.csproj': ['dotnet'],
    '.fsproj': ['dotnet'],
    '.vbproj': ['dotnet'],
    '.asaproj': ['dotnet'],
    '.dcproj': ['dotnet'],
    '.shproj': ['dotnet'],
    '.ccproj': ['dotnet'],
    '.sfproj': ['dotnet'],
    '.njsproj': ['dotnet'],
    '.vcxproj': ['dotnet'],
    '.vcproj': ['dotnet'],
    '.xproj': ['dotnet'],
    '.pyproj': ['dotnet'],
    '.hiveproj': ['dotnet'],
    '.pigproj': ['dotnet'],
    '.jsproj': ['dotnet'],
    '.usqlproj': ['dotnet'],
    '.deployproj': ['dotnet'],
    '.msbuildproj': ['dotnet'],
    '.sqlproj': ['dotnet'],
    '.dbproj': ['dotnet'],
    '.rproj': ['dotnet'],
    '.sln': ['dotnet'],
    '.mk': ['clang']
}

detector_cli_options_dict = {
    'bazel':
        "--detect.bazel.cquery.options='OPTION1,OPTION2'\n" + \
        "   (OPTIONAL List of additional options to pass to the bazel cquery command.)\n" + \
        "--detect.bazel.dependency.type=MAVEN_JAR/MAVEN_INSTALL/UNSPECIFIED\n" + \
        "   (OPTIONAL Bazel workspace external dependency rule: The Bazel workspace rule used to pull in external dependencies.\n" + \
        "    If not set, Detect will attempt to determine the rule from the contents of the WORKSPACE file (default: UNSPECIFIED).)\n",
    'bitbake':
        "--detect.bitbake.package.names='PACKAGE1,PACKAGE2'\n" + \
        "   (OPTIONAL List of package names from which dependencies are extracted.)\n" + \
        "--detect.bitbake.search.depth=X\n" + \
        "   (OPTIONAL The depth at which Detect will search for the recipe-depends.dot or package-depends.dot files (default: 1).)\n" + \
        "--detect.bitbake.source.arguments='ARG1,ARG2,ARG3'\n" + \
        "   (OPTIONAL List of arguments to supply when sourcing the build environment init script)\n",
    'clang':
        "   Note that Detect supports reading a compile_commands.json file generated by cmake.\n" + \
        "   If the project does not use cmake then it is possible to produce the compile_commands.json\n" + \
        "   from standard make using utilities such as https://github.com/rizsotto/Bear. Detect must be\n" + \
        "   run on Linux only for the detection of OSS packages using compile_commands.json, and the packages\n" + \
        "   must be installed in the OS.\n",
    'conda':
        "--detect.conda.environment.name=NAME\n" + \
        "   (OPTIONAL The name of the anaconda environment used by your project)\n",
    'dotnet':
        "--detect.nuget.config.path=PATH\n" + \
        "   (OPTIONAL The path to the Nuget.Config file to supply to the nuget exe)\n" + \
        "--detect.nuget.packages.repo.url=URL\n" + \
        "   (OPTIONAL Nuget Packages Repository URL (default: https://api.nuget.org/v3/index.json).)\n" + \
        "--detect.nuget.excluded.modules=PROJECT\n" + \
        "   (OPTIONAL Nuget Projects Excluded: The names of the projects in a solution to exclude.)\n" + \
        "--detect.nuget.ignore.failure=true\n" + \
        "   (OPTIONAL Ignore Nuget Failures: If true errors will be logged and then ignored.)\n" + \
        "--detect.nuget.included.modules=PROJECT\n" + \
        "   (OPTIONAL Nuget Modules Included: The names of the projects in a solution to include (overrides exclude).)\n",
    'gradle':
        "--detect.gradle.build.command='ARGUMENT1 ARGUMENT2'\n" + \
        "   (OPTIONAL Gradle Build Command: Gradle command line arguments to add to the mvn/mvnw command line.)\n" + \
        "--detect.gradle.excluded.configurations='CONFIG1,CONFIG2'\n" + \
        "   (OPTIONAL Gradle Exclude Configurations: List of Gradle configurations to exclude.)\n" + \
        "--detect.gradle.excluded.projects='PROJECT1,PROJECT2'\n" + \
        "   (OPTIONAL Gradle Exclude Projects: List of Gradle sub-projects to exclude.)\n" + \
        "--detect.gradle.included.configurations='CONFIG1,CONFIG2'\n" + \
        "   (OPTIONAL Gradle Include Configurations: List of Gradle configurations to include.)\n" + \
        "--detect.gradle.included.projects='PROJECT1,PROJECT2'\n" + \
        "   (OPTIONAL Gradle Include Projects: List of Gradle sub-projects to include.)\n",
    'mvn':
        "--detect.maven.build.command='ARGUMENT1 ARGUMENT2'\n" + \
        "   (OPTIONAL Maven Build Command: Maven command line arguments to add to the mvn/mvnw command line.)\n" + \
        "--detect.maven.excluded.scopes='SCOPE1,SCOPE2'\n" + \
        "   (OPTIONAL Dependency Scope Excluded: List of Maven scopes. Output will be limited to dependencies outside these scopes (overrides include).)\n" + \
        "--detect.maven.included.scopes='SCOPE1,SCOPE2'\n" + \
        "   (OPTIONAL Dependency Scope Included: List of Maven scopes. Output will be limited to dependencies within these scopes (overridden by exclude).)\n" + \
        "--detect.maven.excluded.modules='MODULE1,MODULE2'\n" + \
        "   (OPTIONAL Maven Modules Excluded: List of Maven modules (sub-projects) to exclude.)\n" + \
        "--detect.maven.included.modules='MODULE1,MODULE2'\n" + \
        "   (OPTIONAL Maven Modules Included: List of Maven modules (sub-projects) to include.)\n" + \
        "--detect.maven.include.plugins=true\n" + \
        "   (OPTIONAL Maven Include Plugins: Whether or not detect will include the plugins section when parsing a pom.xml.)\n",
    'npm':
        "--detect.npm.arguments='ARG1 ARG2'\n" + \
        "   (OPTIONAL Additional arguments to add to the npm command line when running Detect against an NPM project.)\n" + \
        "--detect.npm.include.dev.dependencies=false\n" + \
        "   (OPTIONAL Include NPM Development Dependencies: Set this value to false if you would like to exclude your dev dependencies.)\n",
    'packagist':
        "--detect.packagist.include.dev.dependencies=false\n" + \
        "   (OPTIONAL Include Packagist Development Dependencies: Set this value to false if you would like to exclude your dev requires dependencies.)\n",
    'pear':
        "--detect.pear.only.required.deps=true\n" + \
        "   (OPTIONAL Include Only Required Pear Dependencies: Set to true if you would like to include only required packages.)\n",
    'python':
        "--detect.pip.only.project.tree=true\n" + \
        "   (OPTIONAL PIP Include Only Project Tree: By default, pipenv includes all dependencies found in the graph. Set to true to only\n" + \
        "   include dependencies found underneath the dependency that matches the provided pip project and version name.)\n" + \
        "--detect.pip.project.name=NAME\n" + \
        "   (OPTIONAL PIP Project Name: The name of your PIP project, to be used if your project's name cannot be correctly inferred from its setup.py file.)\n" + \
        "--detect.pip.project.version.name=VERSION\n" + \
        "   (OPTIONAL PIP Project Version Name: The version of your PIP project, to be used if your project's version name\n" + \
        "   cannot be correctly inferred from its setup.py file.)\n" + \
        "--detect.pip.requirements.path='PATH1,PATH2'\n" + \
        "   (OPTIONAL PIP Requirements Path: List of paths to requirements.txt files.)\n",
    'ruby':
        "--detect.ruby.include.dev.dependencies=true\n" + \
        "   (OPTIONAL Ruby Development Dependencies: If set to true, development dependencies will be included when parsing *.gemspec files.)\n" + \
        "--detect.ruby.include.runtime.dependencies=false\n" + \
        "   (OPTIONAL Ruby Runtime Dependencies: If set to false, runtime dependencies will not be included when parsing *.gemspec files.)\n",
    'sbt':
        "--detect.sbt.report.search.depth\n" + \
        "   (OPTIONAL SBT Report Search Depth: Depth the sbt detector will use to search for report files (default 3))\n" + \
        "--detect.sbt.excluded.configurations='CONFIG'\n" + \
        "   (OPTIONAL SBT Configurations Excluded: The names of the sbt configurations to exclude.)\n" + \
        "--detect.sbt.included.configurations='CONFIG'\n" + \
        "   (OPTIONAL SBT Configurations Included: The names of the sbt configurations to include.)\n",
    'yarn':
        "--detect.yarn.prod.only=true\n" + \
        "   (OPTIONAL Include Yarn Production Dependencies Only: Set this to true to only scan production dependencies.)\n"
}

detector_cli_required_dict = {
    'bazel':
        "--detect.bazel.target='TARGET'\n" + \
        "    (REQUIRED Bazel Target: The Bazel target (for example, //foo:foolib) for which dependencies are collected.)\n",
    'bitbake':
        "--detect.bitbake.build.env.name=NAME\n" + \
        "    (REQUIRED BitBake Init Script Name: The name of the build environment init script (default: oe-init-build-env).)\n"
}

linux_only_detectors = ['clang', 'bitbake']

largesize = 5000000
hugesize = 20000000

notinarc = 0
inarc = 1
inarcunc = 1
inarccomp = 2

#
# Variables
max_arc_depth = 0

counts = {
    'file': [0, 0],
    'dir': [0, 0],
    'ignoredir': [0, 0],
    'arc': [0, 0],
    'bin': [0, 0],
    'jar': [0, 0],
    'detect_wizard_src': [0, 0],
    'det': [0, 0],
    'large': [0, 0],
    'huge': [0, 0],
    'other': [0, 0],
    'lic': [0, 0],
    'pkg': [0, 0]
}

sizes = {
    'file': [0, 0, 0],
    'dir': [0, 0, 0],
    'ignoredir': [0, 0, 0],
    'arc': [0, 0, 0],
    'bin': [0, 0, 0],
    'jar': [0, 0, 0],
    'detect_wizard_src': [0, 0, 0],
    'det': [0, 0, 0],
    'large': [0, 0, 0],
    'huge': [0, 0, 0],
    'other': [0, 0, 0],
    'pkg': [0, 0, 0]
}

# det_min_depth=det_max_depth=None
package_managers_missing = []
use_json_splitter = False

src_list = []
bin_list = []
bin_large_dict = {}
large_list = []
huge_list = []
arc_list = []
jar_list = []
other_list = []
pkg_list = []
docker_list = []

bdignore_list = []

det_dict = {}
detectors_list = []

crc_dict = {}

dup_dir_dict = {}
dup_large_dict = {}

dir_dict = {}
large_dict = {}
arc_files_dict = {}

messages = ""
recs_msgs_dict = {
    'crit': '',
    'imp': '',
    'info': ''
}
cli_msgs_dict = {
    'reqd': '',
    'docker': '',
    'proj': '',
    'scan': '',
    'size': '',
    'dep': '',
    'lic': '',
    'rep': '',
    'sense_log': {}
}

cli_msgs_dict['detect_linux'] = f" bash <(curl -s -L {detect_url})\n"
cli_msgs_dict[
    'detect_linux_proxy'] = " (You may need to configure a proxy to download and run the Detect script as follows)\n" + \
                            " export DETECT_CURL_OPTS='--proxy http://USER:PASSWORD@PROXYHOST:PROXYPORT'\n" + \
                            " bash <(curl -s -L ${DETECT_CURL_OPTS} " + detect_url + ")\n" + \
                            "--blackduck.proxy.host=PROXYHOST\n" + \
                            "--blackduck.proxy.port=PROXYPORT\n" + \
                            "--blackduck.proxy.username=USERNAME\n" + \
                            "--blackduck.proxy.password=PASSWORD\n"
cli_msgs_dict[
    'detect_win'] = " powershell \"[Net.ServicePointManager]::SecurityProtocol = 'tls12'; irm https://detect.synopsys.com/detect.ps1?$(Get-Random) | iex; detect\"\n"
cli_msgs_dict[
    'detect_win_proxy'] = " (You may need to configure a proxy to download and run the Detect script as follows)\n" + \
                          "    ${Env:blackduck.proxy.host} = PROXYHOST\n" + \
                          "    ${Env:blackduck.proxy.port} = PROXYPORT\n" + \
                          "    ${Env:blackduck.proxy.password} = PROXYUSER\n" + \
                          "    ${Env:blackduck.proxy.username} = PROXYPASSWORD\n" + \
                          "    powershell \"[Net.ServicePointManager]::SecurityProtocol = 'tls12'; irm https://detect.synopsys.com/detect.ps1?$(Get-Random) | iex; detect\"\n"
cli_msgs_dict['detect'] = ""
cli_msgs_dict['reqd'] = ""
cli_msgs_dict['proj'] = "--detect.project.name=PROJECT_NAME\n" + \
                        "--detect.project.version.name=VERSION_NAME\n" + \
                        "    (OPTIONAL Specify project and version names)\n" + \
                        "--detect.project.version.update=true\n" + \
                        "    (OPTIONAL Update project and version parameters below for existing projects)\n" + \
                        "--detect.project.tier=X\n" + \
                        "    (OPTIONAL Define project tier numeric for new project)\n" + \
                        "--detect.project.version.phase=ARCHIVED/DEPRECATED/DEVELOPMENT/PLANNING/PRERELEASE/RELEASED\n" + \
                        "    (OPTIONAL Specify project phase for new project - default DEVELOPMENT)\n" + \
                        "--detect.project.version.distribution=EXTERNAL/SAAS/INTERNAL/OPENSOURCE\n" + \
                        "    (OPTIONAL Specify version distribution for new project - default EXTERNAL)\n" + \
                        "--detect.project.user.groups='GROUP1,GROUP2'\n" + \
                        "    (OPTIONAL Define group access for project for new project)\n"

cli_msgs_dict['rep'] = "--detect.wait.for.results=true\n" + \
                       "    (OPTIONAL Wait for server-side analysis to complete - useful for script execution after scan)\n" + \
                       "--detect.cleanup=false\n" + \
                       "    (OPTIONAL Retain scan results in $HOME/blackduck folder)\n" + \
                       "--detect.policy.check.fail.on.severities='ALL,NONE,UNSPECIFIED,TRIVIAL,MINOR,MAJOR,CRITICAL,BLOCKER'\n" + \
                       "    (OPTIONAL Comma-separated list of policy violation severities that will cause Detect to return fail code\n" + \
                       "--detect.notices.report=true\n" + \
                       "    (OPTIONAL Generate Notices Report in text form in project directory)\n" + \
                       "--detect.notices.report.path=NOTICES_PATH\n" + \
                       "    (OPTIONAL The output directory for notices report. Default is the project directory)\n" + \
                       "--detect.risk.report.pdf=true\n" + \
                       "    (OPTIONAL Black Duck risk report in PDF form will be created in project directory)\n" + \
                       "--detect.risk.report.pdf.path=PDF_PATH\n" + \
                       "    (OPTIONAL Output directory for risk report in PDF. Default is the project directory.\n" +\
                       "--detect.report.timeout=XXX\n" + \
                       "    (OPTIONAL Amount of time in seconds Detect will wait for scans to finish and to generate reports (default 300).\n" + \
                       "    300 seconds may be sufficient, but very large scans can take up to 20 minutes (1200 seconds) or longer)\n"

parser = argparse.ArgumentParser(
    description='Check prerequisites for Detect, scan folders, configure and run Synopsys Detect', prog='detect_wizard')
parser.add_argument("scanfolder", nargs="?", help="Project folder to analyse", default="")
parser.add_argument("-b", "--bdignore", help="Create .bdignore files in sub-folders to exclude folders from scan",
                    action='store_true')
parser.add_argument("-i", "--interactive", help="Use interactive mode to review/set options", action='store_true')
parser.add_argument("-s", "--sensitivity",
                    help="Coverage/sensitivity - 1 = dependency scan only & limited FPs, 5 = all scan types including all potential matches")
parser.add_argument("-f", "--focus", help="Scan focus of License Compliance (l) / Security (s) / Both (b)")
parser.add_argument("-u", "--url", help="Black Duck Server URL")
parser.add_argument("-a", "--api_token", help="Black Duck Server API Token")
parser.add_argument("-n", "--no_scan", help="Do not run Detect scan - only create .yml project config file",
                    action='store_true')
#parser.add_argument('--no_write', help="Do not add files to scan directory.", action='store_true')
#parser.add_argument('--aux_write_dir', help="Directory to write intermediate files (default XXXX)")
parser.add_argument('-hp', '--hub_project', help="Hub Project Name")
parser.add_argument('-hv', '--hub_version', help="Hub Project Version")
parser.add_argument('-t', '--trust_cert', help="Automatically trust Black Duck cert")
parser.add_argument('-bdba', '--binary', help="Enable BDBA integration in detect scan (If license is available).",
                    action='store_true')
args = parser.parse_args()


def process_tar_entry(tinfo: tarfile.TarInfo, tarpath, dirdepth, tar):
    fullpath = tarpath + "##" + tinfo.name
    odir = tinfo.name
    dir = os.path.dirname(tinfo.name)
    depthinzip = 0
    while dir != odir:
        depthinzip += 1
        odir = dir
        dir = os.path.dirname(dir)

    dirdepth = dirdepth + depthinzip
    tdir = tarpath + "##" + os.path.dirname(tinfo.name)
    if tdir not in dir_dict.keys():
        counts['dir'][inarc] += 1
        dir_dict[tdir] = {}
        dir_dict[tdir]['num_entries'] = 1
        dir_dict[tdir]['size'] = tinfo.size
        dir_dict[tdir]['depth'] = dirdepth
        dir_dict[tdir]['filenamesstring'] = tinfo.name + ";"
    else:
        dir_dict[tdir]['num_entries'] += 1
        dir_dict[tdir]['size'] += tinfo.size
        dir_dict[tdir]['depth'] = dirdepth
        dir_dict[tdir]['filenamesstring'] += tinfo.name + ";"
    arc_files_dict[fullpath] = get_crc_file(tar.extractfile(tinfo.name))
    # todo the two sizes won't work so well like that
    checkfile(tinfo.name, fullpath, tinfo.size, tinfo.size, dirdepth, True,
              filebuff=tar.extractfile(tinfo.name).read())
    return dirdepth


def process_tar(tarpath, tardepth, dirdepth):
    global max_arc_depth
    global messages

    tardepth += 1
    if tardepth > max_arc_depth:
        max_arc_depth = tardepth

    # print("ZIP:{}:{}".format(zipdepth, zippath))
    try:
        with tarfile.TarFile(tarpath) as t:
            for tinfo in t.getmembers():
                print("CHECKING FILE OUTER: {}".format(tinfo))
                fullpath = tarpath + "##" + tinfo.name
                if tinfo.isdir() and 'tar' not in tinfo.name:
                    continue
                process_tar_entry(tinfo, tarpath, dirdepth, t)
                if os.path.splitext(tinfo.name)[1] in supported_tar_list or tinfo.isdir():
                    with t.extractfile(tinfo.name) as t2:
                        process_nested_tar(t2, fullpath, tardepth, dirdepth)
    except:
        messages += "WARNING: Can't open tar {} (Skipped)\n".format(tarpath)


def process_nested_tar(t, tarpath, tardepth, dirdepth):
    global max_arc_depth
    global messages
    tardepth += 1
    if tardepth > max_arc_depth:
        max_arc_depth = tardepth
    try:
        data = io.BytesIO(t.read())
        with tarfile.TarFile(fileobj=data) as nt:
            print(nt.getmembers())
            for tinfo in nt.getmembers():
                print("CHECKING FILE INNER: {}".format(tinfo.name))
                dirdepth = process_tar_entry(tinfo, tarpath, dirdepth, nt)
                if os.path.splitext(tinfo.name)[1] in supported_tar_list:
                    with nt.extractfile(tinfo.name) as t2:
                        process_nested_tar(t2, tarpath + "##" + tinfo.name, tardepth, dirdepth)
    except:
        messages += "WARNING: Can't open nested tar {} (Skipped)\n".format(tarpath)


def process_nested_zip(z, zippath, zipdepth, dirdepth):
    global max_arc_depth
    global messages

    zipdepth += 1
    if zipdepth > max_arc_depth:
        max_arc_depth = zipdepth

    # print("ZIP:{}:{}".format(zipdepth, zippath))
    z2_filedata = io.BytesIO(z.read())
    try:
        with zipfile.ZipFile(z2_filedata) as nz:
            for zinfo in nz.infolist():
                dirdepth = process_zip_entry(zinfo, zippath, dirdepth, nz)
                if os.path.splitext(zinfo.filename)[1] in supported_zipext_list:
                    with nz.open(zinfo.filename) as z2:
                        process_nested_zip(z2, zippath + "##" + zinfo.filename, zipdepth, dirdepth)
    except:
        messages += "WARNING: Can't open nested zip {} (Skipped)\n".format(zippath)


def process_zip_entry(zinfo, zippath, dirdepth, z):
    # print("ENTRY:" + zippath + "##" + zinfo.filename)
    fullpath = zippath + "##" + zinfo.filename
    odir = zinfo.filename
    dir = os.path.dirname(zinfo.filename)
    depthinzip = 0
    while dir != odir:
        depthinzip += 1
        odir = dir
        dir = os.path.dirname(dir)

    dirdepth = dirdepth + depthinzip
    tdir = zippath + "##" + os.path.dirname(zinfo.filename)
    if tdir not in dir_dict.keys():
        counts['dir'][inarc] += 1
        dir_dict[tdir] = {}
        dir_dict[tdir]['num_entries'] = 1
        dir_dict[tdir]['size'] = zinfo.file_size
        dir_dict[tdir]['depth'] = dirdepth
        dir_dict[tdir]['filenamesstring'] = zinfo.filename + ";"
    else:
        dir_dict[tdir]['num_entries'] += 1
        dir_dict[tdir]['size'] += zinfo.file_size
        dir_dict[tdir]['depth'] = dirdepth
        dir_dict[tdir]['filenamesstring'] += zinfo.filename + ";"

    arc_files_dict[fullpath] = zinfo.CRC
    checkfile(zinfo.filename, fullpath, zinfo.file_size, zinfo.compress_size, dirdepth, True,
              filebuff=z.open(zinfo.filename, 'rb').read())
    return dirdepth


def process_zip(zippath, zipdepth, dirdepth):
    global max_arc_depth
    global messages

    zipdepth += 1
    if zipdepth > max_arc_depth:
        max_arc_depth = zipdepth

    # print("ZIP:{}:{}".format(zipdepth, zippath))
    try:
        with zipfile.ZipFile(zippath) as z:
            for zinfo in z.infolist():
                if zinfo.is_dir():
                    continue
                fullpath = zippath + "##" + zinfo.filename
                process_zip_entry(zinfo, zippath, dirdepth)
                if os.path.splitext(zinfo.filename)[1] in supported_zipext_list:
                    with z.open(zinfo.filename) as z2:
                        process_nested_zip(z2, fullpath, zipdepth, dirdepth)
    except:
        messages += "WARNING: Can't open zip {} (Skipped)\n".format(zippath)


def checkfile(name, path, size, size_comp, dirdepth, in_archive, filebuff=None):

    ext = os.path.splitext(name)[1]
    if filebuff is not None:
        magic_result = magic.from_buffer(filebuff, mime=True)
    else:
        magic_result = magic.from_file(path, mime=True)

    if ext != ".zip":
        if not in_archive:
            counts['file'][notinarc] += 1
            sizes['file'][notinarc] += size
        else:
            counts['file'][inarc] += 1
            sizes['file'][inarcunc] += size
            sizes['file'][inarccomp] += size_comp
        if size > hugesize:
            huge_list.append(path)
            large_dict[path] = size
            if not in_archive:
                counts['huge'][notinarc] += 1
                sizes['huge'][notinarc] += size
            else:
                counts['huge'][inarc] += 1
                sizes['huge'][inarcunc] += size
                sizes['huge'][inarccomp] += size_comp
        elif size > largesize:
            large_list.append(path)
            large_dict[path] = size
            if not in_archive:
                counts['large'][notinarc] += 1
                sizes['large'][notinarc] += size
            else:
                counts['large'][inarc] += 1
                sizes['large'][inarcunc] += size
                sizes['large'][inarccomp] += size_comp

    if name in detectors_file_dict.keys() and path.find("node_modules") < 0:
        if not in_archive:
            det_dict[path] = dirdepth
        ftype = 'det'
    elif os.path.basename(name) in lic_list:
        other_list.append(path)
        ftype = 'other'
        counts['lic'][notinarc] += 1

    if ext in detectors_ext_dict.keys():
        if not in_archive:
            det_dict[path] = dirdepth
        ftype = 'det'
    elif ext in srcext_list:
        src_list.append(path)
        ftype = 'detect_wizard_src'
    elif ext in jarext_list:
        jar_list.append(path)
        ftype = 'jar'
    elif ext in binext_list or magic_result in ['application/x-mach-binary',
                                                'application/x-dosexec',
                                                'application/x-executable']:
        bin_list.append(path)
        if size > largesize:
            bin_large_dict[path] = size
        ftype = 'bin'
    elif ext in arcext_list:
        if ext in dockerext_list:
            if is_tar_docker(path):
                # we will invoke --detect.docker.tar on these
                print("Found Docker layer tar at: {}".format(path))
                # TODO - keep a list instead of overwriting with every new one.
                retval = detect_docker_actionable.test(sensitivity=args.sensitivity, docker_tar_present=True,
                                                       docker_tar=os.path.abspath(path))
                if retval.outcome != "NO-OP":
                    c.str_add('docker', retval.outcome)
                    cli_msgs_dict['docker'] += retval.outcome + "\n"
        arc_list.append(path)
        ftype = 'arc'
    elif ext in pkgext_list:
        pkg_list.append(path)
        ftype = 'pkg'
    else:
        other_list.append(path)
        ftype = 'other'

    if not in_archive:
        counts[ftype][notinarc] += 1
        sizes[ftype][notinarc] += size
    else:
        counts[ftype][inarc] += 1
        sizes[ftype][inarcunc] += size
        if size_comp == 0:
            sizes[ftype][inarccomp] += size
        else:
            sizes[ftype][inarccomp] += size_comp
    return (ftype)


def process_dir(path, dirdepth, ignore):
    dir_size = 0
    dir_entries = 0
    filenames_string = ""
    global messages

    dir_dict[path] = {}
    dirdepth += 1

    all_bin = False
    try:
        ignore_list = []
        if not ignore:
            # Check whether .bdignore exists
            bdignore_file = os.path.join(path, ".bdignore")
            if os.path.exists(bdignore_file):
                b = open(bdignore_file, "r")
                lines = b.readlines()
                for bline in lines:
                    ignore_list.append(bline[1:len(bline) - 2])
                b.close()

        for entry in os.scandir(path):
            ignorethis = False
            if entry.name in ignored_files_and_directories:
                ignorethis = True
            dir_entries += 1
            filenames_string += entry.name + ";"
            if entry.is_dir(follow_symlinks=False):
                if ignore or os.path.basename(entry.path) in ignore_list or ignorethis:
                    ignorethis = True
                    counts['ignoredir'][notinarc] += 1
                else:
                    counts['dir'][notinarc] += 1
                this_size = process_dir(entry.path, dirdepth, ignorethis)
                dir_size += this_size
                if ignorethis:
                    sizes['ignoredir'][notinarc] += this_size
            else:
                if not ignore or ignorethis:
                    ftype = checkfile(entry.name, entry.path, entry.stat(follow_symlinks=False).st_size, 0, dirdepth,
                                      False)
                    if ftype == 'bin':
                        if dir_entries == 1:
                            all_bin = True
                    else:
                        all_bin = False
                    ext = os.path.splitext(entry.name)[1]
                    if ext in supported_zipext_list:
                        process_zip(entry.path, 0, dirdepth)
                    #if ext in supported_tar_list:
                    #    process_tar(entry.path, 0, dirdepth)

                dir_size += entry.stat(follow_symlinks=False).st_size

    except OSError:
        messages += "ERROR: Unable to open folder {}\n".format(path)
        return 0

    if not ignore:
        dir_dict[path]['num_entries'] = dir_entries
        dir_dict[path]['size'] = dir_size
        dir_dict[path]['depth'] = dirdepth
        dir_dict[path]['filenamesstring'] = filenames_string
    if all_bin and path.find("##") < 0:
        bdignore_list.append(path)
    return dir_size


def process_largefiledups(f):
    import filecmp

    if f:
        f.write("\nLARGE DUPLICATE FILES:\n")

    count = 0
    fcount = 0
    total_dup_size = 0
    count_dups = 0
    fitems = len(large_dict)
    for apath, asize in large_dict.items():
        fcount += 1
        if fcount % ((fitems // 6) + 1) == 0:
            print(".", end="", flush=True)
        dup = False
        for cpath, csize in large_dict.items():
            if apath == cpath:
                continue
            if asize == csize:
                aext = os.path.splitext(apath)[1]
                cext = os.path.splitext(cpath)[1]
                if aext == cext:
                    dup = True
                elif aext == "" and cext == "":
                    dup = True
                if dup and asize < 1000000000:
                    if apath.find("##") > 0 or cpath.find("##") > 0:
                        if apath.find("##") > 0:
                            acrc = arc_files_dict[apath]
                        else:
                            acrc = get_crc(apath)
                        if cpath.find("##") > 0:
                            ccrc = arc_files_dict[cpath]
                        else:
                            ccrc = get_crc(cpath)
                        test = (acrc == ccrc)
                    else:
                        test = filecmp.cmp(apath, cpath, True)

                    if test and dup_large_dict.get(cpath) == None and \
                            dup_dir_dict.get(os.path.dirname(apath)) == None and \
                            dup_dir_dict.get(os.path.dirname(cpath)) == None:
                        dup_large_dict[apath] = cpath
                        total_dup_size += asize
                        count_dups += 1
                        if f:
                            f.write("- Large Duplicate file - {}, {} (size {}MB)\n".format(apath, cpath,
                                                                                           trunc(asize / 1000000)))
                            count += 1

    if f and count == 0:
        f.write("    None\n")

    return (count_dups, total_dup_size)


def process_dirdups(f):
    count_dupdirs = 0
    size_dupdirs = 0
    dcount = 0

    tmp_dup_dir_dict = {}

    if f:
        f.write("\nLARGE DUPLICATE FOLDERS:\n")

    count = 0
    ditems = len(dir_dict)
    for apath, adict in dir_dict.items():
        dcount += 1
        if dcount % ((ditems // 6) + 1) == 0:
            print(".", end="", flush=True)
        try:
            if adict['num_entries'] == 0 or adict['size'] < hugesize:
                continue
        except:
            continue
        dupmatch = False
        for cpath, cdict in dir_dict.items():
            if apath != cpath:
                try:
                    if adict['num_entries'] == cdict['num_entries'] and adict['size'] == cdict['size'] \
                            and adict['filenamesstring'] == cdict['filenamesstring']:
                        if adict['depth'] < cdict['depth']:
                            keypath = apath
                            valpath = cpath
                        elif len(apath) < len(cpath):
                            keypath = apath
                            valpath = cpath
                        elif apath < cpath:
                            keypath = apath
                            valpath = cpath
                        else:
                            keypath = cpath
                            valpath = apath

                        newdup = False
                        if keypath not in tmp_dup_dir_dict.keys():
                            newdup = True
                        elif tmp_dup_dir_dict[keypath] != valpath:
                            newdup = True
                        if newdup:
                            tmp_dup_dir_dict[keypath] = valpath
                        break
                except:
                    pass

    # Now remove dupdirs with matching parent folders
    for xpath in tmp_dup_dir_dict.keys():
        ypath = tmp_dup_dir_dict[xpath]
        # print("Processing folder:" + xpath + " dup " + ypath)
        xdir = os.path.dirname(xpath)
        ydir = os.path.dirname(ypath)
        if xdir in tmp_dup_dir_dict.keys() and tmp_dup_dir_dict[xdir] == ydir:
            # parents match - ignore
            # print("Ignorning dup dir: " + xpath + " " + ypath)
            pass
        else:
            # Create dupdir entry
            # print("Adding dup dir: " + xpath + " " + ypath)
            dup_dir_dict[xpath] = ypath
            count_dupdirs += 1
            size_dupdirs += dir_dict[xpath]['size']
            if f and dir_dict[xpath]['size'] > hugesize:
                f.write("- Large Duplicate folder - {}, {} (size {}MB)\n".format(xpath, ypath, \
                                                                                 trunc(dir_dict[xpath][
                                                                                           'size'] / 1000000)))
                count += 1

    if f and count == 0:
        f.write("    None\n")

    return (count_dupdirs, size_dupdirs)


def check_singlefiles(f):
    # Check for singleton js & other single files
    sfmatch = False
    sf_list = []
    for thisfile in src_list:
        ext = os.path.splitext(thisfile)[1]
        if ext == '.js':
            # get dir
            # check for .js in filenamesstring
            thisdir = dir_dict.get(os.path.dirname(thisfile))
            if thisfile.find("node_modules") > 0:
                continue
            if thisdir != None:
                all_js = True
                for filename in thisdir['filenamesstring'].split(';'):
                    srcext = os.path.splitext(filename)[1]
                    if srcext != '.js':
                        all_js = False
                if not all_js:
                    sfmatch = True
                    sf_list.append(thisfile)
    if sfmatch:
        c.str_add('scan', '--detect.blackduck.signature.scanner.individual.file.matching=SOURCE')
        c.str_add('scan', "    (To include singleton .js files in signature scan for OSS matches)")
        if cli_msgs_dict['scan'].find("individual.file.matching") < 0:
            cli_msgs_dict['scan'] += "--detect.blackduck.signature.scanner.individual.file.matching=SOURCE\n" + \
                                     "    (To include singleton .js files in signature scan for OSS matches)\n"


def get_crc(myfile):
    import zlib
    buffersize = 65536

    crcvalue = 0
    try:
        with open(myfile, 'rb') as afile:
            buffr = afile.read(buffersize)
            while len(buffr) > 0:
                crcvalue = zlib.crc32(buffr, crcvalue)
                buffr = afile.read(buffersize)
    except:
        messages += "WARNING: Unable to open file {} to calculate CRC\n".format(myfile)
        return (0)
    return (crcvalue)


def get_crc_file(afile):
    import zlib
    buffersize = 65536

    crcvalue = 0
    try:

        buffr = afile.read(buffersize)
        while len(buffr) > 0:
            crcvalue = zlib.crc32(buffr, crcvalue)
            buffr = afile.read(buffersize)
    except:
        messages += "WARNING: Unable to open file to calculate CRC\n"
        return (0)
    return (crcvalue)


def print_summary(critical_only, f):
    global rep

    summary = "+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n\n" + \
              "SUMMARY INFO:\nTotal Scan Size = {:,d} MB\n\n".format(
                  trunc(b_to_mb(sizes['file'][notinarc] + sizes['arc'][notinarc]))) + \
              "                         Num Outside     Size Outside      Num Inside     Size Inside     Size Inside\n" + \
              "                            Archives         Archives        Archives        Archives        Archives\n" + \
              "                                                                        (UNcompressed)    (compressed)\n" + \
              "====================  ==============   ==============   =============   =============   =============\n"

    row = "{:25} {:>10,d}    {:>10,d} MB      {:>10,d}   {:>10,d} MB   {:>10,d} MB\n"

    summary += row.format("Files (exc. Archives)", \
                          counts['file'][notinarc], \
                          trunc(b_to_mb(sizes['file'][notinarc])), \
                          counts['file'][inarc], \
                          trunc(b_to_mb(sizes['file'][inarcunc])), \
                          trunc(b_to_mb(sizes['file'][inarccomp])))

    summary += row.format("Archives (exc. Jars)", \
                          counts['arc'][notinarc], \
                          trunc(b_to_mb(sizes['arc'][notinarc])), \
                          counts['arc'][inarc], \
                          trunc(b_to_mb(sizes['arc'][inarcunc])), \
                          trunc(b_to_mb(sizes['arc'][inarccomp])))

    summary += "====================  ==============   ==============   =============   =============   =============\n"

    summary += row.format("ALL FILES (Scan size)", counts['file'][notinarc] + counts['arc'][notinarc], \
                          trunc(b_to_mb(sizes['file'][notinarc] + sizes['arc'][notinarc])), \
                          counts['file'][inarc] + counts['arc'][inarc], \
                          trunc(b_to_mb(sizes['file'][inarcunc] + sizes['arc'][inarcunc])), \
                          trunc(b_to_mb(sizes['file'][inarccomp] + sizes['arc'][inarccomp])))

    summary += "====================  ==============   ==============   =============   =============   =============\n"

    summary += "{:25} {:>10,d}              N/A      {:>10,d}             N/A             N/A   \n".format("Folders", \
                                                                                                           counts[
                                                                                                               'dir'][
                                                                                                               notinarc], \
                                                                                                           counts[
                                                                                                               'dir'][
                                                                                                               inarc])

    summary += row.format("Ignored Folders", \
                          counts['ignoredir'][notinarc], \
                          trunc(b_to_mb(sizes['ignoredir'][notinarc])), \
                          counts['ignoredir'][inarc], \
                          trunc(b_to_mb(sizes['ignoredir'][inarcunc])), \
                          trunc(b_to_mb(sizes['ignoredir'][inarccomp])))

    summary += row.format("Source Files", \
                          counts['detect_wizard_src'][notinarc], \
                          trunc(b_to_mb(sizes['detect_wizard_src'][notinarc])), \
                          counts['detect_wizard_src'][inarc], \
                          trunc(b_to_mb(sizes['detect_wizard_src'][inarcunc])), \
                          trunc(b_to_mb(sizes['detect_wizard_src'][inarccomp])))

    summary += row.format("JAR Archives", \
                          counts['jar'][notinarc], \
                          trunc(b_to_mb(sizes['jar'][notinarc])), \
                          counts['jar'][inarc], \
                          trunc(b_to_mb(sizes['jar'][inarcunc])), \
                          trunc(b_to_mb(sizes['jar'][inarccomp])))

    summary += row.format("Binary Files", \
                          counts['bin'][notinarc], \
                          trunc(b_to_mb(sizes['bin'][notinarc])), \
                          counts['bin'][inarc], \
                          trunc(b_to_mb(sizes['bin'][inarcunc])), \
                          trunc(b_to_mb(sizes['bin'][inarccomp])))

    summary += row.format("Other Files", \
                          counts['other'][notinarc], \
                          trunc(b_to_mb(sizes['other'][notinarc])), \
                          counts['other'][inarc], \
                          trunc(b_to_mb(sizes['other'][inarcunc])), \
                          trunc(b_to_mb(sizes['other'][inarccomp])))

    summary += row.format("Package Mgr Files", \
                          counts['det'][notinarc], \
                          trunc(b_to_mb(sizes['det'][notinarc])), \
                          counts['det'][inarc], \
                          trunc(b_to_mb(sizes['det'][inarcunc])), \
                          trunc(b_to_mb(sizes['det'][inarccomp])))

    summary += row.format("OS Package Files", \
                          counts['pkg'][notinarc], \
                          trunc(b_to_mb(sizes['pkg'][notinarc])), \
                          counts['pkg'][inarc], \
                          trunc(b_to_mb(sizes['pkg'][inarcunc])), \
                          trunc(b_to_mb(sizes['pkg'][inarccomp])))

    summary += "--------------------  --------------   --------------   -------------   -------------   -------------\n"

    summary += row.format("Large Files (>{:1d}MB)".format(trunc(b_to_mb(largesize))), \
                          counts['large'][notinarc], \
                          trunc(b_to_mb(sizes['large'][notinarc])), \
                          counts['large'][inarc], \
                          trunc(b_to_mb(sizes['large'][inarcunc])), \
                          trunc(b_to_mb(sizes['large'][inarccomp])))

    summary += row.format("Huge Files (>{:2d}MB)".format(trunc(b_to_mb(hugesize))), \
                          counts['huge'][notinarc], \
                          trunc(b_to_mb(sizes['huge'][notinarc])), \
                          counts['huge'][inarc], \
                          trunc(b_to_mb(sizes['huge'][inarcunc])), \
                          trunc(b_to_mb(sizes['huge'][inarccomp])))

    summary += "--------------------  --------------   --------------   -------------   -------------   -------------\n"

    #summary += rep + "\n"

    if not critical_only:
        print(summary)
    if f:
        f.write(summary)


def pack_binaries(path_list, fname="binary_files.zip"):
    global binpack
    binpack = None
    try:
        with zipfile.ZipFile(os.path.join(args.scanfolder, fname), 'w') as binzip:
            for bin_path in path_list:
                binzip.write(os.path.relpath(bin_path, os.curdir))

    except RuntimeError:
        traceback.print_last(file=sys.stderr)
    finally:
        binpack = fname
        return fname


def signature_process(folder, f):
    use_json_splitter = False
    # test if we should exclude signature scanner
    result = sig_scan_actionable.test(sensitivity=args.sensitivity)
    if result.outcome != "NO-OP":
        c.str_add('reqd', result.outcome)
        cli_msgs_dict['reqd'] += "{}\n".format(result.outcome)

    # Find duplicates without expanding archives - to avoid processing dups
    print("- Processing folders         ", end="", flush=True)
    num_dirdups, size_dirdups = process_dirdups(f)
    print(" Done")

    print("- Processing large files     ", end="", flush=True)
    num_dups, size_dups = process_largefiledups(f)
    print(" Done")

    print("- Processing Signature Scan  .....", end="", flush=True)
    retval = json_splitter_actionable.test(sensitivity=args.sensitivity,
                                           scan_size=b_to_gb(sizes['file'][notinarc] + sizes['arc'][notinarc]))
    # Produce Recommendations
    if retval.outcome != "NO-OP":
        use_json_splitter = True
        for property in retval.outcome:
            c.str_add('size', property)

    if sizes['file'][notinarc] + sizes['arc'][notinarc] > 2000000000:
        recs_msgs_dict['imp'] += "- IMPORTANT: Overall scan size ({:>,d} MB) is large\n".format(
            trunc((sizes['file'][notinarc] + sizes['arc'][notinarc]) / 1000000)) + \
                                 "    Impact:  Will impact Capacity license usage\n" + \
                                 "    Action:  Ignore folders, remove large files or use repeated scans of sub-folders (Also consider detect_advisor -b option to create multiple .bdignore files to ignore duplicate folders)\n\n"

    if counts['file'][notinarc] + counts['file'][inarc] > 1000000:
        recs_msgs_dict['imp'] += "- IMPORTANT: Overall number of files ({:>,d}) is very large\n".format(
            trunc((counts['file'][notinarc] + counts['file'][inarc]))) + \
                                 "    Impact:  Scan time could be VERY long\n" + \
                                 "    Action:  Ignore folders or split project (scan sub-projects or consider detect_advisor -b option to create multiple .bdignore files to ignore duplicate folders)\n\n"

    elif counts['file'][notinarc] + counts['file'][inarc] > 200000:
        recs_msgs_dict['info'] += "- INFORMATION: Overall number of files ({:>,d}) is large\n".format(
            trunc((counts['file'][notinarc] + counts['file'][inarc]))) + \
                                  "    Impact:  Scan time could be long\n" + \
                                  "    Action:  Ignore folders or split project (scan sub-projects or consider detect_advisor -b option to create multiple .bdignore files to ignore duplicate folders)\n\n"

    #
    # Need to add check for nothing to scan (no supported scan files)
    if counts['detect_wizard_src'][notinarc] + counts['detect_wizard_src'][inarc] + counts['jar'][notinarc] + counts['jar'][inarc] + \
            counts['other'][notinarc] + counts['other'][inarc] == 0:
        recs_msgs_dict['info'] += "- INFORMATION: No source, jar or other files found\n".format(
            trunc((counts['file'][notinarc] + sizes['file'][inarc]))) + \
                                  "    Impact:  Scan may not detect any OSS from files (dependencies only)\n" + \
                                  "    Action:  Check scan location is correct\n"

    if sizes['bin'][notinarc] + sizes['bin'][inarc] > 20000000:
        recs_msgs_dict['imp'] += "- IMPORTANT: Large amount of data ({:>,d} MB) in {} binary files found\n".format(
            trunc((sizes['bin'][notinarc] + sizes['bin'][inarc]) / 1000000), len(bin_list)) + \
                                 "    Impact:  Binary files not analysed by standard scan, will impact Capacity license usage\n" + \
                                 "    Action:  Remove files or ignore folders (using .bdignore files), also consider zipping\n" + \
                                 "             files and using Binary scan (See report file produced with -r option)\n\n"

    binzip_list = {bin.split("##")[0] for bin in
                   bin_list}  # if '##' isn't found, the whole string is still in idx 0 of output
    if len(binzip_list) > 1:
        bin_pack_name = pack_binaries(binzip_list)
    elif len(binzip_list) > 0:
        bin_pack_name = binzip_list.pop()
    else:
        bin_pack_name = None
    result = binary_matching_actionable.test(sensitivity=args.sensitivity, num_binaries=len(binzip_list),
                                             bin_pack_name=bin_pack_name, no_write=False, bdba_enable=args.binary)
    if result.outcome != "NO-OP":
        c.str_add('size', result.outcome)

    if size_dirdups > 20000000:
        pass
    retval = directory_dupes_actionable.test(sensitivity=args.sensitivity)
    if retval.outcome != "NO-OP":
        for apath, bpath in dup_dir_dict.items():
            if bpath.find("##") < 0:
                bdignore_list.append(bpath)

    if size_dups > 20000000:
        pass

    for apath, bpath in dup_large_dict.items():
        if bpath.find("##") < 0:
            bdignore_list.append(bpath)

        # TODO How should we deal with this?
        if cli_msgs_dict['lic'].find("upload.source.mode") < 0:
            cli_msgs_dict['lic'] += "--detect.blackduck.signature.scanner.upload.source.mode=true\n" + \
                                    "    (CAUTION - will upload local source files)\n"

            c.str_add('lic', "--detect.blackduck.signature.scanner.upload.source.mode=true")
            c.str_add('lic', "    (CAUTION - will upload local source files)")

    check_singlefiles(f)
    result = indiv_file_match_actionable.test(sensitivity=args.sensitivity)
    if result.outcome != "NO-OP":
        c.str_add('scan', result.outcome)

    result = file_snippet_match_actionable.test(sensitivity=args.sensitivity, scan_focus=args.focus)
    if result.outcome != "NO-OP":
        c.str_add('scan', result.outcome)

    print(" Done")
    print("")
    return use_json_splitter


def detector_process(folder, f):
    import shutil

    global rep
    global det_max_depth
    global det_min_depth
    global c

    print("- Processing Dependency Scan .....", end="", flush=True)

    if f:
        f.write("PROJECT FILES FOUND:\n")
    c.clear_group('dep')
    count = 0
    det_depth1 = 0
    det_other = 0
    cmds_missing1 = ""
    cmds_missingother = ""
    cmds_missing_list = []
    det_max_depth = 0
    det_min_depth = 100
    det_in_arc = 0
    if len(det_dict) > 0:
        for detpath, depth in det_dict.items():
            command_exists = False
            if detpath.find("##") > 0:
                # in archive
                det_in_arc += 1
            else:
                if depth == 1:
                    det_depth1 += 1
                elif depth > 1:
                    det_other += 1
                if depth > det_max_depth:
                    det_max_depth = depth
                if depth < det_min_depth:
                    det_min_depth = depth
                fname = os.path.basename(detpath)
                exes = ""
                if fname in detectors_file_dict.keys():
                    exes = detectors_file_dict[fname]
                elif os.path.splitext(fname)[1] in detectors_ext_dict.keys():
                    exes = detectors_ext_dict[os.path.splitext(fname)[1]]
                missing_cmds = ""
                for exe in exes:
                    if exe not in detectors_list:
                        detectors_list.append(exe)
                        if platform.system() != "Linux" and exe in linux_only_detectors:
                            if depth == 1:
                                recs_msgs_dict[
                                    'crit'] += "- CRITICAL: Package manager '{}' requires scanning on a Linux platform\n".format(
                                    exe) + \
                                               "    Impact:  Scan will fail\n" + \
                                               "    Action:  Re-run Detect scan on Linux\n\n"
                            else:
                                recs_msgs_dict[
                                    'imp'] += "- IMPORTANT: Package manager '{}' requires scanning on a Linux platform\n".format(
                                    exe) + \
                                              "    Impact:  Scan may fail if detector depth changed from default value 0\n" + \
                                              "    Action:  Re-run Detect scan on Linux\n\n"
                    if shutil.which(exe) is not None:
                        command_exists = True
                    else:
                        if exe not in cmds_missing_list:
                            cmds_missing_list.append(exe)
                            if missing_cmds:
                                missing_cmds += " OR " + exe
                            else:
                                missing_cmds = exe
                if f:
                    f.write("{}\n".format(detpath))
                    count += 1
                if not command_exists and missing_cmds:
                    if missing_cmds.find(" OR ") > 0:
                        missing_cmds = "(" + missing_cmds + ")"
                    if depth == 1:
                        if cmds_missing1:
                            cmds_missing1 += " AND " + missing_cmds
                        else:
                            cmds_missing1 = missing_cmds
                    else:
                        if cmds_missingother:
                            cmds_missingother += " AND " + missing_cmds
                        else:
                            cmds_missingother = missing_cmds

        rep = "\nPACKAGE MANAGER CONFIG FILES:\n" + \
              "- In invocation folder:   {}\n".format(det_depth1) + \
              "- In sub-folders:         {}\n".format(det_other) + \
              "- In archives:            {}\n".format(det_in_arc) + \
              "- Minimum folder depth:   {}\n".format(det_min_depth) + \
              "- Maximum folder depth:   {}\n".format(det_max_depth) + \
              "---------------------------------\n" + \
              "- Total discovered:       {}\n\n".format(len(det_dict)) + \
              "Config files for the following Package Managers found: {}\n".format(', '.join(detectors_list))

    if f and count == 0:
        f.write("    None\n")

    if det_depth1 == 0 and det_other > 0:
        recs_msgs_dict[
            'imp'] += "- IMPORTANT: No package manager files found in invocation folder but do exist in sub-folders\n" + \
                      "    Impact:  Dependency scan will not be run\n" + \
                      "    Action:  Specify --detect.detector.search.depth={} (although depth could be up to {})\n".format(
                          det_min_depth, det_max_depth) + \
                      "             optionally with --detect.detector.search.continue=true or scan sub-folders separately.\n\n"

        c.str_add('scan', '--detect.detector.search.depth={}'.format(det_min_depth))
        c.str_add('scan', '--detect.detector.search.continue=true')
        c.str_add('scan', "    (To find package manager files within sub-folders; note depth {} would find\n".format(
            det_max_depth) + \
                  "    all PM files in sub-folders but higher level projects may already include these)\n")
        if cli_msgs_dict['scan'].find("detector.search.depth") < 0:
            cli_msgs_dict['scan'] += "--detect.detector.search.depth={}\n".format(det_min_depth) + \
                                     "    optionally with optionally with -detect.detector.search.continue=true\n" + \
                                     "    (To find package manager files within sub-folders; note depth {} would find\n".format(
                                         det_max_depth) + \
                                     "    all PM files in sub-folders but higher level projects may already include these)\n"

    if det_depth1 == 0 and det_other == 0:
        recs_msgs_dict['info'] += "- INFORMATION: No package manager files found in project at all\n" + \
                                  "    Impact:  No dependency scan will be performed\n" + \
                                  "    Action:  This may be expected, but ensure you are scanning the correct location\n\n"

    result = buildless_mode_actionable.test(sensitivity=args.sensitivity)
    if result.outcome != "NO-OP":
        c.str_add('dep', result.outcome)
        cli_msgs_dict['dep'] += "{}\n".format(result.outcome)

    if cmds_missing1 or cmds_missingother:
        package_managers_missing.append(cmds_missing1)
        if 'clang' not in cmds_missing1 and 'clang' not in cmds_missingother:
            c.add('dep', Property('detect.detector.buildless', 'true', is_commented=True))
            for cmd in cmds_missing_list:
                c.add('dep', Property('detect.{}.path'.format(cmd), '<LOCATION>', is_commented=True))

    for cmd in detectors_list:
        if cmd in dev_dependency_pkg_manager_defaults:
            #parity = dev_dependency_pkg_manager_defaults[cmd]
            #pos_param = True
            #neg_param = False
            #if not parity:
            #    pos_param = False
            #    neg_param = True

            dev_dep_result = dev_dependencies_actionable.test(sensitivity=args.sensitivity, cmd=cmd,
                                                              dev_dependency_param_pos=True,
                                                              dev_dependency_param_neg=False)
            if dev_dep_result.outcome != "NO-OP":
                c.str_add('dep', dev_dep_result.outcome)
                cli_msgs_dict['dep'] += "{}\n".format(dev_dep_result.outcome)
        if cmd in detector_cli_options_dict.keys():
            for prop in detector_cli_options_dict[cmd].splitlines(keepends=False):
                c.str_add('dep', prop, is_commented=True)
            cli_msgs_dict['dep'] += " For {}:\n".format(cmd) + detector_cli_options_dict[cmd]
        if cmd in detector_cli_required_dict.keys():
            if 'crit' in cli_msgs_dict:
                cli_msgs_dict['crit'] += " For {}:\n".format(cmd) + detector_cli_required_dict[cmd]
            else:
                cli_msgs_dict['crit'] = " For {}:\n".format(cmd) + detector_cli_required_dict[cmd]
            for prop in detector_cli_required_dict[cmd].splitlines(keepends=False):
                c.str_add('dep', prop, is_commented=True)

    print(" Done")

    return


def output_recs(critical_only, f):
    global messages

    if f:
        f.write(messages + "\n")

    print(
        "+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n\nRECOMMENDATIONS:\n")
    if f:
        f.write("\nRECOMMENDATIONS:\n")

    if recs_msgs_dict['crit']:
        print(recs_msgs_dict['crit'])
        if f:
            f.write(recs_msgs_dict['crit'] + "\n")

    if recs_msgs_dict['imp']:
        if not critical_only:
            if recs_msgs_dict['crit']:
                print(
                    "-----------------------------------------------------------------------------------------------------")
            print(recs_msgs_dict['imp'])
        if f:
            if recs_msgs_dict['crit']:
                f.write(
                    "-----------------------------------------------------------------------------------------------------\n")
            f.write(recs_msgs_dict['imp'] + "\n")

    if recs_msgs_dict['info']:
        if not critical_only:
            if recs_msgs_dict['crit'] or recs_msgs_dict['imp']:
                print(
                    "-----------------------------------------------------------------------------------------------------")
            print(recs_msgs_dict['info'])
        if f:
            if recs_msgs_dict['crit'] or recs_msgs_dict['imp']:
                f.write(
                    "-----------------------------------------------------------------------------------------------------\n")
            f.write(recs_msgs_dict['info'] + "\n")

    if (not recs_msgs_dict['crit'] and not recs_msgs_dict['imp'] and not recs_msgs_dict['info']):
        print("- None\n")
        if f:
            f.write("None\n")

    if (critical_only and not recs_msgs_dict['crit']):
        print("- No Critical Recommendations\n")

    if len(bdignore_list) > 0 and f:
        f.write(
            "\nFOLDERS WHICH COULD BE IGNORED:\n(Multiple .bdignore files must be created in sub-folders - folder names must use /folder/ pattern)\n\n")
        for bpath in bdignore_list:
            f.write(bpath)


def check_prereqs():
    global rep
    global messages

    # Check java
    try:
        if shutil.which("java") is None:
            recs_msgs_dict['crit'] += "- CRITICAL: Java is not installed or on the PATH\n" + \
                                      "    Impact:  Detect program will fail\n" + \
                                      "    Action:  Install OpenJDK 1.8 or 1.11\n\n"
            c.str_add('reqd', '--detect.java.path=<PATH_TO_JAVA>')
            c.str_add('reqd', "    (If Java installed, specify path to java executable if not on PATH)")
        #             if cli_msgs_dict['reqd'].find("detect.java.path") < 0:
        #                 cli_msgs_dict['reqd'] += ""    --detect.java.path=<PATH_TO_JAVA>\n" + \
        #                 "    (If Java installed, specify path to java executable if not on PATH)\n"
        else:
            try:
                javaoutput = subprocess.check_output(['java', '-version'], stderr=subprocess.STDOUT)
                crit = True
                if javaoutput:
                    line0 = javaoutput.decode("utf-8").splitlines()[0]
                    prog = line0.split(" ")[0].lower()
                    if prog:
                        version_string = line0.split('"')[1]
                        if version_string:
                            major, minor, _ = version_string.split('.')
                            if prog == "openjdk":
                                crit = False
                                if major == "8" or major == "11":
                                    rec = "none"
                                else:
                                    recs_msgs_dict[
                                        'imp'] += "- IMPORTANT: OpenJDK version {} is not documented as supported by Detect\n".format(
                                        version_string) + \
                                                  "    Impact:  Scan may fail\n" + \
                                                  "    Action:  Check that Detect operates correctly\n\n"
                            elif prog == "java":
                                crit = False
                                if major == "1" and (minor == "8" or minor == "11"):
                                    rec = "none"
                                else:
                                    recs_msgs_dict[
                                        'imp'] += "- IMPORTANT: Java version {} is not documented as supported by Detect\n".format(
                                        version_string) + \
                                                  "    Impact:  Scan may fail\n" + \
                                                  "    Action:  Check that Detect operates correctly\n\n"
            except:
                crit = True

            if crit:
                recs_msgs_dict['crit'] += "- CRITICAL: Java program version cannot be determined\n" + \
                                          "    Impact:  Scan may fail\n" + \
                                          "    Action:  Check Java or OpenJDK version 1.8 or 1.11 is installed\n\n"
                c.str_add('reqd', '--detect.java.path=<PATH_TO_JAVA>')
                c.str_add('reqd', "    (If Java installed, specify path to java executable if not on PATH)")
    #                 if cli_msgs_dict['reqd'].find("detect.java.path") < 0:
    #                     cli_msgs_dict['reqd'] += "--detect.java.path=<PATH_TO_JAVA>\n" + \
    #                     "    (If Java installed, specify path to java executable if not on PATH)\n"

    except:
        recs_msgs_dict['crit'] += "- CRITICAL: Java is not installed or on the PATH\n" + \
                                  "    Impact:  Detect program will fail\n" + \
                                  "    Action:  Install OpenJDK 1.8 or 1.11\n\n"
        c.str_add('reqd', '--detect.java.path=<PATH_TO_JAVA>')
        c.str_add('reqd', "    (If Java installed, specify path to java executable if not on PATH)")
    #         if cli_msgs_dict['reqd'].find("detect.java.path") < 0:
    #             cli_msgs_dict['reqd'] += "--detect.java.path=<PATH_TO_JAVA>\n" + \
    #             "    (If Java installed, specify path to java executable if not on PATH)\n"

    os_platform = ""
    if platform.system() == "Linux" or platform.system() == "Darwin":
        os_platform = "linux"
        # check for bash and curl
        if shutil.which("bash") is None:
            recs_msgs_dict['crit'] += "- CRITICAL: Bash is not installed or on the PATH\n" + \
                                      "    Impact:  Detect program will fail\n" + \
                                      "    Action:  Install Bash or add to PATH\n\n"
    else:
        os_platform = "win"

    if shutil.which("curl") is None:
        recs_msgs_dict['crit'] += "- CRITICAL: Curl is not installed or on the PATH\n" + \
                                  "    Impact:  Detect program will fail\n" + \
                                  "    Action:  Install Curl or add to PATH\n\n"
    else:
        if not check_connection("https://detect.synopsys.com"):
            recs_msgs_dict['crit'] += "- CRITICAL: No connection to https://detect.synopsys.com\n" + \
                                      "    Impact:  Detect wrapper script cannot be downloaded, Detect cannot be started\n" + \
                                      "    Action:  Either configure proxy (See CLI section) or download Detect manually and run offline (see docs)\n\n"

            cli_msgs_dict['detect'] = cli_msgs_dict["detect_" + os_platform + "_proxy"]
            c.str_add('detect', cli_msgs_dict["detect_" + os_platform + "_proxy"], is_commented=True)
        else:
            cli_msgs_dict['detect'] = cli_msgs_dict["detect_" + os_platform]
            c.str_add('detect', cli_msgs_dict["detect_" + os_platform], is_commented=True)
            if not check_connection("https://sig-repo.synopsys.com"):
                recs_msgs_dict['crit'] += "- CRITICAL: No connection to https://sig-repo.synopsys.com\n" + \
                                          "    Impact:  Detect jar cannot be downloaded; Detect cannot run\n" + \
                                          "    Action:  Either configure proxy (See CLI section) or download Detect manually and run offline (see docs)\n\n"


def check_connection(url):
    import subprocess

    try:
        output = subprocess.check_output(['curl', '-s', '-m', '5', url], stderr=subprocess.STDOUT)
        return True
    except:
        return False


def check_docker_prereqs():
    import shutil
    import subprocess

    if platform.system() != "Linux" and platform.system() != "Darwin":
        recs_msgs_dict['crit'] += "- CRITICAL: Docker image scanning only supported on Linux or MacOS\n" + \
                                  "    Impact:  Docker image scan will fail\n" + \
                                  "    Action:  Perform scan Docker on Linux or MacOS\n\n"
    else:
        if shutil.which("docker") is None:
            recs_msgs_dict['crit'] += "- CRITICAL: Docker not installed - required for Docker image scanning\n" + \
                                      "    Impact:  Docker image scan will fail\n" + \
                                      "    Action:  Install docker\n\n"
        else:
            try:
                output = subprocess.check_output(['docker', 'run', 'hello-world'], stderr=subprocess.STDOUT)
            except:
                recs_msgs_dict['crit'] += "- CRITICAL: Docker could not be started\n" + \
                                          "    Impact:  Detect image scan will fail (docker inspector cannot be started)\n" + \
                                          "    Action:  Check docker permissions OR not running within container\n\n"

        if shutil.which("curl") is not None:
            if not check_connection("https://blackducksoftware.github.io"):
                recs_msgs_dict['crit'] += "- CRITICAL: No connection to https://blackducksoftware.github.io\n" + \
                                          "    Impact:  Detect docker inspector cannot be downloaded; online scan cannot be performed\n" + \
                                          "    Action:  Download docker inspector manually and run offline (see docs)\n\n"


def output_cli(critical_only, report, f):
    output = "+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++\n\nDETECT CLI:\n\n"
    if recs_msgs_dict['crit']:
        output += "Note that scan will probably fail - see CRITICAL recommendations above\n\n"

    output += "    DETECT COMMAND:\n"
    output += re.sub(r"^", "    ", cli_msgs_dict['detect'], flags=re.MULTILINE)
    output += "\n    MINIMUM REQUIRED OPTIONS:\n"
    output += re.sub(r"^", "    ", cli_msgs_dict['reqd'], flags=re.MULTILINE)

    print(output)

    if len(bdignore_list) > 0:
        if report:
            print(
                "        (Note that '.bdignore' exclude file is recommended - see the report file '{}' or use '-b' option\n" + \
                "        to create '.bdignore' files in sub-folders)\n".format(report))
        else:
            print(
                "        (Note that '.bdignore' exclude file is recommended - create a report file using '-r repfile' to\n" + \
                "        see recommended folders to exclude or use '-b' option to create '.bdignore' files in sub-folders)\n")
    if f:
        f.write(output + "\n")

    output = ""
    if cli_msgs_dict['scan'] != '':
        output += "\nOPTIONS TO IMPROVE SCAN COVERAGE:\n" + cli_msgs_dict['scan'] + "\n"

    if cli_msgs_dict['size'] != '':
        output += "\nOPTIONS TO REDUCE SIGNATURE SCAN SIZE:\n" + cli_msgs_dict['size'] + "\n"

    if cli_msgs_dict['dep'] != '':
        output += "\nOPTIONS TO OPTIMIZE DEPENDENCY SCAN:\n" + cli_msgs_dict['dep'] + "\n"

    if cli_msgs_dict['lic'] != '':
        output += "\nOPTIONS TO IMPROVE LICENSE COMPLIANCE ANALYSIS:\n" + cli_msgs_dict['lic'] + "\n"

    if cli_msgs_dict['proj'] != '':
        output += "\nPROJECT OPTIONS:\n" + cli_msgs_dict['proj'] + "\n"

    if cli_msgs_dict['rep'] != '':
        output += "\nREPORTING OPTIONS:\n" + cli_msgs_dict['rep'] + "\n"

    if cli_msgs_dict['docker'] != '':
        output += "\nDOCKER IMAGES TO SCAN:\n" + cli_msgs_dict['docker'] + "\n"

    output = re.sub(r"^", "    ", output, flags=re.MULTILINE)
    print(output)
    if not critical_only:
        print(output)
    if f:
        f.write(output + "\n")

    if f:
        print("INFO: Output report file '{}' created".format(report))
    else:
        print("INFO: Use '-r repfile' to produce report file with more information")


def create_bdignores():
    filecount = 0
    foldercount = 0

    for bdpath in bdignore_list:
        bdignore_file = os.path.join(os.path.dirname(bdpath), ".bdignore")
        if not os.path.exists(bdignore_file):
            try:
                b = open(bdignore_file, "a")
                b.write("/" + os.path.basename(bdpath) + "/\n")
                b.close()
                # print("INFO: '.bdignore' file created in project folder")
                filecount += 1
            except Exception as e:
                print('ERROR: Unable to create .bdignore file\n' + str(e))
        else:
            # Check whether entry exists
            try:
                b = open(bdignore_file, "r")
                lines = b.readlines()
                exists = False
                for line in lines:
                    if line == "/" + os.path.basename(bdpath) + "/\n":
                        exists = True
                b.close()
                if not exists:
                    b = open(bdignore_file, "a")
                    b.write("/" + os.path.basename(bdpath) + "/\n")
                    b.close()
            except Exception as e:
                print('ERROR: Unable to update .bdignore file\n' + str(e))
        foldercount += 1
    print("INFO: Created/updated {} .bdignore files to ignore {} folders\n".format(filecount, foldercount))


def output_config(conffile, c):
    # config_file = os.path.join(projdir, "application-project.yml")
    # config_file = os.path.join(os.getcwd(), conffile)
    # config_file = os.path "application-project.yml")
    if not os.path.exists(conffile):
        config = "#\n# EXAMPLE PROJECT CONFIG FILE\n" + \
                 "# Uncomment and update required options\n#\n#\n" + \
                 "# DETECT COMMAND TO RUN:\n#\n" + cli_msgs_dict['detect'] + "\n" + \
                 "# MINIMUM REQUIRED OPTIONS:\n#\n" + cli_msgs_dict['reqd'] + "\n" + \
                 "# OPTIONS TO IMPROVE SCAN COVERAGE:\n#\n" + cli_msgs_dict['scan'] + "\n" + \
                 "# OPTIONS TO REDUCE SIGNATURE SCAN SIZE:\n#\n" + cli_msgs_dict['size'] + "\n" + \
                 "# OPTIONS TO CONFIGURE DEPENDENCY SCAN:\n#\n" + cli_msgs_dict['dep'] + "\n" + \
                 "# OPTIONS TO IMPROVE LICENSE COMPLIANCE ANALYSIS:\n#\n" + cli_msgs_dict['lic'] + "\n" + \
                 "# PROJECT OPTIONS:\n#\n" + cli_msgs_dict['proj'] + "\n" + \
                 "# REPORTING OPTIONS:\n#\n" + cli_msgs_dict['rep'] + "\n" + \
                 "# DOCKER SCANNING:\n#\n" + cli_msgs_dict['docker'] + "\n"

        config = re.sub("=", ": ", config)
        config = re.sub(r"\n ", r"\n#", config, flags=re.S)
        config = re.sub(r"\n--", r"\n#", config, flags=re.S)
        try:
            cf = open(conffile, "a")
            cf.write(str(c)) # Writes new config file instead of old.
            cf.close()
        #             print("INFO: Config file 'application-project.yml' file written to project folder (Edit to uncomment options)\n" + \
        #             "      - Use '--spring.profiles.active=project' to specify this configuration")
        except Exception as e:
            print('ERROR: Unable to create project config file ' + str(e))
    else:
        print("INFO: Project config file 'application-project.yml' already exists - not updated")


def get_input_yn(prompt, default):
    value = input(prompt)
    if value == "":
        return default
    ret = value[0].lower()
    if ret == "y":
        return True

    elif ret == "n":
        return False


def get_input(prompt, default):
    value = input(prompt)
    if value == "":
        return default
    return value


def backup_file(filename, filetype):
    if os.path.isfile(filename):
        # Determine root filename so the extension doesn't get longer
        n, e = os.path.splitext(filename)
        # Is e an integer?
        try:
            num = int(e)
            root = n
        except ValueError:
            root = filename

        # Find next available file version
        for i in range(1000):
            new_file = "{}.{:03d}".format(root, i)
            if not os.path.isfile(new_file):
                os.rename(filename, new_file)
                print("INFO: Moved existing {} file '{}' to '{}'\n".format(filetype, filename, new_file))
                return new_file
    return None


def interactive(scanfolder, url, api, sensitivity, focus, no_scan, project_name, project_version, trust_cert):
    if scanfolder is None or scanfolder == "":
        scanfolder = os.getcwd()
    try:
        scanfolder = get_input("Enter project folder to scan (default current folder '{}'):".format(scanfolder),
                               scanfolder)
    except:
        print("Exiting")
        return "", "", "", 0, False, ""
    if not os.path.isdir(scanfolder):
        print("Scan location '{}' does not exist\nExiting".format(scanfolder))
        return "", "", "", 0, False, ""
    try:
        url = get_input("Black Duck Server URL [{}]: ".format(url), url)
        api = get_input("Black Duck API Token [{}]: ".format(api), api)
        sensitivity = int(get_input("Scan sensitivity/coverage (1-5) where 1 = dependency scan only, "
                                    "5 = all scan types including all potential matches [{}]: ".format(sensitivity),
                                    sensitivity))
        focus = str(get_input("Scan Focus (License Compliance (l) / Security (s) / Both (b)) [{}]: ".format(focus),
                              focus))
        project_name = str(get_input("Black Duck Project Name [{}]: ".format(project_name), project_name))
        project_version = str(get_input("Black Duck Project Version [{}]: ".format(project_version), project_version))
        scandef = "n" if no_scan else "y"
        no_scan = not get_input_yn("Run Detect scan (y/n) [{}]: ".format(scandef), scandef)
        trust_cert = True if str(get_input("Dissable SSL verification and automatically trust the certificate (required for self-signed certs) (y/n) [{}]: ".format(trust_cert), trust_cert)) in ('y', 'Y', 'yes', 'Yes') else False
    except:
        print("Exiting")
        return "", "", "", 0, False, ""
    return scanfolder, url, api, sensitivity, focus, no_scan, project_name, project_version, trust_cert


def get_detector_search_depth():
    global args

    result = detector_search_depth_actionable.test(sensitivity=args.sensitivity)
    if result is not None and result.outcome != "NO-OP":
        cli_msgs_dict['scan'] += "detect.detector.search.depth: {}\n".format(result.outcome)
        cli_msgs_dict['scan'] += "detect.detector.search.continue: true\n"
        c.str_add('scan', "detect.detector.search.depth: {}".format(result.outcome), is_commented=False)
        c.str_add('scan', "detect.detector.search.continue: true", is_commented=False)

    return result.outcome


def get_detector_exclusion_args():
    detector_exclusion_args = []

    def detector_exclusions_func():
        detector_exclusion_args = []
        detector_exclusions = ['*test*', '*samples*', '*examples*']

        detector_exclusion_args.append(
            'detect.detector.search.exclusion.patterns: \'{}\''.format(','.join(detector_exclusions)))

        detector_exclusion_args.append(
                'detect.gradle.excluded.configurations: *test*,*Test*')

        detector_exclusion_args.append('detect.maven.excluded.scopes: test')

        return detector_exclusion_args

    result = detector_exclusions_actionable.test(sensitivity=args.sensitivity,
                                                 detector_exclusions_func=detector_exclusions_func)
    if result.outcome != "NO-OP":
        if type(result.outcome) == list:
            for r in result.outcome:
                cli_msgs_dict['size'] += "{}\n".format(r)
                c.str_add('size', r)
        else:
            cli_msgs_dict['size'] += "{}\n".format(result.outcome)
            c.str_add('size', result.outcome)

    return detector_exclusion_args


def get_detector_args():
    detector_args = []
    for item in get_detector_exclusion_args():
        detector_args.append(item)
    result = license_search_actionable.test(scan_focus=args.focus)
    if result.outcome != "NO-OP":
        cli_msgs_dict['scan'] += "{}\n".format(result.outcome)

        c.str_add('scan', result.outcome[0])
        c.str_add('scan', result.outcome[1])
    return detector_args


def uncomment_line(line, key):
    if key in line:
        return line.replace('#', '')
    else:
        return line


def uncomment_min_required_options(data, start_index, end_index):
    global args
    global exclude_detector

    c.uncomment_like('blackduck.url')
    c.uncomment_like('blackduck.api.token')
    c.uncomment_like('detect.source.path')
    c.uncomment_like('detect.detector.buildless')

    exclude_detector = False
    for line in data[start_index:end_index]:
        if "blackduck.url" in line or "detect.source.path" in line:
            data[data.index(line)] = uncomment_line(line)
            continue
        if 'detect.detector.buildless' in line:
            data[data.index(line)] = uncomment_line(line, "detect.detector.buildless")
        if 'blackduck.api.token' in line:
            data[data.index(line)] = line.replace('#', '').replace('API_TOKEN', args.api_token)
        elif 'blackduck.url' in line:
            data[data.index(line)] = line.replace('#', '').replace('BLACKDUCK_URL', args.url)
        elif line.strip().startswith('#blackduck') or line.strip().startswith('#detect'):
            data[data.index(line)] = uncomment_line(line)
    return data


def uncomment_improve_scan_coverage_options(data, start_index, end_index):
    individual_file_matching_uncommented = False
    get_detector_search_depth()
    for line in data[start_index:end_index]:
        # TODO IS THIS THE WAY IT SHOULD BE? that we don't get the chance to set this?
        if 'detect.detector.search.depth' in line and get_detector_search_depth():
            data[data.index(line)] = 'detect.detector.search.depth: {}\n'.format(get_detector_search_depth())
            data.append('detect.detector.search.continue: true\n')

        if 'detect.blackduck.signature.scanner.individual.file.matching' in line and not individual_file_matching_uncommented:
            data[data.index(line)] = uncomment_line(line,
                                                    'detect.blackduck.signature.scanner.individual.file.matching: SOURCE')
            individual_file_matching_uncommented = True

        if 'detect.blackduck.signature.scanner.snippet.matching' in line:
            data[data.index(line)] = uncomment_line(line,
                                                    'detect.blackduck.signature.scanner.snippet.matching: SNIPPET_MATCHING')

        if 'detect.binary.scan.file.path' in line:
            data[data.index(line)] = uncomment_line(line, 'detect.binary.scan.file.path')

    return data


def uncomment_reduce_sig_scan_size_options(data, start_index, end_index):
    #c.uncomment_property('detect.tools.excluded')
    #for line in data[start_index:end_index]:
    #    if 'detect.tools.excluded' in line:
    #        data[data.index(line)] = uncomment_line(line, 'detect.tools.excluded')
    return data


def uncomment_optimize_dependency_options(data, start_index, end_index):
    # TODO: IT DOESNT SEEM LIKE THIS EVER IS SET ANYWHERE - AND WE REWRITE THE CONFIG EVERY TIME...
    for line in data[start_index:end_index]:

        if args.sensitivity > 2:
            data[data.index(line)] = uncomment_line(line, 'dev.dependencies: true')

        elif args.sensitivity < 3:
            data[data.index(line)] = uncomment_line(line, 'dev.dependencies: false')

    return data


def uncomment_line(line, key=None):
    if key:
        if key in line:
            return line.replace('#', '')
        else:
            return line
    return line.replace('#', '')


def uncomment_line_from_data(data, key):
    for line in data:
        data[data.index(line)] = uncomment_line(line, key)


def json_splitter(scan_path, maxNodeEntries=200000, maxScanSize=4500000000):
    """
    Splits a json file into multiple jsons so large scans can be broken up with multi-part uploads
    Modified from source: https://github.com/blackducksoftware/json-splitter
    """
    new_scan_files = []

    with open(scan_path, 'r') as f:
        scanData = json.load(f)

    dataLength = len(scanData['scanNodeList'])

    scanName = scanData['name']
    scanNodeList = scanData.pop('scanNodeList')
    scanData.pop('scanProblemList')
    scanData['scanProblemList'] = []
    base = scanNodeList[0]

    # Computing split points for the file
    #
    scanChunkSize = 0
    scanChunkNodes = 0
    splitAt = [0]
    for i in range(0, dataLength - 1):
        if scanChunkSize + scanNodeList[i + 1]['size'] > maxScanSize or scanChunkNodes + 1 > maxNodeEntries:
            scanChunkSize = 0
            scanChunkNodes = 0
            splitAt.append(i)
        if scanNodeList[i]['uri'].startswith('file://'):
            scanChunkSize = scanChunkSize + scanNodeList[i]['size']
        scanChunkNodes += 1

    # Create array of split points shifting by one position
    splitTo = splitAt[1:]
    splitTo.append(None)

    # Splitting and writing the chunks
    #

    for i in range(len(splitAt)):
        print("Processing range {}, {}".format(splitAt[i], splitTo[i]))
        # for i in range(0, dataLength, maxNodeEntries):
        nodeData = scanNodeList[splitAt[i]:splitTo[i]]
        if i > 0:
            nodeData.insert(0, base)
        # scanData['baseDir'] = baseDir + "-" + str(i)
        scanData['scanNodeList'] = nodeData
        scanData['name'] = scanName + "-" + str(splitAt[i])
        filename = scan_path + "-" + str(splitAt[i]) + '.json'
        with open(filename, 'w') as outfile:
            json.dump(scanData, outfile)
        scanData.pop('scanNodeList')
        new_scan_files.append(filename)

    return new_scan_files


def generate_detect_config(config_file):

    detector_args = get_detector_args()

    c.uncomment_property('detect.docker.tar')
    c.uncomment_like('blackduck.url')
    c.uncomment_like('blackduck.api.token')
    c.uncomment_like('detect.source.path')
    c.uncomment_like('detect.detector.buildless')
    get_detector_search_depth()
    if args.hub_project is not None and args.hub_project != "None":
        c.str_add('proj', 'detect.project.name: \'{}\''.format(args.hub_project), should_update=True)
    if args.hub_version is not None and args.hub_version != "None":
        c.str_add('proj', 'detect.project.version.name: \'{}\''.format(args.hub_version), should_update=True)

    if use_json_splitter:
        c.str_add('size', 'blackduck.offline.mode: true', is_commented=False)
        c.str_add('size', 'detect.bom.aggregate.name: detect_advisor_run_{}'.format(datetime.now()), is_commented=False)

    with open(config_file, 'w') as f:
        f.writelines(str(c))


def file_tree_string(start_path, max_depth=10):
    return (p.displayable() for p in PathTree.make_tree(start_path, max_depth=max_depth))


def run_detect(config_file):

    detect_command = c['detect'].get_line(1).strip() + ' ' \
                     + '--spring.profiles.active=project' + ' ' \
                     + ' --spring.config.location="file:' + config_file + '"'

    print("Running command: {}\n".format(detect_command))
    if platform.system() == "Windows":
        detect_command = 'powershell "[Net.ServicePointManager]::SecurityProtocol = \'tls12\'; irm https://detect.synopsys.com/detect.ps1?$(Get-Random) | iex; detect"' + ' ' \
                     + '--spring.profiles.active=project' + ' ' \
                     + ' --spring.config.location="file:' + config_file + '"'

        p = subprocess.Popen(["powershell.exe",
                              detect_command],
                             stdout=subprocess.PIPE)
    else:
        p = subprocess.Popen(detect_command, shell=True, executable='/bin/bash', stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    stdout = p.stdout
    with open(os.path.join(args.scanfolder, 'latest_detect_run.txt'), "w+") as out_file:
        out_file.write(Actionable.wl.make_table(args.sensitivity))
        while True:
            std_out_output = stdout.readline()
            if std_out_output == '' or re.findall(r'Result code of [0-9]*, exiting', std_out_output.decode('utf-8')):
                break
            if std_out_output:
                out_file.write(std_out_output.decode('utf-8'))
                print(std_out_output.decode('utf-8').strip())

    with open(os.path.join(args.scanfolder, 'latest_detect_run.txt'), "r") as out_file:
        file_contents = out_file.read()

    detect_status = re.search(r'Overall Status: (.*)\n', file_contents)
    bom_location = re.search(r'Black Duck Project BOM: (.*)\n', file_contents)

    if use_json_splitter:
        print("Using JSON splitter")
        # upload scan files

        match = re.search(r'Run directory: (.*)\n', file_contents)
        output_directory = None

        if match:
            output_directory = match.group(1)
            json_files = glob.glob('{}/scan/BlackDuckScanOutput/*/data/*.json'.format(output_directory))
            bdio_files = glob.glob('{}/bdio/*.jsonld'.format(output_directory))
            if bdio_files:
                bdio_file = bdio_files[0]
            if json_files:
                json_file = json_files[0]

            json_lst = json_splitter(json_file)

            with open('.restconfig.json', 'w') as f:
                json_data = {"baseurl": args.url, "api_token": args.api_token, "insecure": args.trust_cert, "debug": False}
                json.dump(json_data, f)
            hub = HubInstance()
            print("Will upload 1 bdio file and {} json files".format(str(len(json_lst))))
            hub.upload_scan(bdio_file)
            print("Uploaded bdio file: {}".format(bdio_file))

            for f in json_lst:
                hub.upload_scan(f)
                print("Uploaded json file {}".format(f))
                os.remove(f)  # don't keep a million jsons

            print("All files have been uploaded")
        if not output_directory:
            print("Output directory could not be located. Dry run and BDIO files were not uploaded.")

    print("Detect logs written to: {}".format(out_file.name))
    if detect_status:
        print("Detect run complete. Overall status: {}".format(detect_status.group(1)))
    else:
        print("Detect run complete. Status could not be found. Please check logs for potential errors.")

    if bom_location:
        print("BOM location: {}".format(bom_location.group(1)))


@atexit.register
def cleanup():
    global binpack
    try:
        if binpack is not None:
            os.remove(binpack)  # clean up binary zip archive
    except:
        pass


def run():

    global c

    if os.environ.get('BLACKDUCK_URL') != "" and args.url is None:
        args.url = os.environ.get('BLACKDUCK_URL')
    if os.environ.get('BLACKDUCK_API_TOKEN') != "" and args.api_token is None:
        args.api_token = os.environ.get('BLACKDUCK_API_TOKEN')
    if args.sensitivity is None:
        args.sensitivity = 3
    else:
        args.sensitivity = int(args.sensitivity)
    if args.focus is None:
        args.focus = "b"
    if args.no_scan is None:
        args.no_scan = False
    if args.hub_project is None:
        args.hub_project = None
    if args.hub_version is None:
        args.hub_version = None
    if args.trust_cert is None:
        args.trust_cert = "n"
    if args.binary is None:
        args.binary = False

    if args.scanfolder == "" or args.interactive or args.url is None or args.api_token is None:
        args.scanfolder, args.url, args.api_token, args.sensitivity, args.focus, args.no_scan, \
        args.hub_project, args.hub_version, args.trust_cert \
            = interactive(args.scanfolder, args.url, args.api_token, args.sensitivity, args.focus, args.no_scan,
                          args.hub_project, args.hub_version, args.trust_cert)

    if args.scanfolder == "" or args.url is None or args.api_token is None:
        print("Black Duck server URL and API token are required\nExiting")
        sys.exit(1)

    with open(os.path.join(args.scanfolder, 'detect_wizard_input.log'), "w+") as input_log_file:
        input_log_file.write("Scan Dir: {}\n".format(args.scanfolder))
        input_log_file.write("Sensitivity: {}\n".format(args.sensitivity))
        input_log_file.write("Focus: {}\n".format(args.focus))
        input_log_file.write("Trust cert: {}\n".format(str(args.trust_cert)))
        input_log_file.write("\nScan Folder File Tree --\n")
        for line in file_tree_string(args.scanfolder, 6):
            log_file_size = b_to_gb(os.fstat(input_log_file.fileno()).st_size)
            if log_file_size <= 1:
                input_log_file.write(line + "\n")
                input_log_file.flush()
            else:
                input_log_file.write("----> INPUT LOG FILE TRUNCATED FOR REACHING 1 GB. <----\n")
                break

    conffile = os.path.join(args.scanfolder, "application-project.yml")
    backup = backup_file(conffile, "project config")
    c = Configuration(conffile, [PropertyGroup('detect', 'DETECT COMMAND TO RUN'),
                                 PropertyGroup('reqd', 'MINIMUM REQUIRED OPTIONS'),
                                 PropertyGroup('scan', 'OPTIONS TO IMPROVE SCAN COVERAGE'),
                                 PropertyGroup('size', 'OPTIONS TO REDUCE SIGNATURE SCAN SIZE'),
                                 PropertyGroup('dep', 'OPTIONS TO CONFIGURE DEPENDENCY SCAN'),
                                 PropertyGroup('lic', 'OPTIONS TO IMPROVE LICENSE COMPLIANCE ANALYSIS'),
                                 PropertyGroup('proj', 'PROJECT OPTIONS',
                                               defaults="--detect.project.name=PROJECT_NAME\n" + \
                                                        "--detect.project.version.name=VERSION_NAME\n" + \
                                                        "    (OPTIONAL Specify project and version names)\n" + \
                                                        "--detect.project.version.update=true\n" + \
                                                        "    (OPTIONAL Update project and version parameters below for existing projects)\n" + \
                                                        "--detect.project.tier=X\n" + \
                                                        "    (OPTIONAL Define project tier numeric for new project)\n" + \
                                                        "--detect.project.version.phase=ARCHIVED/DEPRECATED/DEVELOPMENT/PLANNING/PRERELEASE/RELEASED\n" + \
                                                        "    (OPTIONAL Specify project phase for new project - default DEVELOPMENT)\n" + \
                                                        "--detect.project.version.distribution=EXTERNAL/SAAS/INTERNAL/OPENSOURCE\n" + \
                                                        "    (OPTIONAL Specify version distribution for new project - default EXTERNAL)\n" + \
                                                        "--detect.project.user.groups='GROUP1,GROUP2'\n" + \
                                                        "    (OPTIONAL Define group access for project for new project)\n"),
                                 PropertyGroup('rep', 'REPORTING OPTIONS',
                                               defaults="--detect.wait.for.results=true\n" + \
                                                        "    (OPTIONAL Wait for server-side analysis to complete - useful for script execution after scan)\n" + \
                                                        "--detect.cleanup=false\n" + \
                                                        "    (OPTIONAL Retain scan results in $HOME/blackduck folder)\n" + \
                                                        "--detect.policy.check.fail.on.severities='ALL,NONE,UNSPECIFIED,TRIVIAL,MINOR,MAJOR,CRITICAL,BLOCKER'\n" + \
                                                        "    (OPTIONAL Comma-separated list of policy violation severities that will cause Detect to return fail code\n" + \
                                                        "--detect.notices.report=true\n" + \
                                                        "    (OPTIONAL Generate Notices Report in text form in project directory)\n" + \
                                                        "--detect.notices.report.path=NOTICES_PATH\n" + \
                                                        "    (OPTIONAL The output directory for notices report. Default is the project directory)\n" + \
                                                        "--detect.risk.report.pdf=true\n" + \
                                                        "    (OPTIONAL Black Duck risk report in PDF form will be created in project directory)\n" + \
                                                        "--detect.risk.report.pdf.path=PDF_PATH\n" + \
                                                        "    (OPTIONAL Output directory for risk report in PDF. Default is the project directory.\n"
                                                        "--detect.report.timeout=XXX\n" + \
                                                        "    (OPTIONAL Amount of time in seconds Detect will wait for scans to finish and to generate reports (default 300).\n" + \
                                                        "    300 seconds may be sufficient, but very large scans can take up to 20 minutes (1200 seconds) or longer)\n"),
                                 PropertyGroup('docker', 'DOCKER SCANNING')])

    c.str_add('reqd', "--blackduck.url={}\n".format(args.url), should_update=True)
    c.str_add('reqd', "--blackduck.api.token={}\n".format(args.api_token), should_update=True)
    if args.trust_cert:
        c.str_add('reqd', "--blackduck.trust.cert=true\n")

    if not os.path.isdir(args.scanfolder):
        print("Scan location '{}' does not exist\nExiting".format(args.scanfolder))
        sys.exit(1)

    rep = ""
    c.str_add('reqd', "--detect.source.path='{}'".format(os.path.abspath(args.scanfolder)), should_update=True)
    cli_msgs_dict['reqd'] += "--detect.source.path='{}'\n".format(os.path.abspath(args.scanfolder))

    print("\nDETECT WIZARD v{}\n".format(advisor_version))

    print("PROCESSING:")

    if os.path.isabs(args.scanfolder):
        print("Working on project folder '{}'\n".format(args.scanfolder))
    else:
        print("Working on project folder '{}' (Absolute path '{}')\n".format(args.scanfolder,
                                                                             os.path.abspath(args.scanfolder)))

    print("- Reading hierarchy          ..... ", end="", flush=True)
    process_dir(args.scanfolder, 0, False)
    print("Done")

    # if args.report:
    #    if os.path.exists(args.report):
    #        backup = backup_file(args.report, "report")
    #    print("Report file '{}' already existed - backed up to {}".format(args.report, backup))
    #
    #    try:
    #        f = open(args.report, "a")
    #    except Exception as e:
    #        print('ERROR: Unable to create output report file \n' + str(e))
    #        sys.exit(3)
    # else:
    f = None

    #     if not args.docker_only:
    #         detector_process(args.scanfolder, f)
    detector_process(args.scanfolder, f)

    # if not args.detector_only and not args.docker_only:
    #     if not args.docker_only:
    #         use_json_splitter = signature_process(args.scanfolder, f)
    use_json_splitter = signature_process(args.scanfolder, f)

    print_summary(False, f)

    check_prereqs()

    #     if args.docker or args.docker_only:
    #         check_docker_prereqs()
    #     if args.docker_only:
    #         c.str_add('reqd', "--detect.tools=DOCKER")
    #         cli_msgs_dict['reqd'] += "--detect.tools=DOCKER\n"

    # output_recs(args.critical_only, f)
    output_recs(True, f)

    # output_cli(args.critical_only, args.report, f)
    # output_cli(False, args.report, f)

    # if args.output_config:
    #conffile = os.path.join(args.scanfolder, "application-project.yml")


    output_config(conffile, c)
    if not args.no_scan:
        # print out information on what the sensitivity setting is doing
        generate_detect_config(conffile)
        config_file = conffile.replace(" ", "\ ")
        print(Actionable.wl.make_table(args.sensitivity))
        run_detect(conffile)

    if args.bdignore:
        create_bdignores()


if __name__ == "__main__":
    run()
