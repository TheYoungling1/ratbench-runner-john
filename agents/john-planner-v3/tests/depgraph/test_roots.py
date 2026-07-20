"""Root selection — manifest-declared roots only, non-distribution filtered."""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace

from python_deps.depgraph.roots import _env_marker_excludes, select_roots
from python_deps.depgraph.scan import scan_to_nodes
from python_deps.depgraph.target_env import TargetEnv


def _write(repo: Path, rel: str, body: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["flask", "requests"]
        """,
    )
    # A py2 shim, a stdlib import, a real external import, and a declared dep.
    _write(
        repo,
        "proj/app.py",
        """
        import os
        import StringIO
        import requests
        import boto3
        """,
    )
    return repo


def test_declared_dependencies_become_roots_with_none_import_id(tmp_path):
    repo = _fixture_repo(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    declared = {dist for imp, dist in roots if imp is None}
    assert "flask" in declared
    assert "requests" in declared


def test_py2_shim_is_filtered_out(tmp_path):
    repo = _fixture_repo(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    dists = {dist for _imp, dist in roots}
    assert "StringIO" not in dists


def test_scanned_import_does_not_fabricate_root(tmp_path):
    # boto3 is imported but NOT declared in the manifest. Imports never generate
    # roots: under declared-only construction no scanned import fabricates a root
    # (whether or not it maps via the curated table). An undeclared import is an
    # audit signal only -- reconciled post-install, never fabricated at
    # construction. boto3 must NOT appear as a root.
    repo = _fixture_repo(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    assert "boto3" not in {dist for _imp, dist in roots}


def test_scanned_curated_import_does_not_fabricate_root(tmp_path):
    # yaml is imported (the curated table maps yaml -> PyYAML) but NOT declared
    # in the manifest. A curated-table match does NOT license fabrication either:
    # imports never generate roots. It is an AUDIT signal only -- no PyYAML root
    # is fabricated from the import. The two-phase design re-homes
    # under-declared-alias recovery to a later post-install repair pass, never to
    # a fabricated construction root. Every root is declared.
    repo = tmp_path / "proj2"
    repo.mkdir()
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj2"
        version = "0.1.0"
        dependencies = ["flask", "requests"]
        """,
    )
    _write(
        repo,
        "proj2/app.py",
        """
        import os
        import StringIO
        import requests
        import yaml
        """,
    )
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    assert "PyYAML" not in {dist for _imp, dist in roots}
    assert all(imp is None for imp, _dist in roots)   # every root is declared


