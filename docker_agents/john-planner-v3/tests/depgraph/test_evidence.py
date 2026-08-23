"""Evidence-parsing unit tests for extras handling (Task 8: targeted extras).

Bug this guards: per-dep extras (``uvicorn[standard]``) were silently stripped
by ``_parse_requirement_line`` before ever reaching a ``PythonRequirement``, so
the extra's transitive deps vanished under a ``--no-deps`` install. These tests
pin the 3-tuple -> 4-tuple parse change and the ``PythonRequirement.extras``
field it feeds, plus the existing optional-dependency group tag it must not
disturb.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from python_deps.evidence import (
    _add_requirement_line,
    _build_import_mappings,
    _parse_requirement_line,
    collect_python_dependency_evidence,
)
from python_deps.models import ImportFinding, PythonDependencyEvidence, PythonRequirement


def test_parse_requirement_line_returns_four_tuple_with_extras():
    parsed = _parse_requirement_line("uvicorn[standard]>=0.20")
    assert parsed == ("uvicorn", ">=0.20", "", ("standard",))


def test_parse_requirement_line_no_extras_yields_empty_tuple():
    parsed = _parse_requirement_line("requests>=2.0")
    assert parsed == ("requests", ">=2.0", "", ())


def test_parse_requirement_line_multiple_extras_sorted():
    parsed = _parse_requirement_line("uvicorn[standard,dotenv]>=0.20")
    assert parsed is not None
    name, specifier, marker, extras = parsed
    assert name == "uvicorn"
    assert specifier == ">=0.20"
    assert marker == ""
    assert extras == ("dotenv", "standard")  # sorted, deterministic


def test_parse_requirement_line_vcs_url_yields_no_extras():
    # The git+/egg= fallback path never parses extras; must still be a 4-tuple.
    parsed = _parse_requirement_line("git+https://example.com/x.git#egg=widget")
    assert parsed == ("widget", "git+https://example.com/x.git#egg=widget", "", ())


def test_add_requirement_line_stores_extras_on_requirement():
    target: list[PythonRequirement] = []
    _add_requirement_line(target, "uvicorn[standard]>=0.20", "pyproject.toml:project.dependencies")
    assert len(target) == 1
    req = target[0]
    assert req.name == "uvicorn"
    assert req.extras == ("standard",)
    assert req.specifier == ">=0.20"


def test_add_requirement_line_no_extras_defaults_empty_tuple():
    target: list[PythonRequirement] = []
    _add_requirement_line(target, "requests>=2.0", "pyproject.toml:project.dependencies")
    assert target[0].extras == ()


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def test_collect_pyproject_optional_dependency_group_tag_preserved(tmp_path):
    # The group tag (kind + source suffix) must survive alongside the new
    # extras field -- this is what roots.py's needed_extras gate reads.
    _write(
        tmp_path,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["uvicorn[standard]>=0.20"]

        [project.optional-dependencies]
        test = ["pytest"]
        docs = ["sphinx"]
        """,
    )
    evidence = collect_python_dependency_evidence(str(tmp_path))
    by_name = {req.name: req for req in evidence.declared_dependencies}

    uvicorn = by_name["uvicorn"]
    assert uvicorn.kind == "dependency"
    assert uvicorn.extras == ("standard",)

    pytest_req = by_name["pytest"]
    assert pytest_req.kind == "optional_dependency"
    assert pytest_req.source.endswith("optional-dependencies.test")

    sphinx_req = by_name["sphinx"]
    assert sphinx_req.kind == "optional_dependency"
    assert sphinx_req.source.endswith("optional-dependencies.docs")


def test_build_import_mappings_omits_unresolved(monkeypatch):
    # Bug this guards: an unresolved import carries no distribution name to
    # advise (Task 6). Before the guard, _build_import_mappings would still
    # emit an ImportPackageMapping for it (package_name=None), polluting the
    # advisory evidence layer with a mapping nobody can act on.
    import python_deps.evidence as evidence_module
    from python_deps.import_mapping import MappingResult, unresolved_result

    monkeypatch.setattr(
        evidence_module,
        "map_import_to_package",
        lambda name, *a, **k: unresolved_result(name)
        if name == "mystery"
        else MappingResult(name, name, "direct_name", "low"),
    )

    ev = PythonDependencyEvidence(repo_path=".")
    ev.imports.extend(
        [
            ImportFinding(import_name="requests", classification="external"),
            ImportFinding(import_name="mystery", classification="external"),
        ]
    )

    mappings = _build_import_mappings(ev)
    names = {m.import_name for m in mappings}
    assert "requests" in names
    assert "mystery" not in names


