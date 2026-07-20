"""TDD tests for the pkg_layer Contract plane + contract-only roots (Task 3).

``select_roots`` here is the "verifier" root selection: it consults ONLY the
declared contract, never imports. This is the structural fix for the
``depgraph.roots``/``package_roots`` bug where an optional dep excluded from
``needed_extras`` gets silently re-added because the code happens to import
it (scan gap-fill). A contract-only selector cannot do that -- there is no
import input at all.
"""
from __future__ import annotations

from python_deps.pkg_layer.contract import in_scope_deps, read_contract, select_roots
from python_deps.pkg_layer.planes import DeclaredDep


# --------------------------------------------------------------------------- #
# in_scope_deps: row-level scope, the single source of truth select_roots
# derives its name list from
# --------------------------------------------------------------------------- #

def test_in_scope_deps_excludes_row_declared_in_ungated_optional_group():
    base = DeclaredDep(
        name="widget", kind="dependency", specifier=None, marker=None, group=None
    )
    excluded_optional = DeclaredDep(
        name="widget", kind="optional_dependency", specifier=None, marker=None,
        group="extras",
    )
    contract = (base, excluded_optional)

    in_scope = in_scope_deps(contract, needed_extras=frozenset())

    # The base-dependency row is in scope; the excluded optional-group row
    # for the SAME distribution name must not leak in just because its name
    # also appears on an in-scope row (the scope-leak bug being fixed).
    assert base in in_scope
    assert excluded_optional not in in_scope
    assert in_scope == (base,)


def test_in_scope_deps_includes_row_declared_in_gated_optional_group():
    optional_dep = DeclaredDep(
        name="brotli", kind="optional_dependency", specifier=None, marker=None,
        group="speedups",
    )

    in_scope = in_scope_deps((optional_dep,), needed_extras=frozenset({"speedups"}))

    assert in_scope == (optional_dep,)


def test_in_scope_deps_empty_contract_returns_empty_tuple():
    assert in_scope_deps((), needed_extras=frozenset()) == ()


# --------------------------------------------------------------------------- #
# select_roots: pure membership logic over hand-built DeclaredDep tuples
# --------------------------------------------------------------------------- #

def test_runtime_dependency_always_included():
    contract = (
        DeclaredDep(name="requests", kind="dependency", specifier=">=2.0",
                    marker=None, group=None),
    )

    roots = select_roots(contract, needed_extras=frozenset())

    assert roots == ("requests",)


def test_optional_dependency_excluded_when_group_not_needed():
    contract = (
        DeclaredDep(name="brotli", kind="optional_dependency", specifier=None,
                    marker=None, group="speedups"),
    )

    roots = select_roots(contract, needed_extras=frozenset())

    assert "brotli" not in roots
    assert roots == ()


def test_optional_dependency_included_when_group_needed():
    contract = (
        DeclaredDep(name="brotli", kind="optional_dependency", specifier=None,
                    marker=None, group="speedups"),
    )

    roots = select_roots(contract, needed_extras=frozenset({"speedups"}))

    assert roots == ("brotli",)


def test_mixed_runtime_and_gated_optional_deps():
    contract = (
        DeclaredDep(name="requests", kind="dependency", specifier=None,
                    marker=None, group=None),
        DeclaredDep(name="brotli", kind="optional_dependency", specifier=None,
                    marker=None, group="speedups"),
        DeclaredDep(name="psycopg2", kind="optional_dependency", specifier=None,
                    marker=None, group="postgres"),
    )

    roots = select_roots(contract, needed_extras=frozenset({"postgres"}))

    assert set(roots) == {"requests", "psycopg2"}
    assert "brotli" not in roots


