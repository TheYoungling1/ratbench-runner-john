#!/usr/bin/env python3
"""
Tool command parser.

Parses tool commands defined in ToolKit.
Similar to build_agent/utils/parser/parse_command.py.
"""

import re
from typing import Dict, Optional, Union


def match_construct_test(command: str) -> Union[Dict, int]:
    """
    Match the construct-test command.

    Format: construct-test [--repo REPO_PATH] [--mode MODE]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    # Normalize command
    command = command.strip()

    # Base match
    pattern = r"^\s*construct-test\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else ""

    # Parse args
    args = {
        "tool": "construct_test",
        "repo": "/repo",  # Default
        "mode": "llm",  # Default
    }

    # Extract --repo
    repo_match = re.search(r"--repo\s+([^\s]+)", args_str)
    if repo_match:
        args["repo"] = repo_match.group(1)

    # Extract --mode
    mode_match = re.search(r"--mode\s+([^\s]+)", args_str)
    if mode_match:
        args["mode"] = mode_match.group(1)

    return args


def match_run_test(command: str) -> Union[Dict, int]:
    """
    Match the run-test command.

    Format: run-test [--repo REPO_PATH] [--max N] [--type TYPE] [--list]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    # Normalize command
    command = command.strip()

    # Base match
    pattern = r"^\s*run-test\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else ""

    # Parse args
    args = {
        "tool": "run_test",
        "repo": "/repo",  # Default
        "max": 0,  # Default: run all tests
        "type": None,  # Default: all types
        "list": False,  # Default: do not list-only
    }

    # Extract --repo
    repo_match = re.search(r"--repo\s+([^\s]+)", args_str)
    if repo_match:
        args["repo"] = repo_match.group(1)

    # Extract --max
    max_match = re.search(r"--max\s+(\d+)", args_str)
    if max_match:
        args["max"] = int(max_match.group(1))

    # Extract --type
    type_match = re.search(r"--type\s+([^\s]+)", args_str)
    if type_match:
        args["type"] = type_match.group(1)

    # Check --list
    if re.search(r"--list|-l\b", args_str):
        args["list"] = True

    return args


def match_read_file(command: str) -> Union[Dict, int]:
    """
    Match the read-file command.

    Format: read-file FILE_PATH [--question QUESTION] [--no-llm]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*read-file\s+(.+)$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1)

    args = {
        "tool": "read_file",
        "file_path": None,
        "question": None,
        "no_llm": False,
    }

    # Extract --question (supports quotes)
    question_match = re.search(r'--question\s+(["\'])(.*?)\1', args_str)
    if not question_match:
        question_match = re.search(r"--question\s+([^\s]+)", args_str)

    if question_match:
        args["question"] = (
            question_match.group(2)
            if question_match.lastindex > 1
            else question_match.group(1)
        )
        # Remove the question segment from args_str
        args_str = re.sub(r'--question\s+(["\'].*?["\']|[^\s]+)', "", args_str).strip()

    # Check --no-llm
    if re.search(r"--no-llm", args_str, re.IGNORECASE):
        args["no_llm"] = True
        args_str = re.sub(r"--no-llm", "", args_str, re.IGNORECASE).strip()

    # Remaining content is file path
    if args_str:
        args["file_path"] = args_str.strip()

    if not args["file_path"]:
        return -1  # File path is required
    print(args)
    return args


def match_view_outline(command: str) -> Union[Dict, int]:
    """
    Match the view-outline command.

    Format: view-outline PATH [-r] [--no-line-numbers] [-e EXTENSIONS]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*view-outline\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else "."

    args = {
        "tool": "view_outline",
        "path": ".",
        "recursive": False,
        "no_line_numbers": False,
        "extensions": None,
    }

    # Check -r / --recursive
    if re.search(r"-r\b|--recursive", args_str, re.IGNORECASE):
        args["recursive"] = True
        args_str = re.sub(r"-r\b|--recursive", "", args_str, re.IGNORECASE).strip()

    # Check --no-line-numbers
    if re.search(r"--no-line-numbers", args_str, re.IGNORECASE):
        args["no_line_numbers"] = True
        args_str = re.sub(r"--no-line-numbers", "", args_str, re.IGNORECASE).strip()

    # Extract -e / --extensions
    ext_match = re.search(
        r"(?:-e|--extensions)\s+((?:[^\s]+\s*)+)", args_str, re.IGNORECASE
    )
    if ext_match:
        # Extract extensions
        extensions_str = ext_match.group(1).strip()
        args["extensions"] = extensions_str.split()
        args_str = re.sub(
            r"(?:-e|--extensions)\s+(?:[^\s]+\s*)+", "", args_str, re.IGNORECASE
        ).strip()

    # Remaining content is path
    if args_str:
        args["path"] = args_str.strip()

    return args


