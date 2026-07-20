# tests/test_build_pin_instructions.py
"""TDD tests for build_pin_instructions (src/envstate/synthesis.py)."""
from src.envstate.world_model import Fact
from src.envstate.synthesis import build_pin_instructions


def F(n, v):
    return Fact(name=n, detail=v)


def test_emits_two_commands_printf_then_pip_r():
    cmds = build_pin_instructions((F("requests", "2.31.0"), F("urllib3", "2.0.0")))
    assert len(cmds) == 2
    assert cmds[0].startswith("printf ") and ">" in cmds[0]
    assert cmds[1].startswith("pip install -r ") and "&&" not in cmds[1]
    assert "requests==2.31.0" in cmds[0] and "urllib3==2.0.0" in cmds[0]
    assert "jayint-pinned-closure.txt" in cmds[0] and "jayint-pinned-closure.txt" in cmds[1]


def test_excludes_project_by_normalized_name():
    cmds = build_pin_instructions(
        (F("My_Pkg", "0.1.0"), F("requests", "2.31.0")), project_name="my-pkg"
    )
    assert "requests==2.31.0" in cmds[0]
    assert "my_pkg" not in cmds[0].lower() and "my-pkg" not in cmds[0].lower()


def test_empty_or_no_versions_returns_empty():
    assert build_pin_instructions(()) == []
    assert build_pin_instructions((Fact(name="x", detail=""),)) == []


def test_excludes_installer_packages():
    """pip, setuptools, wheel must never be pinned (causes self-downgrade)."""
    cmds = build_pin_instructions((
        F("pip", "25.0.1"),
        F("setuptools", "68.0.0"),
        F("wheel", "0.41.0"),
        F("requests", "2.31.0"),
    ))
    assert len(cmds) == 2, f"Expected 2 commands, got {cmds}"
    printf_line = cmds[0]
    assert "requests==2.31.0" in printf_line
    assert "pip==" not in printf_line
    assert "setuptools==" not in printf_line
    assert "wheel==" not in printf_line


def test_excludes_installer_only_returns_empty():
    """When only installers are in the list, nothing to pin."""
    cmds = build_pin_instructions((F("pip", "25.0.1"), F("wheel", "0.41.0")))
    assert cmds == []
