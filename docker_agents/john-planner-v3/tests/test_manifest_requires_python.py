# tests/test_manifest_requires_python.py
"""read_requires_python: locate the project's declared python constraint.

Returns the RAW specifier string (PEP 621 ``requires-python`` or poetry's
``tool.poetry.dependencies.python``); interpreting it is choose_python_minor's
job, not this reader's. Pure, never raises.
"""
from src.envstate.manifest import read_requires_python


def test_pep621_requires_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.10,<3.13"\n'
    )
    assert read_requires_python(str(tmp_path)) == ">=3.10,<3.13"


def test_poetry_python_constraint(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.10"\nflask = "^2.0"\n'
    )
    assert read_requires_python(str(tmp_path)) == "^3.10"


def test_pep621_takes_precedence_over_poetry(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.11"\n'
        '[tool.poetry.dependencies]\npython = "^3.8"\n'
    )
    assert read_requires_python(str(tmp_path)) == ">=3.11"


def test_absent_returns_none(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert read_requires_python(str(tmp_path)) is None


def test_no_pyproject_returns_none(tmp_path):
    assert read_requires_python(str(tmp_path)) is None


def test_malformed_pyproject_does_not_raise(tmp_path):
    (tmp_path / "pyproject.toml").write_text("not valid toml [[[")
    assert read_requires_python(str(tmp_path)) is None