def match_search_repo(command: str) -> Union[Dict, int]:
    """
    Match the search-repo command.

    Format: search-repo --query QUERY [--repo PATH] [--mode MODE] [--max N]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*search-repo\s+(.+)$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1)

    args = {
        "tool": "search_repo",
        "query": None,
        "repo": "/repo",
        "mode": "llm",
        "max": 10,
    }

    # Extract --query (supports quotes)
    query_match = re.search(r'(?:--query|-q)\s+(["\'])(.*?)\1', args_str, re.IGNORECASE)
    if not query_match:
        query_match = re.search(r"(?:--query|-q)\s+([^\s-]+)", args_str, re.IGNORECASE)

    if query_match:
        args["query"] = (
            query_match.group(2) if query_match.lastindex > 1 else query_match.group(1)
        )
    else:
        return -1  # query is required

    # Extract --repo
    repo_match = re.search(r"--repo\s+([^\s]+)", args_str, re.IGNORECASE)
    if repo_match:
        args["repo"] = repo_match.group(1)

    # Extract --mode
    mode_match = re.search(r"--mode\s+([^\s]+)", args_str, re.IGNORECASE)
    if mode_match:
        args["mode"] = mode_match.group(1)

    # Extract --max
    max_match = re.search(r"--max\s+(\d+)", args_str, re.IGNORECASE)
    if max_match:
        args["max"] = int(max_match.group(1))

    return args


def match_retrieve_image(command: str) -> Union[Dict, int]:
    """
    Match the retrieve-image command.

    Format: retrieve-image [--repo PATH] [--max N] [--llm]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*retrieve-image\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else ""

    args = {
        "tool": "retrieve_image",
        "repo": "/repo",
        "max": 10,
        "llm": False,
    }

    # Extract --repo
    repo_match = re.search(r"--repo\s+([^\s]+)", args_str, re.IGNORECASE)
    if repo_match:
        args["repo"] = repo_match.group(1)

    # Extract --max
    max_match = re.search(r"--max\s+(\d+)", args_str, re.IGNORECASE)
    if max_match:
        args["max"] = int(max_match.group(1))

    # Check --llm
    if re.search(r"--llm", args_str, re.IGNORECASE):
        args["llm"] = True

    return args


def match_retrieve_issue(command: str) -> Union[Dict, int]:
    """
    Match the retrieve-issue command.

    Format: retrieve-issue ERROR_MESSAGE

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*retrieve-issue\s+(.+)$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    error_msg = match.group(1).strip()

    # Strip quotes (if any)
    if (error_msg.startswith('"') and error_msg.endswith('"')) or (
        error_msg.startswith("'") and error_msg.endswith("'")
    ):
        error_msg = error_msg[1:-1]

    args = {
        "tool": "retrieve_issue",
        "error_message": error_msg,
    }

    return args


def match_retrieve_trajectory(command: str) -> Union[Dict, int]:
    """
    Match the retrieve-trajectory command.

    Format: retrieve-trajectory

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*retrieve-trajectory\s*$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args = {
        "tool": "retrieve_trajectory",
    }

    return args


def match_search_web(command: str) -> Union[Dict, int]:
    """
    Match the search-web command.

    Format: search-web QUERY

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*search-web\s+(.+)$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    query = match.group(1).strip()

    # Strip quotes (if any)
    if (query.startswith('"') and query.endswith('"')) or (
        query.startswith("'") and query.endswith("'")
    ):
        query = query[1:-1]

    args = {
        "tool": "search_web",
        "query": query,
    }

    return args


def match_ls_structure(command: str) -> Union[Dict, int]:
    """
    Match the ls-structure command.

    Format: ls-structure [--repo PATH] [--depth N]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*ls-structure\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else ""

    args = {
        "tool": "ls_structure",
        "repo": "/repo",
        "depth": 5,
    }

    # Extract --repo
    repo_match = re.search(r"--repo\s+([^\s]+)", args_str, re.IGNORECASE)
    if repo_match:
        args["repo"] = repo_match.group(1)

    # Extract --depth
    depth_match = re.search(r"--depth\s+(\d+)", args_str, re.IGNORECASE)
    if depth_match:
        args["depth"] = int(depth_match.group(1))

    return args


