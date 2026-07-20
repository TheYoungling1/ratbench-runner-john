"""Tier-1 Fix 1: capture final file content instead of replaying edit commands."""
from src.envstate.ledger import ActionEvent, ActionLedger
from src.envstate.file_capture import (
    DEFAULT_MAX_FILE_BYTES,
    FileStateCapturer,
    build_ordered_recipe_ops,
    extract_edited_file_paths,
    extract_paths_from_command,
    is_probably_binary,
)


def _ev(cmd, rc=0, mc=None, step=1):
    return ActionEvent(
        step=step, task_id=None, cmd=cmd, rc=rc, stdout="", stdout_path=None,
        stderr_path=None, env_revision_before=0, env_revision_after=0,
        mutation_class=mc, container_id="c1", summary="",
    )


def test_extract_paths_sed():
    assert extract_paths_from_command("sed -i 's/a/b/' config.py") == ["config.py"]
    assert extract_paths_from_command("sed -i.bak -e 's/a/b/' /app/x.py") == ["/app/x.py"]


def test_extract_paths_redirect():
    assert extract_paths_from_command("printf 'x' >> pkg/utils.py") == ["pkg/utils.py"]
    assert extract_paths_from_command("echo hi > a.txt") == ["a.txt"]
    assert extract_paths_from_command("cat x 2>/dev/null") == []
    assert extract_paths_from_command("cat foo > /dev/null") == []


def test_extract_paths_python_open():
    cmd = "python -c \"open('/app/pkg/u.py','a').write('x')\""
    assert extract_paths_from_command(cmd) == ["/app/pkg/u.py"]


def test_extract_paths_heredoc_opener_still_captured():
    """A truncated single-line heredoc opener still names the file it wrote."""
    cmd = "cat > /app/pyproject.toml << 'EOF'"
    assert extract_paths_from_command(cmd) == ["/app/pyproject.toml"]


def test_extract_edited_paths_dedupe_and_order():
    led = ActionLedger()
    led.append(_ev("sed -i 's/a/b/' c.py"))
    led.append(_ev("printf 'x' >> d.py"))
    led.append(_ev("sed -i 's/c/d/' c.py"))     # same file again -> deduped
    led.append(_ev("python -m pytest -q"))       # not an edit
    led.append(_ev("printf 'x' >> e.py", rc=1))  # failed -> ignored
    assert extract_edited_file_paths(led) == ["c.py", "d.py"]


def test_capturer_reads_final_content_once():
    led = ActionLedger()
    led.append(_ev("sed -i 's/a/b/' c.py"))
    led.append(_ev("sed -i 's/b/c/' c.py"))
    captured = FileStateCapturer(lambda p: {"c.py": "final\n"}.get(p)).capture(led)
    assert captured == {"c.py": "final\n"}


def test_capturer_skips_deleted_or_unreadable():
    led = ActionLedger()
    led.append(_ev("printf 'x' > gone.py"))
    assert FileStateCapturer(lambda p: None).capture(led) == {}


def test_capturer_skips_binary_and_oversize():
    led = ActionLedger()
    led.append(_ev("printf 'x' > big.txt"))
    led.append(_ev("printf 'x' > bin.dat"))

    def read(p):
        if p == "big.txt":
            return "A" * (DEFAULT_MAX_FILE_BYTES + 10)
        return "\x00\x01binary"

    assert FileStateCapturer(read).capture(led) == {}


def test_is_probably_binary():
    assert is_probably_binary("\x00abc")
    assert is_probably_binary("ok" + "�" * 50)
    assert not is_probably_binary("hello world\n")


# --- HIGH 1: edit -> build ordering (build_ordered_recipe_ops) -------------------


def test_ordered_ops_file_write_precedes_consuming_build_step():
    """The file write must precede an irreducible build step that ran AFTER the edit."""
    led = ActionLedger()
    led.append(_ev("sed -i 's/x/y/' setup.py"))                 # edit
    led.append(_ev("python setup.py build_ext", mc="other"))    # consumes setup.py
    ops = build_ordered_recipe_ops(led, {"setup.py": "FINAL\n"})
    assert ops == [("file", "setup.py", "FINAL\n"), ("run", "python setup.py build_ext")]


def test_ordered_ops_command_before_edit_is_unaffected():
    """A build step that ran BEFORE the first edit keeps its position (not hoisted)."""
    led = ActionLedger()
    led.append(_ev("meson setup build", mc="other"))            # before any edit
    led.append(_ev("sed -i 's/x/y/' conf.py"))                  # edit
    led.append(_ev("ninja -C build", mc="other"))               # after the edit
    ops = build_ordered_recipe_ops(led, {"conf.py": "C\n"})
    assert ops == [
        ("run", "meson setup build"),
        ("file", "conf.py", "C\n"),
        ("run", "ninja -C build"),
    ]


def test_ordered_ops_file_write_at_last_edit_position():
    """A file edited multiple times is written once, at its LAST edit position."""
    led = ActionLedger()
    led.append(_ev("sed -i 's/a/b/' conf.py"))                  # edit 1
    led.append(_ev("echo step", mc="other"))                    # interleaved build
    led.append(_ev("sed -i 's/b/c/' conf.py"))                  # last edit
    led.append(_ev("make", mc="other"))                         # after last edit
    ops = build_ordered_recipe_ops(led, {"conf.py": "Z\n"})
    assert ops == [
        ("run", "echo step"),
        ("file", "conf.py", "Z\n"),
        ("run", "make"),
    ]


def test_ordered_ops_uncaptured_edit_falls_back_to_replay():
    """HIGH 2 safety net: a file we could NOT capture replays its edit command(s)."""
    led = ActionLedger()
    led.append(_ev("sed -i 's/a/b/' kept.py"))                  # captured
    led.append(_ev("sed -i 's/c/d/' missed.py"))                # NOT captured -> replay
    ops = build_ordered_recipe_ops(led, {"kept.py": "K\n"})
    assert ("file", "kept.py", "K\n") in ops
    assert ("run", "sed -i 's/c/d/' missed.py") in ops          # replayed, not dropped
    assert not any(o[0] == "file" and o[1] == "missed.py" for o in ops)


def test_ordered_ops_drops_package_installs():
    led = ActionLedger()
    led.append(_ev("pip install requests", mc="language_package_install"))
    led.append(_ev("apt-get install -y gcc", mc="system_package_install"))
    ops = build_ordered_recipe_ops(led, {})
    assert ops == [("run", "apt-get install -y gcc")]
