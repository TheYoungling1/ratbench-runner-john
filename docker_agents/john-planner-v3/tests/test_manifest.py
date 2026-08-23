# tests/test_manifest.py
import os
from src.envstate.manifest import parse_manifests, ManifestResult


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(tmp_path)


def test_pip_requirements_and_includes(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask>=2.0\n# comment\n-r extra.txt\n")
    (tmp_path / "extra.txt").write_text("pytest\npsycopg2-binary==2.9.5\n")
    r = parse_manifests(str(tmp_path))
    assert r.build_system == "pip"
    names = {f.name.lower() for f in r.required}
    assert {"flask", "pytest", "psycopg2-binary"} <= names


def test_pyproject_pep621_and_backend(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["flask[async]>=2.0", "requests; python_version<\\"3.9\\""]\n'
        '[build-system]\nbuild-backend = "setuptools.build_meta"\n'
    )
    r = parse_manifests(str(tmp_path))
    assert r.build_system == "setuptools"
    assert "flask" in {f.name.lower() for f in r.required}
    assert "requests" in {f.name.lower() for f in r.required}


def test_poetry_detected(tmp_path):
    (tmp_path / "poetry.lock").write_text("")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.10"\nflask = "^2.0"\n'
    )
    r = parse_manifests(str(tmp_path))
    assert r.build_system == "poetry"
    assert "flask" in {f.name.lower() for f in r.required}
    assert "python" not in {f.name.lower() for f in r.required}


def test_malformed_pyproject_does_not_raise(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is not = valid toml [[[")
    r = parse_manifests(str(tmp_path))
    assert isinstance(r, ManifestResult)
    assert r.build_system in ("unknown", "setuptools", "pip")


def test_none_present_is_unknown(tmp_path):
    r = parse_manifests(str(tmp_path))
    assert r.build_system == "unknown"
    assert r.required == ()


def test_dedup_across_files(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n")
    (tmp_path / "requirements-dev.txt").write_text("flask\npytest\n")
    r = parse_manifests(str(tmp_path))
    names = [f.name.lower() for f in r.required]
    assert names.count("flask") == 1


def test_pipfile_detected_and_packages(tmp_path):
    # spec §4.1: Pipfile → pipenv; [packages] names extracted (detail dropped for "*")
    (tmp_path / "Pipfile").write_text(
        '[packages]\nflask = "*"\nrequests = "==2.31.0"\n[dev-packages]\npytest = "*"\n'
    )
    r = parse_manifests(str(tmp_path))
    assert r.build_system == "pipenv"
    names = {f.name.lower() for f in r.required}
    assert "flask" in names
    assert "requests" in names


def test_setup_only_is_setuptools(tmp_path):
    # spec §4.1: setup.py / setup.cfg only → setuptools
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    r = parse_manifests(str(tmp_path))
    assert r.build_system == "setuptools"
