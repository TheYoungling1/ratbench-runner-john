# tests/test_manifest_optional_deps.py
"""Unit tests for BUG-2: parse [project.optional-dependencies] test extras."""
import pytest
from pathlib import Path

from src.envstate.manifest import parse_manifests


_WELL_KNOWN_EXTRAS = ["tests", "test", "testing", "dev", "development", "ci"]


def _make_pyproject(extras_name: str) -> str:
    return (
        '[project]\n'
        'name = "mypackage"\n'
        'dependencies = []\n'
        '\n'
        f'[project.optional-dependencies]\n'
        f'{extras_name} = ["pytest", "psycopg2-binary>=2.9"]\n'
    )


def test_optional_deps_tests_key(tmp_path: Path) -> None:
    """[project.optional-dependencies] tests=[...] should be included in required."""
    (tmp_path / "pyproject.toml").write_text(_make_pyproject("tests"))
    r = parse_manifests(str(tmp_path))
    names = {f.name.lower() for f in r.required}
    assert "pytest" in names, f"pytest missing from required; got {names}"
    assert "psycopg2-binary" in names, f"psycopg2-binary missing from required; got {names}"


@pytest.mark.parametrize("extra_name", _WELL_KNOWN_EXTRAS)
def test_optional_deps_well_known_extra_keys(
    tmp_path: Path, extra_name: str
) -> None:
    """All well-known test/dev extra names should be parsed."""
    (tmp_path / "pyproject.toml").write_text(_make_pyproject(extra_name))
    r = parse_manifests(str(tmp_path))
    names = {f.name.lower() for f in r.required}
    assert "pytest" in names, (
        f"pytest missing for extra_name={extra_name!r}; got {names}"
    )
    assert "psycopg2-binary" in names, (
        f"psycopg2-binary missing for extra_name={extra_name!r}; got {names}"
    )


def test_optional_deps_unknown_extra_ignored(tmp_path: Path) -> None:
    """Extras with unrecognized names (e.g. 'docs') should NOT be included."""
    (tmp_path / "pyproject.toml").write_text(_make_pyproject("docs"))
    r = parse_manifests(str(tmp_path))
    names = {f.name.lower() for f in r.required}
    # docs extra should be skipped; no pytest/psycopg2-binary leaked in
    assert "pytest" not in names, f"pytest should not appear for docs extra; got {names}"


def test_optional_deps_dedup_with_main_deps(tmp_path: Path) -> None:
    """Packages declared in both dependencies and optional-dependencies are deduped."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\n'
        'name = "mypackage"\n'
        'dependencies = ["pytest>=7"]\n'
        '\n'
        '[project.optional-dependencies]\n'
        'tests = ["pytest", "psycopg2-binary>=2.9"]\n'
    )
    r = parse_manifests(str(tmp_path))
    names = [f.name.lower() for f in r.required]
    assert names.count("pytest") == 1, f"pytest should appear exactly once; got {names}"
    assert "psycopg2-binary" in names
