"""Tier-1 Fix 1 & 2 (mechanism): build_commands_from_ledger must be able to DROP
file-edit and package-install commands so the closure/file-capture units become the
single source of truth.

The drop behaviour is opt-in (the v1g finalize path opts in); the default keeps the
historical replay behaviour, so the legacy path and existing tests are untouched.
"""
from src.envstate.ledger import ActionEvent, ActionLedger
from src.envstate.synthesis import build_commands_from_ledger


def _ev(step, cmd, rc=0, mc=None):
    return ActionEvent(
        step=step, task_id=None, cmd=cmd, rc=rc, stdout="", stdout_path=None,
        stderr_path=None, env_revision_before=0, env_revision_after=0,
        mutation_class=mc, container_id="c1", summary="",
    )


def test_default_keeps_file_edits_and_installs():
    led = ActionLedger()
    led.append(_ev(1, "pip install requests", 0, "language_package_install"))
    led.append(_ev(2, "sed -i 's/a/b/' c.py", 0, None))
    cmds = build_commands_from_ledger(led)
    assert "pip install requests" in cmds
    assert "sed -i 's/a/b/' c.py" in cmds


def test_drop_file_edits_removes_edits_keeps_system():
    led = ActionLedger()
    led.append(_ev(1, "apt-get install -y libpq-dev", 0, "system_package_install"))
    led.append(_ev(2, "sed -i 's/a/b/' c.py", 0, None))
    led.append(_ev(3, "echo 'x' >> .env", 0, "file_or_env_change"))
    led.append(_ev(4, "printf 'y' > f.cfg", 0, None))
    cmds = build_commands_from_ledger(led, drop_file_edits=True)
    assert cmds == ["apt-get install -y libpq-dev"]


def test_drop_package_installs_removes_all_flavors():
    led = ActionLedger()
    flavors = [
        "pip install requests",
        "pip3 install flask",
        "python -m pip install numpy",
        "python3 -m pip install -r requirements.txt",
        "poetry install",
        "poetry add httpx",
        "uv pip install rich",
        "uv sync",
        "uv add typer",
        "conda install pandas",
        "pipenv install",
    ]
    for c in flavors:
        led.append(_ev(1, c, 0, "language_package_install"))
    led.append(_ev(2, "apt-get install -y gcc", 0, "system_package_install"))
    cmds = build_commands_from_ledger(led, drop_package_installs=True)
    assert cmds == ["apt-get install -y gcc"]


def test_drop_package_install_with_trailing_test_compound():
    led = ActionLedger()
    led.append(_ev(1, "pip install -r requirements.txt && pytest -q", 0, "language_package_install"))
    cmds = build_commands_from_ledger(led, drop_package_installs=True)
    assert cmds == []


def test_drop_navigation_prefixed_install():
    """`cd /app && pip install -e .` is a project install and must be dropped."""
    led = ActionLedger()
    led.append(_ev(1, "cd /app && pip install -e .", 0, "language_package_install"))
    led.append(_ev(2, "apt-get install -y gcc", 0, "system_package_install"))
    cmds = build_commands_from_ledger(led, drop_package_installs=True)
    assert cmds == ["apt-get install -y gcc"]


def test_apt_pip_compound_keeps_apt_drops_smuggled_pip():
    """MEDIUM 2: an apt-led compound keeps its apt segment but the trailing replayed
    (unversioned) pip install is stripped — the pinned closure is the single source of
    truth, so a smuggled `pip install Y` must not survive."""
    led = ActionLedger()
    led.append(_ev(1, "apt-get install -y libpq-dev && pip install psycopg2", 0, "system_package_install"))
    cmds = build_commands_from_ledger(led, drop_package_installs=True)
    assert cmds == ["apt-get install -y libpq-dev"]


def test_apt_pip_compound_with_distill_drops_pip():
    """Same MEDIUM 2 case, but with the real distiller that splits the chain into units:
    the apt unit survives, the standalone pip unit is dropped."""
    from src.synthesizer import Synthesizer
    led = ActionLedger()
    led.append(_ev(1, "apt-get install -y libpq-dev && pip install psycopg2", 0, "system_package_install"))
    synth = Synthesizer(base_image="python:3.11-slim", workdir="/app")
    cmds = build_commands_from_ledger(
        led, distill=synth._extract_recordable_setup_commands, drop_package_installs=True
    )
    assert any("apt-get install -y libpq-dev" in c for c in cmds)
    assert not any("psycopg2" in c for c in cmds)


def test_apt_pip_compound_kept_whole_in_legacy_mode():
    """Without drop_package_installs the historical behavior is unchanged (kept whole)."""
    led = ActionLedger()
    led.append(_ev(1, "apt-get install -y libpq-dev && pip install psycopg2", 0, "system_package_install"))
    cmds = build_commands_from_ledger(led)
    assert cmds == ["apt-get install -y libpq-dev && pip install psycopg2"]


def test_drop_both_keeps_only_irreducible():
    led = ActionLedger()
    led.append(_ev(1, "pip install -e .", 0, "language_package_install"))
    led.append(_ev(2, "sed -i 's/a/b/' c.py", 0, None))
    led.append(_ev(3, "apt-get install -y libpq-dev", 0, "system_package_install"))
    led.append(_ev(4, "ln -s /usr/lib/x /usr/lib/y", 0, "file_or_env_change"))
    led.append(_ev(5, "meson setup build", 0, "other_mutation"))
    cmds = build_commands_from_ledger(led, drop_file_edits=True, drop_package_installs=True)
    assert "pip install -e ." not in cmds
    assert "sed -i 's/a/b/' c.py" not in cmds
    assert "apt-get install -y libpq-dev" in cmds
    assert "ln -s /usr/lib/x /usr/lib/y" in cmds
    assert "meson setup build" in cmds


def test_failed_commands_still_dropped_with_flags():
    led = ActionLedger()
    led.append(_ev(1, "apt-get install -y x", 1, "system_package_install"))
    cmds = build_commands_from_ledger(led, drop_file_edits=True, drop_package_installs=True)
    assert cmds == []
