# src/python_deps/pkg_layer/usage.py
"""Usage plane: context-tagged import scan — Task 2.

Turns a repo's imports into ``ImportNode``s tagged by CONTEXT (runtime / test
/ optional-guarded / typing / dynamic), so a later completeness check can
flag *runtime* under-declarations only, never crying wolf on guarded or
type-only imports.

Reuses ``python_deps.import_graph.scan_imports`` for the raw
external/stdlib/local classification and per-import provenance (does not
re-walk to redo that split). Runs its own focused, parent-aware AST pass over
the same repo to determine, per import SITE, the SET of applicable context
labels; the node's context is then the highest-precedence label across ALL of
an import's sites, using the total order
``RUNTIME > OPTIONAL > TYPING > TEST > DYNAMIC``.

Per-site applicable labels:
- ``OPTIONAL`` — the site is inside the ``try`` BODY of a try whose ``except``
  handles ``ImportError``/``ModuleNotFoundError``, or inside that
  ImportError-handling ``except`` handler body. NOT the ``else``/``finally``.
- ``TYPING`` — inside an ``if TYPE_CHECKING:`` block.
- ``TEST`` — the source file is a test file (``tests/`` | ``test/`` |
  ``conftest.py``).
- ``DYNAMIC`` — the site is an ``importlib.import_module("x")`` /
  ``__import__("x")`` STRING-LITERAL call.
- ``RUNTIME`` — a normal statically-executed import with none of
  ``OPTIONAL``/``TYPING``/``TEST`` applying. A dynamic-literal site is never
  ``RUNTIME``.
"""
from __future__ import annotations

import ast
import os
import sys
from collections import defaultdict
from pathlib import Path

from python_deps.import_graph import EXCLUDED_DIRS, scan_imports
from python_deps.pkg_layer.planes import ImportContext, ImportNode

# Highest-runtime-relevance-wins total order, most to least "real".
_PRECEDENCE: tuple[ImportContext, ...] = (
    ImportContext.RUNTIME,
    ImportContext.OPTIONAL,
    ImportContext.TYPING,
    ImportContext.TEST,
    ImportContext.DYNAMIC,
)

_GUARD_EXCEPTION_NAMES = frozenset({"ImportError", "ModuleNotFoundError"})
_TEST_DIR_NAMES = frozenset({"tests", "test"})


def scan_usage(repo_path: str) -> tuple[ImportNode, ...]:
    root = Path(repo_path)
    findings, project_local, _errors = scan_imports(root)
    external_provenance: dict[str, tuple[str, ...]] = {
        finding.import_name: finding.source_files
        for finding in findings
        if finding.classification == "external"
    }
    project_local_set = frozenset(project_local)

    labels_by_name: dict[str, set[ImportContext]] = defaultdict(set)
    dynamic_provenance: dict[str, set[str]] = defaultdict(set)

    for python_file in _iter_python_files(root):
        relative_path = python_file.relative_to(root).as_posix()
        tree = _parse(python_file)
        if tree is None:
            continue

        static_sites, dynamic_sites = _collect_sites(tree)
        is_test_file = _is_test_path(relative_path)

        for name, guards in static_sites:
            if name not in external_provenance:
                continue
            labels_by_name[name] |= _site_labels(guards, is_test_file, dynamic=False)

        for name, guards in dynamic_sites:
            # scan_imports never sees dynamic-literal sites (it only walks
            # ast.Import/ImportFrom), so classify the literal name ourselves
            # using scan_imports' own project_local set + the same stdlib
            # authority the rest of the codebase uses (sys.stdlib_module_names,
            # e.g. depgraph/roots.py) so it cannot drift from scan_imports.
            if name in project_local_set or name in sys.stdlib_module_names:
                continue
            labels_by_name[name] |= _site_labels(guards, is_test_file, dynamic=True)
            if name not in external_provenance:
                dynamic_provenance[name].add(relative_path)

    nodes = tuple(
        ImportNode(
            name=name,
            context=_strongest(labels),
            provenance=external_provenance.get(name)
            or tuple(sorted(dynamic_provenance[name])),
        )
        for name, labels in sorted(labels_by_name.items())
    )
    return nodes


def _site_labels(
    guards: frozenset[ImportContext], is_test_file: bool, *, dynamic: bool
) -> set[ImportContext]:
    """SET of context labels applicable to a single import site.

    ``guards`` carries the lexically-active guard labels (subset of
    ``{OPTIONAL, TYPING}``). RUNTIME is the pure fallback for a static site
    with no other label; a dynamic site is never RUNTIME.
    """
    labels: set[ImportContext] = set(guards)
    if is_test_file:
        labels.add(ImportContext.TEST)
    if dynamic:
        labels.add(ImportContext.DYNAMIC)
    elif not labels:
        labels.add(ImportContext.RUNTIME)
    return labels


def _strongest(labels: set[ImportContext]) -> ImportContext:
    for context in _PRECEDENCE:
        if context in labels:
            return context
    raise AssertionError(f"unreachable: empty label set {labels!r}")


def _is_test_path(relative_path: str) -> bool:
    path = Path(relative_path)
    if path.name == "conftest.py":
        return True
    return any(part in _TEST_DIR_NAMES for part in path.parts[:-1])