def test_declared_import_not_duplicated(tmp_path):
    repo = _fixture_repo(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    # requests is BOTH declared and imported; it must appear exactly once,
    # via the declared (import_id=None) entry, not a second scanned entry.
    requests_entries = [(imp, dist) for imp, dist in roots if dist == "requests"]
    assert requests_entries == [(None, "requests")]


def test_stdlib_import_never_a_root(tmp_path):
    repo = _fixture_repo(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    dists = {dist for _imp, dist in roots}
    assert "os" not in dists


def test_no_duplicate_distributions(tmp_path):
    repo = _fixture_repo(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    dists = [dist for _imp, dist in roots]
    assert len(dists) == len(set(dists))


def test_typing_only_stub_filtered(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(repo, "pyproject.toml", """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = []
        """)
    _write(repo, "proj/app.py", "import _typeshed\n")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)
    assert "_typeshed" not in {dist for _imp, dist in roots}


def test_junk_and_dunder_filtered(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(repo, "pyproject.toml", """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = []
        """)
    _write(repo, "proj/app.py", "import __main__\nimport _private\n")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)
    dists = {dist for _imp, dist in roots}
    assert "__main__" not in dists
    assert "_private" not in dists


def test_declared_py2_shim_filtered(tmp_path):
    # The manifest path (declared deps) must also drop py2-shim non-distributions.
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(repo, "pyproject.toml", """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["urllib2", "requests"]
        """)
    _write(repo, "proj/app.py", "import requests\n")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)
    dists = {dist for _imp, dist in roots}
    assert "urllib2" not in dists
    assert "requests" in dists


def test_declared_version_specifier_is_propagated(tmp_path):
    # A declared version pin must reach the resolver so a conflict is visible
    # (spec's "project pinning numpy<2 plus a dep requiring numpy>=2").
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(repo, "pyproject.toml", """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["urllib3<1.21", "requests==2.32.3"]
        """)
    _write(repo, "proj/app.py", "import requests\n")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)
    dists = {dist for _imp, dist in roots}
    assert "urllib3<1.21" in dists
    assert "requests==2.32.3" in dists


def test_unsafe_specifier_falls_back_to_bare_name(tmp_path):
    # A specifier carrying a marker / odd chars is dropped (bare name kept) rather
    # than risking injection into the resolver's temp pyproject.
    from python_deps.depgraph.roots import _manifest_root_token
    from python_deps.models import PythonRequirement

    assert _manifest_root_token(PythonRequirement("flask", ">=2.0")) == "flask>=2.0"
    assert (
        _manifest_root_token(PythonRequirement("flask", ">=2.0; python_version<'3.9'"))
        == "flask"
    )
    assert _manifest_root_token(PythonRequirement("flask", "")) == "flask"


def test_manifest_scan_dedup_via_normalization(tmp_path):
    # Declared `Flask` and imported `flask` must dedup via normalize_package_name.
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(repo, "pyproject.toml", """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["Flask"]
        """)
    _write(repo, "proj/app.py", "import flask\n")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)
    flask_entries = [(imp, dist) for imp, dist in roots if dist.lower() == "flask"]
    assert flask_entries == [(None, "Flask")]


# --------------------------------------------------------------------------- #
# Task 8 — targeted extras: needed_extras gating + per-dep extras preserved.
# --------------------------------------------------------------------------- #
def _fixture_repo_with_optional_groups(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["requests"]

        [project.optional-dependencies]
        test = ["pytest"]
        docs = ["sphinx"]
        """,
    )
    _write(repo, "proj/app.py", "import requests\n")
    return repo


def test_only_needed_extra_group_becomes_a_root(tmp_path):
    repo = _fixture_repo_with_optional_groups(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, needed_extras=frozenset({"test"}))

    names = {tok for _imp, tok in roots}
    assert any(t.startswith("requests") for t in names)   # runtime always
    assert any(t.startswith("pytest") for t in names)     # needed extra
    assert not any(t.startswith("sphinx") for t in names)  # unneeded group excluded


def test_no_needed_extras_default_excludes_all_optional_groups(tmp_path):
    repo = _fixture_repo_with_optional_groups(tmp_path)
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)  # default needed_extras=frozenset()

    names = {tok for _imp, tok in roots}
    assert any(t.startswith("requests") for t in names)
    assert not any(t.startswith("pytest") for t in names)
    assert not any(t.startswith("sphinx") for t in names)


def test_per_dep_extra_specifier_is_preserved(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["uvicorn[standard]>=0.20"]
        """,
    )
    _write(repo, "proj/app.py", "import uvicorn\n")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)

    names = {tok for _imp, tok in roots}
    assert any("uvicorn[standard]" in t for t in names)   # extra NOT stripped
    assert not any(t == "uvicorn" for t in names)          # not silently bare


# --------------------------------------------------------------------------- #
# Task 8 review fix — target_env-conditioned environment-marker filtering.
#
# CRITICAL: "no silent shrink" — a marker'd dep must be dropped ONLY when a
# target_env is present, the marker has no `extra` reference, and it evaluates
# False for that target. Every other case (no target_env, no marker, an
# extra-gated marker, a True evaluation) must KEEP the dep.
# --------------------------------------------------------------------------- #
_LINUX_TARGET_ENV = TargetEnv(
    python_full="3.11.0",
    python_version="3.11",
    platform_machine="x86_64",
    sys_platform="linux",
    os_name="posix",
    platform_system="Linux",
    python_platform_tag="x86_64-manylinux_2_28",
)


