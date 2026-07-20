"""Tier-1 mechanism tests (§7.2): synthetic, repo-agnostic ledgers must produce a
Dockerfile that reflects FINAL state with no replay artifacts, no duplicated installs,
exactly one project install, and no malformed RUN.
"""
import base64
import os
import tempfile

from src.envstate.file_capture import FileStateCapturer
from src.envstate.ledger import ActionEvent, ActionLedger
from src.envstate.recipe import build_closure_recipe
from src.envstate.synthesis import build_commands_from_ledger
from src.envstate.world_model import Fact
from src.synthesizer import Synthesizer


def _ev(cmd, rc=0, mc=None, step=1):
    return ActionEvent(
        step=step, task_id=None, cmd=cmd, rc=rc, stdout="", stdout_path=None,
        stderr_path=None, env_revision_before=0, env_revision_after=0,
        mutation_class=mc, container_id="c1", summary="",
    )


def _assemble(ledger, installed, final_files, project_name, project_root=None):
    """Mirror the v1g finalize wiring without docker.

    Uses build_ordered_recipe_ops so irreducible commands and captured file writes are
    INTERLEAVED in trajectory order (Fix 1 ordering), exactly like the agent does.
    """
    from src.envstate.file_capture import build_ordered_recipe_ops

    synth = Synthesizer(base_image="python:3.11-slim", workdir="/app")
    captured = FileStateCapturer(lambda p: final_files.get(p)).capture(ledger)
    ops = build_ordered_recipe_ops(
        ledger, captured, distill=synth._extract_recordable_setup_commands
    )
    for op in ops:
        if op[0] == "run":
            synth.add_build_instruction(op[1])
        else:
            synth.add_file_write_instruction(op[1], op[2])
    for cmd in build_closure_recipe(
        installed, project_name=project_name, ledger=ledger, project_root=project_root
    ):
        synth.add_build_instruction(cmd)
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as tmp:
        path = tmp.name
    try:
        return synth.generate_dockerfile(file_path=path)
    finally:
        os.unlink(path)


def test_same_file_edited_thrice_emits_final_once():
    led = ActionLedger()
    led.append(_ev("sed -i 's/a/b/' conf.py"))
    led.append(_ev("sed -i 's/b/c/' conf.py"))
    led.append(_ev("sed -i 's/c/d/' conf.py"))
    df = _assemble(led, (Fact("requests", "2.31.0"),), {"conf.py": "FINAL=1\n"}, "proj")
    assert "sed -i" not in df                                   # no replayed edits
    assert df.count("base64 -d > 'conf.py'") == 1               # written exactly once
    assert base64.b64encode(b"FINAL=1\n").decode() in df        # FINAL content


def test_install_then_overwrite_single_source():
    led = ActionLedger()
    led.append(_ev("pip install -e .", mc="language_package_install"))
    led.append(_ev("pip install proj", mc="language_package_install"))  # overwrite bug
    df = _assemble(led, (Fact("requests", "2.31.0"), Fact("proj", "0.1")), {}, "proj")
    assert "pip install proj" not in df                         # no replayed install
    assert df.count("pip install -e .") == 1                    # one editable install
    assert "requests==2.31.0" in df                            # pinned closure
    assert "proj==0.1" not in df                                # project excluded from pin


def test_truncated_heredoc_recovers_final_content():
    led = ActionLedger()
    led.append(_ev("cat > /app/pyproject.toml << 'EOF'", mc="file_or_env_change"))
    final = "[project]\nname = \"x\"\nversion = \"0.1\"\n"
    df = _assemble(led, (Fact("x", "0.1"),), {"/app/pyproject.toml": final}, "x")
    assert "<< 'EOF'" not in df                                 # broken opener gone
    assert base64.b64encode(final.encode()).decode() in df      # real content baked


def test_compound_install_and_test_leaves_no_replay():
    led = ActionLedger()
    led.append(_ev("pip install -r requirements.txt && pytest -q", mc="language_package_install"))
    df = _assemble(led, (Fact("flask", "3.0.0"),), {}, "proj")
    assert "requirements.txt" not in df
    assert "pytest" not in df
    assert "flask==3.0.0" in df


def test_no_malformed_run_in_output():
    from src.envstate.dockerfile_validate import find_malformed_runs

    led = ActionLedger()
    led.append(_ev("apt-get install -y libpq-dev", mc="system_package_install"))
    led.append(_ev("sed -i 's/a/b/' conf.py"))
    led.append(_ev("pip install -e .", mc="language_package_install"))
    df = _assemble(
        led,
        (Fact("psycopg2", "2.9.9"), Fact("proj", "0.1")),
        {"conf.py": "X=1\n"},
        "proj",
    )
    assert find_malformed_runs(df) == []


def test_edit_then_build_step_writes_file_before_build():
    """HIGH 1: a build step that consumes an edited file must see the FINAL content —
    the file write must precede that build RUN in the emitted Dockerfile."""
    led = ActionLedger()
    led.append(_ev("sed -i 's/x/y/' setup.py"))                           # edit
    led.append(_ev("python setup.py build_ext --inplace", mc="other"))    # consumes setup.py
    df = _assemble(led, (Fact("requests", "2.31.0"),), {"setup.py": "FINAL\n"}, "proj")
    assert "sed -i" not in df                                             # no replayed edit
    write_idx = df.index("base64 -d > 'setup.py'")
    build_idx = df.index("build_ext")
    assert write_idx < build_idx                                          # file written first


def test_lossless_capture_non_utf8_byte():
    """A mostly-text file with a non-UTF-8 byte is baked byte-exact (not corrupted)."""
    raw = b"line1\nval = \xff\nline3\n"                       # 0xff is not valid UTF-8
    content = raw.decode("utf-8", "surrogateescape")          # how the agent captures it
    synth = Synthesizer(base_image="python:3.11-slim", workdir="/app")
    synth.add_file_write_instruction("conf.cfg", content)
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as tmp:
        path = tmp.name
    try:
        out = synth.generate_dockerfile(file_path=path)
    finally:
        os.unlink(path)
    assert base64.b64encode(raw).decode() in out              # exact original bytes baked


def test_add_file_write_instruction_is_idempotent():
    synth = Synthesizer(base_image="python:3.11-slim", workdir="/app")
    synth.add_file_write_instruction("conf.py", "X=1\n")
    synth.add_file_write_instruction("conf.py", "X=1\n")
    run_lines = [i for i in synth.instructions if "conf.py" in i]
    assert len(run_lines) == 1