def _iter_python_files(root: Path):
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in EXCLUDED_DIRS and not directory.startswith(".")
        ]
        for filename in files:
            if filename.endswith(".py"):
                yield Path(current_root) / filename


def _parse(python_file: Path) -> ast.Module | None:
    try:
        content = python_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            content = python_file.read_text(encoding="latin-1")
        except OSError:
            return None
    except OSError:
        return None
    try:
        return ast.parse(content)
    except SyntaxError:
        return None


# Each site is recorded as (top_level_name, active_guard_labels), where the
# guard labels are a subset of {OPTIONAL, TYPING} carried down the recursive
# walk. TEST/DYNAMIC/RUNTIME are decided later (file-level / site-kind).
_Sites = list[tuple[str, frozenset[ImportContext]]]


def _collect_sites(tree: ast.Module) -> tuple[_Sites, _Sites]:
    static_sites: _Sites = []
    dynamic_sites: _Sites = []
    importlib_names, import_module_names = _importlib_bindings(tree)

    def visit(node: ast.AST, guards: frozenset[ImportContext]) -> None:
        if isinstance(node, ast.Try):
            _visit_try(node, guards)
            return
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            body_guards = guards | {ImportContext.TYPING}
            for child in node.body:
                visit(child, body_guards)
            for child in node.orelse:
                visit(child, guards)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                static_sites.append((alias.name.split(".", 1)[0], guards))
            return
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                static_sites.append((node.module.split(".", 1)[0], guards))
            return
        if isinstance(node, ast.Call):
            literal = _dynamic_import_literal(node, importlib_names, import_module_names)
            if literal is not None:
                dynamic_sites.append((literal.split(".", 1)[0], guards))
        for child in ast.iter_child_nodes(node):
            visit(child, guards)

    def _visit_try(node: ast.Try, guards: frozenset[ImportContext]) -> None:
        # OPTIONAL applies to the try body and to any ImportError-handling
        # except handler body — NOT to else/finally.
        handles_import_error = any(
            _exception_type_matches(handler.type) for handler in node.handlers
        )
        body_guards = (
            guards | {ImportContext.OPTIONAL} if handles_import_error else guards
        )
        for child in node.body:
            visit(child, body_guards)
        for handler in node.handlers:
            handler_guards = (
                guards | {ImportContext.OPTIONAL}
                if _exception_type_matches(handler.type)
                else guards
            )
            for child in handler.body:
                visit(child, handler_guards)
        for child in node.orelse:  # else: — statically executed, not optional
            visit(child, guards)
        for child in node.finalbody:  # finally: — always runs, not optional
            visit(child, guards)

    visit(tree, frozenset())
    return static_sites, dynamic_sites


def _exception_type_matches(exc_type: ast.expr | None) -> bool:
    if exc_type is None:  # bare `except:` — not an ImportError guard
        return False
    if isinstance(exc_type, ast.Tuple):
        return any(_exception_type_matches(element) for element in exc_type.elts)
    if isinstance(exc_type, ast.Name):
        return exc_type.id in _GUARD_EXCEPTION_NAMES
    if isinstance(exc_type, ast.Attribute):
        return exc_type.attr in _GUARD_EXCEPTION_NAMES
    return False


def _is_type_checking_test(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _importlib_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Names in this file that genuinely refer to importlib's dynamic-import API.

    Returns ``(importlib_module_names, import_module_func_names)``:
    - ``importlib_module_names`` — names bound to the importlib MODULE, so that
      ``<name>.import_module(...)`` is genuinely importlib's (from
      ``import importlib`` / ``import importlib as X`` / ``import importlib.sub``).
    - ``import_module_func_names`` — names bound directly to
      ``importlib.import_module`` (from ``from importlib import import_module``
      / ``... as im``), so a bare ``<name>(...)`` is genuinely dynamic.

    A bare ``import_module(...)`` with no such binding (e.g. a user-defined
    ``def import_module``) is therefore NOT treated as a dynamic import.
    """
    module_names: set[str] = set()
    func_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    module_names.add(alias.asname or "importlib")
                elif alias.name.startswith("importlib.") and alias.asname is None:
                    # `import importlib.util` also binds the top-level name
                    # `importlib`, making `importlib.import_module` reachable.
                    module_names.add("importlib")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "importlib":
                for alias in node.names:
                    if alias.name == "import_module":
                        func_names.add(alias.asname or "import_module")
    return frozenset(module_names), frozenset(func_names)


def _dynamic_import_literal(
    call: ast.Call,
    importlib_names: frozenset[str],
    import_module_names: frozenset[str],
) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        # `<importlib>.import_module("literal")` — receiver must be importlib.
        matched = func.attr == "import_module" and (
            isinstance(func.value, ast.Name) and func.value.id in importlib_names
        )
    elif isinstance(func, ast.Name):
        # `__import__("literal")` (builtin) or a name bound from importlib.
        matched = func.id == "__import__" or func.id in import_module_names
    else:
        matched = False
    if not matched:
        return None
    if not call.args:
        return None
    first_arg = call.args[0]
    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
        return first_arg.value
    return None