def test_evidence_used_extras_defaults_to_empty_set():
    ev = PythonDependencyEvidence(repo_path="/x")
    assert ev.used_extras == set()


def test_evidence_to_dict_includes_sorted_used_extras():
    ev = PythonDependencyEvidence(repo_path="/x")
    ev.used_extras.update({"socks", "http2"})
    assert ev.to_dict()["used_extras"] == ["http2", "socks"]


def _canon_deps(evidence, kind):
    return {(r.name, r.kind, r.source) for r in evidence.declared_dependencies if r.kind == kind}


def test_collect_dependency_groups_basic(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "proj"
            version = "0.1.0"
            dependencies = ["flask"]

            [dependency-groups]
            test = ["pytest", "pytest-cov"]
            """
        ),
        encoding="utf-8",
    )
    ev = collect_python_dependency_evidence(str(tmp_path))
    dev = _canon_deps(ev, "dev_group")
    assert ("pytest", "dev_group", "pyproject.toml:dependency-groups.test") in dev
    assert ("pytest-cov", "dev_group", "pyproject.toml:dependency-groups.test") in dev
    # runtime dep still classified as dependency
    assert any(r.name == "flask" and r.kind == "dependency" for r in ev.declared_dependencies)


def test_collect_dependency_groups_include_group_flattens_transitively(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "proj"
            version = "0.1.0"

            [dependency-groups]
            test = ["pytest"]
            typing = [{include-group = "test"}, "mypy"]
            """
        ),
        encoding="utf-8",
    )
    ev = collect_python_dependency_evidence(str(tmp_path))
    typing = {r.name for r in ev.declared_dependencies
              if r.source == "pyproject.toml:dependency-groups.typing"}
    assert typing == {"pytest", "mypy"}  # test's member flattened under typing


def test_collect_dependency_groups_cycle_terminates_and_records_error(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "proj"
            version = "0.1.0"

            [dependency-groups]
            a = [{include-group = "b"}, "pkg-a"]
            b = [{include-group = "a"}, "pkg-b"]
            """
        ),
        encoding="utf-8",
    )
    ev = collect_python_dependency_evidence(str(tmp_path))  # must not hang
    names = {r.name for r in ev.declared_dependencies if r.kind == "dev_group"}
    assert "pkg-a" in names and "pkg-b" in names
    assert any("cycle" in e.lower() for e in ev.collection_errors)


def test_collect_dependency_groups_absent_is_noop(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "proj"
            version = "0.1.0"
            dependencies = ["flask"]
            """
        ),
        encoding="utf-8",
    )
    ev = collect_python_dependency_evidence(str(tmp_path))
    assert not [r for r in ev.declared_dependencies if r.kind == "dev_group"]


def _by_name(evidence):
    return {r.name: r for r in evidence.declared_dependencies}


