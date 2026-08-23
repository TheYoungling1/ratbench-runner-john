import subprocess

from src.envstate.extractor import EXTRACTOR_COMMANDS, run_extractor, SYSTEM_TOOL_PROBES


def test_system_tools_command_present_and_curated():
    assert "system_tools" in EXTRACTOR_COMMANDS
    # the failure-artifact tools we care about must be probed
    for t in ("gcc", "pg_config", "pkg-config", "make", "cmake", "mysql_config"):
        assert t in SYSTEM_TOOL_PROBES


def test_system_tools_parsed_as_present_subset():
    # fake exec returns only gcc + pg_config present (one name per line)
    table = {EXTRACTOR_COMMANDS["system_tools"]: (0, "gcc\npg_config\n")}
    res = run_extractor(lambda cmd: table.get(cmd, (1, "")), fields=("system_tools",))
    assert res.fields["system_tools"] == "gcc\npg_config"


def test_system_tools_command_exits_zero_when_all_tools_absent():
    # Regression (found in the burr arm-v1 e2e): the for-loop's exit status is the
    # last iteration's `command -v <tool>` rc. When the last probed tool is absent
    # (icu-config commonly is), the whole command exits non-zero, and run_extractor's
    # `rc == 0` gate then silently DROPS the field — discarding every tool it printed.
    # The command must therefore force a zero exit. Reproduce the worst case with an
    # emptied PATH so NO probed tool resolves (POSIX `command`/`echo`/`true` are
    # builtins, so the loop still runs). Without the fix this returns 127.
    cmd = EXTRACTOR_COMMANDS["system_tools"]
    result = subprocess.run(
        ["/bin/sh", "-c", cmd], env={"PATH": ""}, capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""  # nothing resolvable on PATH, yet rc stays 0