def _fixture_repo_with_marker_dep(tmp_path: Path, marker: str) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(
        repo,
        "pyproject.toml",
        f"""
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["foo ; {marker}"]
        """,
    )
    _write(repo, "proj/app.py", "")
    return repo


def test_env_marker_false_dep_skipped_for_target(tmp_path):
    repo = _fixture_repo_with_marker_dep(tmp_path, "sys_platform == 'win32'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" not in dists


def test_env_marker_true_dep_kept_for_target(tmp_path):
    repo = _fixture_repo_with_marker_dep(tmp_path, "sys_platform == 'linux'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


def test_extra_marker_dep_not_dropped_by_env_filter(tmp_path):
    # An extra-gated marker must NEVER be judged by the env filter — that is
    # needed_extras' job. Even with a target_env given (and no needed_extras
    # requested), the env filter itself must not be the reason it's dropped;
    # to isolate that, request the "x" extra so the ONLY question left is
    # whether the env filter wrongly re-excludes it.
    repo = _fixture_repo_with_marker_dep(tmp_path, "extra == 'x'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(
        str(repo),
        graph,
        needed_extras=frozenset({"x"}),
        target_env=_LINUX_TARGET_ENV,
    )

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


def test_no_target_env_keeps_marker_deps(tmp_path):
    repo = _fixture_repo_with_marker_dep(tmp_path, "sys_platform == 'win32'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph)  # target_env=None (default)

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


# --------------------------------------------------------------------------- #
# marker-field-coverage: TargetEnv.marker_env() now covers the interpreter-
# implementation trio (platform_python_implementation / implementation_name /
# implementation_version), so a marker gated ONLY on those + covered fields is
# now judged against the CPython/linux target instead of forcing a keep. Only
# the genuinely-unknowable kernel fields (platform_release / platform_version)
# and `extra` remain uncovered → still keep-on-uncertainty.
#
# The two tests below previously asserted the OLD (over-including) behavior —
# that a `platform_python_implementation == 'PyPy'` / `implementation_name ==
# 'pypy'` dep was KEPT on a CPython target because the field was uncovered.
# marker_env() now covers those fields, so the correct verdict is DROP.
# --------------------------------------------------------------------------- #


def test_platform_python_implementation_false_dep_dropped_on_cpython(tmp_path):
    # `platform_python_implementation` is now COVERED (='CPython' — the only base
    # this pipeline builds, probed by detect_target_env). A `== 'PyPy'` dep is
    # therefore definitively False for the target and correctly dropped (it was
    # wrongly kept before marker_env() covered the field).
    repo = _fixture_repo_with_marker_dep(
        tmp_path, "platform_python_implementation == 'PyPy'"
    )
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" not in dists


def test_implementation_name_false_dep_dropped_on_cpython(tmp_path):
    # Same, for `implementation_name` (now ='cpython'): a `== 'pypy'` guard is
    # False for the CPython target and correctly dropped.
    repo = _fixture_repo_with_marker_dep(tmp_path, "implementation_name == 'pypy'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" not in dists


def test_winloop_shape_dropped_on_linux_cpython(tmp_path):
    # The real anyio [dependency-groups].test winloop marker: a Windows-only
    # CPython C-extension. Both fields are now covered, so it evaluates False on
    # a linux CPython target and is dropped (the reported over-include bug).
    repo = _fixture_repo_with_marker_dep(
        tmp_path,
        "platform_python_implementation == 'CPython' and platform_system == 'Windows'",
    )
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" not in dists


def test_uvloop_shape_kept_on_linux_cpython(tmp_path):
    # The real anyio uvloop marker: applies on non-Windows CPython < 3.15 — TRUE
    # for a linux CPython 3.11 target, so it must still be KEPT even though it
    # references the now-covered platform_python_implementation field.
    repo = _fixture_repo_with_marker_dep(
        tmp_path,
        "platform_python_implementation == 'CPython' "
        "and platform_system != 'Windows' and python_version < '3.15'",
    )
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


def test_implementation_version_false_dep_dropped_on_cpython(tmp_path):
    # implementation_version is now covered (CPython: == python_full = 3.11.0).
    # A PyPy-version-style guard (`>= '7.0'`) is False for the target → dropped.
    repo = _fixture_repo_with_marker_dep(tmp_path, "implementation_version >= '7.0'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" not in dists


def test_uncovered_kernel_field_still_kept(tmp_path):
    # platform_release stays UNCOVERED (a kernel-specific string the container
    # cannot know ahead of run time), so a dep gated on it is kept-on-uncertainty
    # — the "no silent shrink" invariant still protects genuinely-unknowable
    # fields even though the impl trio is now covered.
    repo = _fixture_repo_with_marker_dep(tmp_path, "platform_release < '5.0'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


def test_uncovered_platform_version_field_still_kept(tmp_path):
    # platform_version (kernel build string) likewise stays uncovered → kept.
    repo = _fixture_repo_with_marker_dep(tmp_path, "platform_version == 'Windows'")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


def test_unevaluable_marker_is_kept():
    # A marker referencing a name outside the PEP 508 grammar (e.g. a typo)
    # can never survive `packaging.requirements.Requirement()` parsing, so
    # evidence.py drops such a dependency line entirely -- it never reaches
    # root selection at all (verified directly: `_parse_requirement_line`
    # returns None for "foo ; bogus_field == 'x'", so a pyproject.toml fixture
    # can't exercise this path). Exercise `_env_marker_excludes` directly
    # instead, with a stand-in object whose `.marker` is a raw (unparseable)
    # string, to hit the `_marker_applies`-returns-None / eval-error path.
    req = SimpleNamespace(name="foo", marker="bogus_field == 'x'")
    assert _env_marker_excludes(req, _LINUX_TARGET_ENV) is False


def test_unmarked_dep_always_kept(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["foo"]
        """,
    )
    _write(repo, "proj/app.py", "")
    graph = scan_to_nodes(str(repo))
    roots = select_roots(str(repo), graph, target_env=_LINUX_TARGET_ENV)

    dists = {dist for _imp, dist in roots}
    assert "foo" in dists


from python_deps.depgraph.roots import _requirement_group, _DEV_GROUP_DENYLIST


def test_requirement_group_parses_optional_dependencies_source():
    assert _requirement_group("pyproject.toml:project.optional-dependencies.test") == "test"


def test_requirement_group_parses_extras_require_source():
    assert _requirement_group("setup.cfg:options.extras_require.docs") == "docs"


def test_requirement_group_parses_dependency_groups_source():
    assert _requirement_group("pyproject.toml:dependency-groups.typing") == "typing"


def test_requirement_group_parses_requirements_file_source():
    assert _requirement_group("requirements-file.dev") == "dev"


def test_requirement_group_no_match_returns_empty():
    assert _requirement_group("pyproject.toml:project.dependencies") == ""


def test_dev_group_denylist_contents():
    assert _DEV_GROUP_DENYLIST == frozenset(
        {
            "docs", "doc", "documentation",
            "release", "publish", "deploy",
            "benchmark", "benchmarks", "profiling",
            "examples", "demo",
        }
    )


# --------------------------------------------------------------------------- #
# Task 4 — fixed testability-scope policy: runtime + dev/test groups (minus
# docs/release denylist) + import-signalled feature extras.
# --------------------------------------------------------------------------- #
from python_deps.depgraph.roots import _in_test_scope


def _req(kind, source, name="x"):
    return SimpleNamespace(name=name, specifier="", marker="", extras=(), source=source, kind=kind)


def test_in_test_scope_runtime_always_in():
    assert _in_test_scope(_req("dependency", "pyproject.toml:project.dependencies"), frozenset())


def test_in_test_scope_feature_extra_gated_by_in_scope_extras():
    req = _req("optional_dependency", "pyproject.toml:project.optional-dependencies.http2")
    assert not _in_test_scope(req, frozenset())
    assert _in_test_scope(req, frozenset({"http2"}))


def test_in_test_scope_dev_group_default_in():
    for group in ("test", "tests", "lint", "typing", "dev"):
        req = _req("dev_group", f"pyproject.toml:dependency-groups.{group}")
        assert _in_test_scope(req, frozenset()), group


def test_in_test_scope_dev_group_docs_release_excluded():
    for group in ("docs", "documentation", "release", "publish", "benchmark"):
        req = _req("dev_group", f"pyproject.toml:dependency-groups.{group}")
        assert not _in_test_scope(req, frozenset()), group


def test_in_test_scope_denylist_is_case_insensitive():
    req = _req("dev_group", "requirements-file.DOCS")
    assert not _in_test_scope(req, frozenset())


def test_dependency_groups_test_becomes_root(tmp_path):
    _write(
        tmp_path / "proj",
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["flask"]

        [dependency-groups]
        test = ["pytest"]
        docs = ["sphinx"]
        """,
    )
    repo = tmp_path / "proj"
    graph = scan_to_nodes(str(repo))
    dists = {dist for _imp, dist in select_roots(str(repo), graph)}
    assert "flask" in dists          # runtime
    assert "pytest" in dists         # dev_group test -> in
    assert "sphinx" not in dists     # dev_group docs -> excluded


def test_used_extras_from_editable_puts_extra_in_scope(tmp_path):
    repo = tmp_path / "proj"
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["httpx"]

        [project.optional-dependencies]
        http2 = ["h2"]
        """,
    )
    _write(repo, "requirements.txt", "-e .[http2]\npytest\n")
    graph = scan_to_nodes(str(repo))
    dists = {dist for _imp, dist in select_roots(str(repo), graph)}
    assert "h2" in dists       # optional extra activated by -e .[http2]
    assert "pytest" in dists   # runtime line in requirements.txt


def test_optional_extra_not_signalled_stays_out(tmp_path):
    repo = tmp_path / "proj"
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["httpx"]

        [project.optional-dependencies]
        http2 = ["h2"]
        """,
    )
    graph = scan_to_nodes(str(repo))
    dists = {dist for _imp, dist in select_roots(str(repo), graph)}
    assert "h2" not in dists   # no signal -> feature extra stays gated


# --------------------------------------------------------------------------- #
# PEP 685 separator normalization — extras compared with only `.strip().lower()`
# treat `-`/`_`/`.` as distinct, so a `-e .[socks-extra]` signal fails to match
# a `[project.optional-dependencies]` group declared as `socks_extra`, silently
# dropping a declared, signalled extra from scope.
# --------------------------------------------------------------------------- #
def test_in_test_scope_extra_matches_across_separator_variants():
    # group is declared with an underscore; the signal uses a dash. PEP 685
    # treats these as the same extra and both sides must normalize to match.
    req = _req("optional_dependency", "pyproject.toml:project.optional-dependencies.socks_extra")
    assert _in_test_scope(req, frozenset({"socks-extra"}))


def test_dash_separator_extra_signal_matches_underscore_group(tmp_path):
    repo = tmp_path / "proj"
    _write(
        repo,
        "pyproject.toml",
        """
        [project]
        name = "proj"
        version = "0.1.0"
        dependencies = ["httpx"]

        [project.optional-dependencies]
        socks_extra = ["socksio"]
        """,
    )
    _write(repo, "requirements.txt", "-e .[socks-extra]\n")
    graph = scan_to_nodes(str(repo))
    dists = {dist for _imp, dist in select_roots(str(repo), graph)}
    assert "socksio" in dists  # dash-form signal must match underscore-form group
