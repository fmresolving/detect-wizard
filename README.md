# Synopsys Detect Wizard (detect-wizard)

Black Duck scanning wizard to pre-scan folders, determine optimal scan configuration and call Synopsys Detect to scan

# INTRODUCTION

This script is provided under an OSS license (specified in the LICENSE file) to assist users when scanning projects in Black Duck for OSS analysis.

It does not represent any extension of licensed functionality of Synopsys software itself and is provided as-is, without warranty or liability.

# OVERVIEW

The Detect Wizard is intended to provide a simple, comprehensive method for scanning new projects with Black Duck, checking prerequisites,
pre-scanning the specified folder to identify the contents and using supplied preferences (license, security analysis or both) and a 
sensitivity value to determine the optimal Synopsys Detect options to use, to create a yml project file (for running Synopsys Detect later) 
and optionally invoking Synopsys Detect to perform the scan.

# DETAILED DESCRIPTION

The Detect Wizard uses several inputs by default including:
- Folder to scan (required)
- Black Duck server URL (required)
- Black Duck API token (required)
- Scan sensitivity value (determines how detailed the scan will be - default 3)
- Scan focus (license, security or both - default both)
- Black Duck Project name (default none)
- Black Duck Project version (default none)

These values can be specified as arguments, or will be requested in an interactive mode if not supplied. Server URL and API key will also be picked up 
from standard Detect environment variables if set in the environment.

The scan sensitivity value specifies the analysis scope ranging from 1 (most accurate Bill of Materials with minimal false positives – but with the potential 
to miss some OSS components) to 5 (most comprehensive analysis to identify as many OSS components as possible but with the potential for many false positives).

Detect Wizard supports similar techniques to Synopsys Detect to determine predefined Detect scan parameters (including environment variables and using 
existing .yml project configuration files). Detect Wizard will check the prerequisites to run Synopsys Detect (including the correct version of Java) and then scan the project location for files 
and archives, calculate the total scan size, check for project (package manager) files and package managers themselves and will also detect large duplicate 
files and folders.

It will expand .zip and .jar files automatically, processing recursive files (zips within zips etc.). Other archive types (.gz, .tar, .Z etc.) are not 
currently expanded by Detect Wizard (although they will be expanded by Synopsys Detect).

Based on the specified sensitivity and scan type, it will identify Detect options which are relevant to the scanned project and determine suitable settings 
to support the level of scan required.

It will export a .yml file for use in Detect scans later, and will optionally call Detect directly to run the scan.

# PREREQUISITES
Detect Wizard requires Python 3 to be installed.

# INSTALLATION
pip3 install detect-wizard

# DETECT WIZARD USAGE
The Detect Wizard can be invoked with or without parameters.

If the scan folder is not specified or -I/- -interactive is used, then required options will be requested in interactive mode.

The detect_advisor.py script arguments are shown below:

    usage: detect_advisor [-h] [-b] [-i] [-s SENSITIVITY] [-f FOCUS] [-u URL]
                          [-a API_TOKEN] [-n] [—no_write]
                          [--aux_write_dir AUX_WRITE_DIR] [-hp HUB_PROJECT]
                          [-hv HUB_VERSION] [scanfolder]

    Check prerequisites for Detect, scan folders, configure and run Synopsys Detect

    positional arguments:
      scanfolder            Project folder to analyse

    optional arguments:
      -h, --help            show this help message and exit
      -b, --bdignore        Create .bdignore files in sub-folders to exclude folders from scan
      -i, --interactive     Use interactive mode to review/set options
      -s SENSITIVITY, --sensitivity SENSITIVITY
                            Coverage/sensitivity - 1 = dependency scan only & limited FPs, 5 = all
                            scan types including all potential matches
      -f FOCUS, --focus FOCUS
                            Scan focus of License Compliance (l) / Security (s) / Both (b)
      -u URL, --url URL     Black Duck Server URL
      -a API_TOKEN, --api_token API_TOKEN
                            Black Duck Server API Token
      -n, --no_scan         Do not run Detect scan - only create .yml project config file
      --no_write            Do not add files to scan directory.
      --aux_write_dir AUX_WRITE_DIR
                            Directory to write intermediate files (default XXXX)
      -hp HUB_PROJECT, --hub_project HUB_PROJECT
                            Hub Project Name
      -hv HUB_VERSION, --hub_version HUB_VERSION
                            Hub Project Version

If scanfolder is not specified then all required options will be requested interactively (alternatively use -i or --interactive option to run interactive 
mode). Enter q or use CTRL-C to terminate interactive entry and the program. Special characters such as ~ or environment variables such as $HOME are not 
supported in interactive mode. Default values will be identified from the environment variables BLACKDUCK_URL or BLACKDUCK_API_TOKEN if set in the environment.