def test_requirements_txt_is_runtime(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    assert _by_name(ev)["flask"].kind == "dependency"


def test_requirements_dev_is_dev_group(tmp_path):
    (tmp_path / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    req = _by_name(ev)["pytest"]
    assert req.kind == "dev_group"
    assert req.source == "requirements-file.dev"


def test_nested_docs_requirements_is_dev_group_docs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "requirements.txt").write_text("sphinx\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    req = _by_name(ev)["sphinx"]
    assert req.kind == "dev_group"
    assert req.source == "requirements-file.docs"


def test_nested_requirements_dir_test_file_is_dev_group_test(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "test.txt").write_text("pytest-xdist\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    req = _by_name(ev)["pytest-xdist"]
    assert req.kind == "dev_group"
    assert req.source == "requirements-file.test"


def test_nested_requirements_dir_base_file_is_runtime(tmp_path):
    (tmp_path / "requirements").mkdir()
    (tmp_path / "requirements" / "base.txt").write_text("flask\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    assert _by_name(ev)["flask"].kind == "dependency"


def test_editable_self_extras_captured_into_used_extras(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "-e .[http2,socks]\npytest\n", encoding="utf-8"
    )
    ev = collect_python_dependency_evidence(str(tmp_path))
    assert {"http2", "socks"} <= ev.used_extras
    # the -e line is NOT added as a distribution named "."/project
    assert "." not in _by_name(ev)


def test_bare_editable_self_is_ignored(tmp_path):
    (tmp_path / "requirements.txt").write_text("-e .\nflask\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    assert ev.used_extras == set()
    assert "flask" in _by_name(ev)


def test_dash_r_include_is_followed_with_referenced_file_role(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (tmp_path / "requirements-dev.txt").write_text("-r requirements.txt\npytest\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))
    by = _by_name(ev)
    assert by["flask"].kind == "dependency"       # base file's role
    assert by["pytest"].kind == "dev_group"        # dev file's role
    assert by["pytest"].source == "requirements-file.dev"


def test_dash_r_self_cycle_terminates(tmp_path):
    (tmp_path / "requirements.txt").write_text("-r requirements.txt\nflask\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))  # must not hang
    assert "flask" in _by_name(ev)


def test_option_lines_are_ignored(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "--index-url https://example.com/simple\n-i https://example.com/simple\nflask\n",
        encoding="utf-8",
    )
    ev = collect_python_dependency_evidence(str(tmp_path))
    assert set(_by_name(ev)) == {"flask"}


def test_dash_c_constraint_include_routes_to_constraints_not_declared(tmp_path):
    # Bug this guards: a -c/--constraint include must land in
    # evidence.constraint_dependencies (kind="constraint"), NOT be followed as
    # a -r/--requirement include into declared_dependencies.
    (tmp_path / "requirements.txt").write_text(
        "-c constraints.txt\nflask\n", encoding="utf-8"
    )
    (tmp_path / "constraints.txt").write_text("foo==1.0\n", encoding="utf-8")
    ev = collect_python_dependency_evidence(str(tmp_path))

    assert _by_name(ev)["flask"].kind == "dependency"
    assert "foo" not in _by_name(ev)

    constraint = {r.name: r for r in ev.constraint_dependencies}["foo"]
    assert constraint.kind == "constraint"


def test_nested_tests_dir_requirements_file_is_discovered(tmp_path):
    # Nested-dir allowlist branch: tests/*requirements*.txt is discovered even
    # though it isn't a root-level requirements*.txt file.
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "requirements.txt").write_text(
        "pytest-mock\n", encoding="utf-8"
    )
    ev = collect_python_dependency_evidence(str(tmp_path))
    req = _by_name(ev)["pytest-mock"]
    assert req.kind == "dev_group"
    assert req.source == "requirements-file.test"


def test_include_depth_cap_bites_on_linear_chain(tmp_path):
    # _MAX_INCLUDE_DEPTH = 5; the guard in _ingest_requirements_file is
    # `depth > _MAX_INCLUDE_DEPTH`, so depths 0..5 are processed and depth 6
    # is cut off before its file is even read. requirements.txt is discovered
    # at depth 0; chain1..chain6.txt are reachable ONLY via -r includes (they
    # don't match the root requirements*.txt glob), landing at depths 1..6.
    (tmp_path / "requirements.txt").write_text("-r chain1.txt\npkg0\n", encoding="utf-8")
    for i in range(1, 6):
        (tmp_path / f"chain{i}.txt").write_text(
            f"-r chain{i + 1}.txt\npkg{i}\n", encoding="utf-8"
        )
    (tmp_path / "chain6.txt").write_text("pkg6\n", encoding="utf-8")

    ev = collect_python_dependency_evidence(str(tmp_path))
    names = set(_by_name(ev))

    for i in range(6):  # pkg0..pkg5: depths 0..5, all processed
        assert f"pkg{i}" in names
    assert "pkg6" not in names  # depth 6: guard `depth > 5` returns before reading


def test_requirements_file_source_survives_symlinked_root(tmp_path):
    # Bug: _ingest_requirements_file resolves `path` (for cycle detection) but
    # NOT `root` before computing the relpath-based `source`. On a repo root
    # that resolves through a symlink (e.g. macOS /var -> /private/var), the
    # divergent prefix makes os.path.relpath walk upward, corrupting `source`
    # into something like "../../../../private/var/.../requirements.txt"
    # instead of the clean relative path "requirements.txt".
    #
    # Constructed via an explicit symlink (real/proj -> link/proj) so the
    # root/resolved divergence is forced on every platform, not just macOS.
    real_root = tmp_path / "real" / "proj"
    real_root.mkdir(parents=True)
    (real_root / "requirements.txt").write_text("flask\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real_root.parent)
    repo_path = link / "proj"

    ev = collect_python_dependency_evidence(str(repo_path))
    req = _by_name(ev)["flask"]

    assert req.source == "requirements.txt"
    assert ".." not in req.source
    assert "/private/" not in req.source
