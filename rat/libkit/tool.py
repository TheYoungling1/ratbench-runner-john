from enum import Enum


SAFE_CMD = [
    "cd",
    "ls",
    "cat",
    "echo",
    "pwd",
    "whoami",
    "who",
    "date",
    "cal",
    "df",
    "du",
    "free",
    "uname",
    "uptime",
    "w",
    "ps",
    "pgrep",
    "top",
    "htop",
    "vmstat",
    "iostat",
    "dmesg",
    "tail",
    "head",
    "more",
    "less",
    "grep",
    "find",
    "locate",
    "whereis",
    "which",
    "file",
    "stat",
    "cmp",
    "diff",
    "md5sum",
    "sha256sum",
    "gzip",
    "gunzip",
    "bzip2",
    "bunzip2",
    "xz",
    "unxz",
    "sort",
    "uniq",
    "wc",
    "tr",
    "cut",
    "paste",
    "tee",
    "awk",
    "sed",
    "env",
    "printenv",
    "hostname",
    "ping",
    "traceroute",
    "ssh",
    "journalctl",
    "lsblk",
    "blkid",
    "uptime",
    "lscpu",
    "lsusb",
    "lspci",
    "lsmod",
    "dmidecode",
    "ip",
    "ifconfig",
    "netstat",
    "ss",
    "route",
    "nmap",
    "strace",
    "ltrace",
    "time",
    "nice",
    "renice",
    "killall",
    "printf",
    "pip",
]


TIME_OUT_LABEL = " seconds. Partial output:"
BASH_FENCE = ["```bash", "```"]
SUMMARY_FENCE = ["```summary", "```"]