The scanfolder can be a relative or absolute path.

# EXAMPLE USAGE

The following command will request arguments interactively:

    python3 -m detect-wizard

The interactive questions are shown below (set the environment variables BLACKDUCK_URL and BLACKDUCK_API_TOKEN to pre-populate these values):

    Enter project folder to scan (default current folder ‘/Users/myuser/Desktop'):
    Black Duck Server URL [None]: 
    Black Duck API Token [None]: 
    Scan sensitivity/coverage (1-5) where 1 = dependency scan only, 5 = all scan types including all potential matches [3]: 
    Scan Focus (License Compliance (l) / Security (s) / Both (b)) [b]: 
    Hub Project Name [None]:
    Hub Project Version [None]:
    Run Detect scan (y/n) [y]: 

The following example command specifies the folder to scan and uses default values for other arguments (sensitivity = 3, scan focus = both, run Detect scan = y).
If not specified, then the Black Duck project and version names will be determined by Synopsys Detect. For this command 

    python3 -m detect-wizard /Users/myuser/myproject

# EXPLANATION OF SCAN SENSITIVITY

To be added here.

# SUMMARY INFO OUTPUT
This section includes counts and size analysis for the files and folders beneath the project location.

The Size Outside Archives value in the ALL FILES (Scan Size) row represents the total scan size as calculated by Detect (used for capacity license).

Note that the Archives(exc. Jars) row covers all archive file types but that only .zip files are extracted by detect_advisor (whereas Synopsys Detect 
extracts other types of archives automatically). The final 3 Inside Archives columns indicate items found within .zip archives for the different types 
(except for the Jar row which references .jar/.ear/.war files). The Inside Archives columns for the Archives row itself reports archive files within .zips 
(or nested deeper - zips within zips within zips etc.).

    SUMMARY INFO:
    Total Scan Size = 5,856 MB

                             Num Outside     Size Outside      Num Inside     Size Inside     Size Inside
                                Archives         Archives        Archives        Archives        Archives
                                                            (UNcompressed)    (compressed)
    ====================  ==============   ==============   =============   =============   =============
    Files (exc. Archives)        297,415         4,905 MB         130,126          653 MB          160 MB
    Archives (exc. Jars)              39           951 MB               9            0 MB            0 MB
    ====================  ==============   ==============   =============   =============   =============
    ALL FILES (Scan size)        297,454         5,856 MB         130,135          654 MB          160 MB
    ====================  ==============   ==============   =============   =============   =============
    Folders                       30,435              N/A          10,309             N/A             N/A   
    Ignored Folders                4,169         2,319 MB               0            0 MB            0 MB
    Source Files                 164,240         1,024 MB          53,740          171 MB           34 MB
    JAR Archives                       6             6 MB               0            0 MB            0 MB
    Binary Files                      33            99 MB              10            0 MB            0 MB
    Other Files                  129,476         2,988 MB          75,282          478 MB          124 MB
    Package Mgr Files              3,633            25 MB           1,094            2 MB            0 MB
    OS Package Files                   0             0 MB               0            0 MB            0 MB
    --------------------  --------------   --------------   -------------   -------------   -------------
    Large Files (>5MB)                38           336 MB               1            9 MB            4 MB
    Huge Files (>20MB)                27         1,875 MB               1           35 MB            6 MB
    --------------------  --------------   --------------   -------------   -------------   -------------

    PACKAGE MANAGER CONFIG FILES:
    - In invocation folder:   0
    - In sub-folders:         3633
    - In archives:            0
    - Minimum folder depth:   2
    - Maximum folder depth:   14
    ---------------------------------
    - Total discovered:       3633

    Config files for the following Package Managers found: gradlew, gradle, clang, dotnet, npm, yarn, pod, python, python3, pip

# RECOMMENDATIONS
This section includes a list of critical findings which will cause Detect to fail.

    RECOMMENDATIONS:
    - CRITICAL: Overall scan size (6,520 MB) is too large
        Impact:  Scan will fail
        Action:  Ignore folders or remove large files

# OUTPUT CONFIG FILES
The file application-project.yml will be created in the project folder if it does not already exist. If a copy already exists it will be renamed first. 
The application-project.yml config file can be used to configure Detect using the single --spring.profiles.active=project option.

The -b or --bdignore option will create multiple .bdignore files in sub-folders beneath the project folder if they do not already exist. The .bdignore files 
will be created in parent folders of duplicate folders or those containing only binary files for exclusion. USE WITH CAUTION as it will cause specified folders 
to be permanently ignored by the Signature scan until the .bdignore files are removed.
