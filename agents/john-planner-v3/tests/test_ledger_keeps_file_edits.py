"""TDD tests for CHANGE D: synthesis must keep rc=0 file-edit commands
whose mutation_class is None (classifier doesn't recognise printf/python-c writes).
"""
from src.envstate.ledger import ActionEvent, ActionLedger
from src.envstate.synthesis import build_commands_from_ledger


def _event(step, cmd, rc, mutation_class=None):
    return ActionEvent(
        step=step,
        task_id=None,
        cmd=cmd,
        rc=rc,
        stdout="",
        stdout_path=None,
        stderr_path=None,
        env_revision_before=0,
        env_revision_after=0,
        mutation_class=mutation_class,
        container_id="c1",
        summary="",
    )


PRINTF_EDIT = "printf 'def f():\n    pass\n' >> pkg/utils.py"
PYTHON_C_EDIT = "python -c \"open('/app/pkg/utils.py','a').write('\\ndef g(): pass')\""


def test_keeps_source_file_edits_with_null_mutation_class():
    ledger = ActionLedger()
    ledger.append(_event(1, "pip install -e .", 0, "language_package_install"))
    ledger.append(_event(2, PRINTF_EDIT, 0, None))          # file edit -> KEPT
    ledger.append(_event(3, PYTHON_C_EDIT, 0, None))        # file edit -> KEPT
    ledger.append(_event(4, "python -m pytest -q", 0, None))  # test -> DROPPED
    ledger.append(_event(5, "cat pkg/utils.py", 0, None))   # read-only -> DROPPED

    cmds = build_commands_from_ledger(ledger)

    # Both file-edit commands must appear
    assert any("utils.py" in c and (">>" in c or "open(" in c) for c in cmds), (
        f"Expected file-edit commands in result, got: {cmds}"
    )
    # pip install kept (existing behaviour)
    assert "pip install -e ." in cmds
    # test runner dropped
    assert not any("pytest" in c for c in cmds), f"pytest should be dropped; got: {cmds}"
    # cat (read-only) dropped
    assert not any(c.strip().startswith("cat ") for c in cmds), (
        f"cat read-only should be dropped; got: {cmds}"
    )


def test_failed_file_edit_not_kept():
    """rc!=0 file edits must NOT be kept even if they look like file edits."""
    ledger = ActionLedger()
    ledger.append(_event(1, "printf 'x' >> a.py", 1, None))  # rc=1 -> dropped

    cmds = build_commands_from_ledger(ledger)
    assert cmds == [], f"Expected empty list for failed file edit, got {cmds}"


def test_order_preserved_with_file_edits():
    """File edits must appear in trajectory order alongside normal mutations."""
    ledger = ActionLedger()
    ledger.append(_event(1, "pip install requests", 0, "language_package_install"))
    ledger.append(_event(2, PRINTF_EDIT, 0, None))
    ledger.append(_event(3, "pip install pytest", 0, "language_package_install"))

    cmds = build_commands_from_ledger(ledger)
    assert cmds[0] == "pip install requests"
    assert cmds[1] == PRINTF_EDIT
    assert cmds[2] == "pip install pytest"


def test_sed_i_kept():
    """sed -i (in-place edit) must be kept."""
    ledger = ActionLedger()
    ledger.append(_event(1, "sed -i 's/old/new/' config.py", 0, None))

    cmds = build_commands_from_ledger(ledger)
    assert cmds == ["sed -i 's/old/new/' config.py"]


def test_echo_redirect_kept():
    """echo >> file is a file write and must be kept."""
    ledger = ActionLedger()
    ledger.append(_event(1, "echo 'PYTHONPATH=.' >> .env", 0, None))

    cmds = build_commands_from_ledger(ledger)
    assert cmds == ["echo 'PYTHONPATH=.' >> .env"]