def test_canon_dedup_returns_canon_tokens():
    # Input spellings vary in case/separator; output must be the PEP 503 CANON
    # token (first-seen original spelling is NOT preserved) and deduped. The
    # assertion checks the raw returned value directly -- no re-canonicalizing
    # inside the assert, which would hide a "returns original spelling" bug.
    contract = (
        DeclaredDep(name="Flask", kind="dependency", specifier=">=2.0",
                    marker=None, group=None),
        DeclaredDep(name="flask", kind="dependency", specifier=None,
                    marker=None, group=None),
        DeclaredDep(name="Typing-Extensions", kind="dependency", specifier=None,
                    marker=None, group=None),
        DeclaredDep(name="typing_extensions", kind="dependency", specifier=None,
                    marker=None, group=None),
    )

    roots = select_roots(contract, needed_extras=frozenset())

    # Exact returned value: canon tokens, first-seen order, deduped.
    assert roots == ("flask", "typing-extensions")


def test_single_capitalized_dependency_returns_canon_token():
    # Guards bug #1 in isolation: a single "Flask" input must come out as the
    # canon "flask", never the original "Flask".
    contract = (
        DeclaredDep(name="Flask", kind="dependency", specifier=None,
                    marker=None, group=None),
    )

    assert select_roots(contract, needed_extras=frozenset()) == ("flask",)


def test_empty_contract_returns_empty_roots():
    assert select_roots((), needed_extras=frozenset()) == ()


def test_imports_are_never_consulted_key_bug_regression():
    """The bug this task fixes: the old ``package_roots`` gap-fill re-adds an
    optional dep whenever the code happens to import it, even when its extras
    group is not requested. ``select_roots`` here takes NO import argument at
    all -- structurally, there is nothing for an import to re-add. Assert a
    known optional-backend dist (``brotli``) stays absent with empty
    ``needed_extras`` regardless of how "used" it might be at runtime.
    """
    contract = (
        DeclaredDep(name="requests", kind="dependency", specifier=None,
                    marker=None, group=None),
        DeclaredDep(name="brotli", kind="optional_dependency", specifier=None,
                    marker=None, group="speedups"),
    )

    roots = select_roots(contract, needed_extras=frozenset())

    assert "brotli" not in roots
    assert roots == ("requests",)


# --------------------------------------------------------------------------- #
# read_contract: real evidence collection from a tmp_path pyproject.toml
# --------------------------------------------------------------------------- #

def _pyproject_with_optional_backend(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '  "requests>=2.0",\n'
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        'speedups = ["brotli"]\n'
        'postgres = ["psycopg2>=2.9"]\n'
    )
    return tmp_path


def test_read_contract_maps_runtime_dependency(tmp_path):
    _pyproject_with_optional_backend(tmp_path)

    contract = read_contract(str(tmp_path))

    by_name = {dep.name: dep for dep in contract}
    assert by_name["requests"].kind == "dependency"
    assert by_name["requests"].group is None


def test_read_contract_maps_optional_dependency_group(tmp_path):
    _pyproject_with_optional_backend(tmp_path)

    contract = read_contract(str(tmp_path))

    by_name = {dep.name: dep for dep in contract}
    assert by_name["brotli"].kind == "optional_dependency"
    assert by_name["brotli"].group == "speedups"
    assert by_name["psycopg2"].kind == "optional_dependency"
    assert by_name["psycopg2"].group == "postgres"


def test_read_contract_result_is_tuple_of_declared_dep(tmp_path):
    _pyproject_with_optional_backend(tmp_path)

    contract = read_contract(str(tmp_path))

    assert isinstance(contract, tuple)
    assert all(isinstance(dep, DeclaredDep) for dep in contract)


def test_read_contract_then_select_roots_end_to_end_excludes_ungated_optional(tmp_path):
    _pyproject_with_optional_backend(tmp_path)

    contract = read_contract(str(tmp_path))
    roots = select_roots(contract, needed_extras=frozenset())

    assert "brotli" not in roots
    assert "psycopg2" not in roots
    assert "requests" in roots


def test_read_contract_then_select_roots_end_to_end_includes_gated_optional(tmp_path):
    _pyproject_with_optional_backend(tmp_path)

    contract = read_contract(str(tmp_path))
    roots = select_roots(contract, needed_extras=frozenset({"postgres"}))

    assert "psycopg2" in roots
    assert "brotli" not in roots
    assert "requests" in roots


def test_read_contract_empty_repo_returns_empty_tuple(tmp_path):
    assert read_contract(str(tmp_path)) == ()