class ToolKit(Enum):
    construct_test = {
        "command": "construct-test [--repo REPO_PATH]",
        "description": "Create test cases and detect the repository entry point. Usage: construct-test --repo /path. Args: --repo (repo path, required). Behavior: scan the repo, identify the entry point, extract run commands from README, and locate tests or entry modules. Output: entry point, run commands, and test info.",
    }
    read_file = {
        "command": "read-file FILE_PATH [--question Q]",
        "description": "Read a file with optional question-guided analysis. Compared to cat, read-file can do deeper understanding but may be slower. Usage: read-file file.py or read-file file.py --question 'What does this do?'. Args: FILE_PATH (required), --question (optional), --no-llm (skip LLM and just print content). Output: analysis report, key components, dependencies.",
    }
    view_outline = {
        "command": "view-outline PATH [-r] [--no-line-numbers]",
        "description": "Show a code outline (classes and function signatures) for a file or directory. Usage: view-outline dir -r. Args: PATH (file/dir, default current), -r (recursive), --no-line-numbers. Output: outline (class/function names and line numbers).",
    }
    search_repo = {
        "command": "search-repo --query QUERY [--repo PATH] [--mode MODE] [--max N]",
        "description": "Search repository code snippets globally (optionally with LLM). Usage: search-repo --query 'authentication' or search-repo --query 'API' --mode llm --max 5. Args: --query (required), --repo (default /repo), --mode (detailed/simple/llm, default llm), --max (default 10). Output: code snippets with paths/line numbers and brief notes.",
    }
    retrieve_image = {
        "command": "retrieve-image [--repo PATH] [--max N]",
        "description": "Retrieve relevant Docker images from Docker Hub. Args: --repo (default /repo), --max (default 10). Behavior: analyze repo files (requirements.txt/package.json), infer language/framework/deps, search Docker Hub, recommend tags. Output: image list, pull commands, tags.",
    }
    retrieve_issue = {
        "command": "retrieve-issue ERROR_MSG",
        "description": "Retrieve relevant issues/solutions from an issue database. Usage: retrieve-issue 'ModuleNotFoundError: torch'. Args: ERROR_MSG (required). Output: related issues and suggested fixes.",
    }
    # retrieve_trajectory = {
    #     "command": "retrieve-trajectory",
    #     "description": "Retrieve setup ideas from past trajectories. Usage: retrieve-trajectory. Output: suggested setup steps and successful cases.",
    # }
    search_web = {
        "command": "search-web QUERY",
        "description": "Search the web (StackOverflow, GitHub, docs) for errors or how-tos. Usage: search-web 'How to install PyTorch' or search-web 'Python numpy error'. Args: QUERY (required). Output: results, links, and summaries.",
    }
    ls_structure = {
        "command": "ls-structure [--repo PATH] [--depth N]",
        "description": "Show the repository directory tree. Usage: ls-structure --repo /path or ls-structure --depth 3. Args: --repo (default /repo), --depth (default 5). Output: directory tree and important files (README/setup.py/Dockerfile, etc.).",
    }
    edit_file = {
        "command": "edit-file FILE_PATH --mode MODE [OPTIONS]",
        "description": "Edit file content. Usage: edit-file file.py --mode llm --instruction 'Add docstring' or edit-file config.json --mode search --search 'old' --replace 'new'. Args: FILE_PATH (required), --mode (replace/insert/delete/search/llm/append). Behavior: create .bak backups, support regex replace and LLM-assisted edits. Output: edit result and backup file path.",
    }
    run_test = {
        "command": "run-test [--repo REPO_PATH] [--type TYPE] [--list]",
        "description": "Run tests based on construct_test_result.json. Usage: run-test or run-test --repo /repo. Args: --repo (default /repo), --type (test/run/collect), --list (list only). Output: execution results and pass/fail stats.",
    }
    run_pytest = {
        "command": "run-pytest",
        "description": "Automatically run all pytest tests in /repo. Usage: run-pytest. Output: pass/fail counts and categorized errors (ModuleNotFoundError/ImportError/etc.). Saves results to /repo/logs/run_pytest_results.json.",
    }
    run_pytest_collect = {
        "command": "run-pytest-collect",
        "description": "Collect pytest tests in /repo without executing them. Usage: run-pytest-collect. Behavior: run python -m pytest --co -q, count collected tests, detect import errors. Saves results to /repo/logs/run_pytest_collect_results.json.",
    }
    detect_environment = {
        "command": "detect-environment [--format FORMAT] [--save PATH]",
        "description": "Detect basic environment information. Usage: detect-environment or detect-environment --format json --save env.json. Args: --format (text/json), --save (path). Output: environment report (GPU/system/network/tools/mirrors).",
    }
    cicd_config = {
        "command": "cicd-config [--repo PATH] [--workflow FILE] [--execute] [--output PATH] [--llm]",
        "description": "Configure environment based on GitHub CI/CD workflows (.github/workflows/*.yml). Usage: cicd-config --repo /repo or cicd-config --workflow test.yml --execute. Args: --repo (default /repo), --workflow (file), --execute, --output, --llm. Output: setup script, command list, optional execution results.",
    }
    stop = {
        "command": "stop",
        "description": "Stop environment setup. Usage: stop. Behavior: stop the flow and save state when finished or interrupted.",
    }

    change_python_version = {
        "command": "change-python-version python_version",
        "description": "Switch the Python version inside the Docker container. Note: switching will lose all previously installed packages and environment state. Provide the version as digits and dots (e.g. 3.10) without quotes.",
    }

    change_java_version = {
        "command": "change-java-version java_version",
        "description": "Switch the Java version inside the Docker container. Note: switching will lose all previously installed packages and environment state. Provide the version as digits (e.g. 11, 17, 21) without quotes.",
    }
    # change_base_image = {
    #     "command": 'change_base_image base_image',
    #     "description": "Switching the base image in the Docker container will forgo any installations made prior to the switch. The base image does not necessarily have to follow the format 'python:<Python version>'. Preferably, specify it in the form of 'base_image_name:tag', such as 'pytorch/pytorch:1.10.0-cuda11.1-cudnn8-runtime'. If no tag is provided, it defaults to 'latest'. No any quotation marks are needed."
    # }
    # clear_configuration = {
    #     "command": 'clear_configuration',
    #     "description": "Reset all the configuration to the initial setting of python:3.10."
    # }


def update_trajectory(trajectory, messages, agent: str):
    # Add an agent field to each message
    for message in messages:
        message["agent"] = agent.lower()
        trajectory.append(message)