def match_detect_environment(command: str) -> Union[Dict, int]:
    """
    Match the detect-environment command.

    Format: detect-environment [--format FORMAT] [--save PATH]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*detect-environment\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else ""

    args = {
        "tool": "detect_environment",
        "format": "text",
        "save": None,
    }

    # Extract --format
    format_match = re.search(r"--format\s+([^\s]+)", args_str, re.IGNORECASE)
    if format_match:
        args["format"] = format_match.group(1)

    # Extract --save
    save_match = re.search(r"--save\s+([^\s]+)", args_str, re.IGNORECASE)
    if save_match:
        args["save"] = save_match.group(1)

    return args


def match_cicd_config(command: str) -> Union[Dict, int]:
    """
    Match the cicd-config command.

    Format: cicd-config [--repo PATH] [--workflow FILE] [--execute] [--output PATH] [--llm]

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*cicd-config\s*(.*)?$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args_str = match.group(1) if match.group(1) else ""

    args = {
        "tool": "cicd_config",
        "repo": "/repo",
        "workflow": None,
        "execute": False,
        "output": None,
        "llm": False,
    }

    # Extract --repo
    repo_match = re.search(r"--repo\s+([^\s]+)", args_str, re.IGNORECASE)
    if repo_match:
        args["repo"] = repo_match.group(1)

    # Extract --workflow
    workflow_match = re.search(r"--workflow\s+([^\s]+)", args_str, re.IGNORECASE)
    if workflow_match:
        args["workflow"] = workflow_match.group(1)

    # Extract --output
    output_match = re.search(r"--output\s+([^\s]+)", args_str, re.IGNORECASE)
    if output_match:
        args["output"] = output_match.group(1)

    # Check --execute
    if re.search(r"--execute", args_str, re.IGNORECASE):
        args["execute"] = True

    # Check --llm
    if re.search(r"--llm", args_str, re.IGNORECASE):
        args["llm"] = True

    return args


def match_stop(command: str) -> Union[Dict, int]:
    """
    Match the stop command.

    Format: stop

    Returns:
        Dict: Parsed args on success
        -1: Parse failure
    """
    command = command.strip()

    pattern = r"^\s*stop\s*$"
    match = re.match(pattern, command, re.IGNORECASE)

    if not match:
        return -1

    args = {
        "tool": "stop",
    }

    return args


def parse_tool_command(command: str) -> Union[Dict, int]:
    """
    Unified entrypoint for parsing tool commands.

    Args:
        command: Command string to parse

    Returns:
        Dict: Parsed args (includes the 'tool' field)
        -1: Parse failure

    Examples:
        >>> parse_tool_command("construct-test --repo /path --mode llm")
        {'tool': 'construct_test', 'repo': '/path', 'mode': 'llm'}

        >>> parse_tool_command("search-repo --query 'user auth' --max 5")
        {'tool': 'search_repo', 'query': 'user auth', 'repo': '/repo', 'mode': 'llm', 'max': 5}
    """
    # Try matchers in order
    matchers = [
        match_construct_test,
        match_run_test,
        match_read_file,
        match_view_outline,
        match_search_repo,
        match_retrieve_image,
        match_retrieve_issue,
        match_retrieve_trajectory,
        match_search_web,
        match_ls_structure,
        match_detect_environment,
        match_cicd_config,
        match_stop,
    ]

    for matcher in matchers:
        result = matcher(command)
        if result != -1:
            return result

    # All matchers failed
    return -1


if __name__ == "__main__":
    # Test cases
    test_commands = [
        "construct-test --repo /path/to/repo --mode llm",
        "construct-test --repo /repo",
        "construct-test",
        "read-file /path/to/file.py",
        "read-file /path/to/file.py --question 'what does this do?'",
        "read-file /path/to/file.py --no-llm",
        "view-outline .",
        "view-outline /path/to/dir -r",
        "view-outline /path/to/dir -r -e .py .js",
        "search-repo --query 'user authentication'",
        "search-repo --query 'API endpoint' --mode llm --max 5",
        "search-repo -q 'database' --repo /custom/path",
        "retrieve-image --repo /path --llm",
        "retrieve-image --max 15",
        "retrieve-image",
        "retrieve-issue 'ModuleNotFoundError: No module named torch'",
        "retrieve-trajectory",
        "search-web 'how to install PyTorch'",
        "search-web Python numpy error",
        "ls-structure --repo /path/to/repo --depth 3",
        "ls-structure --repo /repo",
        "ls-structure",
        "stop",
        "invalid command",
    ]

    print("=" * 80)
    print("Tool command parser tests")
    print("=" * 80)

    for cmd in test_commands:
        print(f"\nCommand: {cmd}")
        result = parse_tool_command(cmd)
        if result != -1:
            print("✓ Parsed:")
            for key, value in result.items():
                print(f"  {key}: {value}")
        else:
            print("✗ Parse failed")
        print("-" * 80)
