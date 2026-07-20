import re
from libkit.tool import SAFE_CMD


def split_cmd_statements(cmd):
    # Remove backslash + newline continuations
    cmd = re.sub(r"\\\s*\n", "", cmd)

    # Detect and protect heredoc blocks
    heredoc_pattern = r"<<\s*['\"]?(\w+)['\"]?\s*\n(.*?)\n\1"
    heredocs = []

    def save_heredoc(match):
        placeholder = f"__HEREDOC_{len(heredocs)}__"
        heredocs.append(match.group(0))
        return placeholder

    cmd = re.sub(heredoc_pattern, save_heredoc, cmd, flags=re.DOTALL)

    # Replace normal newlines with a single space
    cmd = re.sub(r"\n", " ", cmd)

    # Restore heredoc blocks
    for i, heredoc in enumerate(heredocs):
        cmd = cmd.replace(f"__HEREDOC_{i}__", heredoc)

    # Split by && and strip whitespace
    statements = re.split(r"\s*&&\s*", cmd)

    return [statement.strip() for statement in statements]


def extract_commands(text):
    BASH_FENCE = ("```bash", "```")
    pattern = rf"{re.escape(BASH_FENCE[0])}([\s\S]*?){re.escape(BASH_FENCE[1])}"
    matches = re.findall(pattern, text)
    return matches


def parse_commands(inner_commands):
    res_cmd = list()
    for inner_command in inner_commands:
        command = inner_command["command"]
        dir = inner_command["dir"] if "dir" in inner_command else "/"
        returncode = inner_command["returncode"]
        action_name = command.split(" ")[0].strip()
        if str(returncode).strip() != "0":
            continue
        if action_name in ["pipdeptree"]:
            continue
        if action_name in SAFE_CMD and ">" not in command:
            continue
        if (
            command == "python /home/tools/runtest.py"
            or command == "python /home/tools/poetryruntest.py"
            or command == "python /home/tools/runpipreqs.py"
            or command == "python /home/tools/generate_diff.py"
            or command == "$pwd$"
            or command == "$pip list --format json$"
        ):
            continue
        if action_name == "change-python-version":
            res_cmd = list()
            continue
        if action_name == "change-java-version":
            res_cmd = list()
            continue
        if action_name == "clear_configuration":
            res_cmd = list()
            continue
        if dir != "/":
            res_cmd.append(f"cd {dir} && {command}")
        else:
            res_cmd.append(command)
    return res_cmd
