from __future__ import annotations

import ast
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from .models import ImportFinding


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
    "vendor",
}

SOURCE_ROOT_NAMES = {"src", "lib", "python"}
MAX_PYTHON_FILES = 1000
MAX_FILE_BYTES = 500_000


def collect_project_local_modules(repo_path: str | Path) -> list[str]:
    root = Path(repo_path)
    module_names: set[str] = set()
    for source_root in _candidate_source_roots(root):
        if not source_root.is_dir():
            continue
        for child in source_root.iterdir():
            if child.name.startswith(".") or child.name in EXCLUDED_DIRS:
                continue
            if child.is_file() and child.suffix == ".py":
                module_names.add(child.stem)
            elif child.is_dir() and _looks_like_python_module_dir(child):
                module_names.add(child.name)
    return sorted(module_names)


def scan_imports(repo_path: str | Path) -> tuple[list[ImportFinding], list[str], list[str]]:
    root = Path(repo_path)
    project_local_modules = set(collect_project_local_modules(root))
    stdlib_modules = _stdlib_module_names()
    imports_by_name: dict[str, set[str]] = defaultdict(set)
    # Cross-file optionality tracking: a name is optional only if EVERY occurrence
    # is guarded (i.e. it never appears as a hard, unguarded import anywhere).
    hard_import_names: set[str] = set()
    optional_import_names: set[str] = set()
    errors: list[str] = []

    for python_file in _iter_python_files(root):
        relative_path = python_file.relative_to(root).as_posix()
        try:
            content = python_file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = python_file.read_text(encoding="latin-1")
            except OSError as error:
                errors.append(f"{relative_path}: {error}")
                continue
        except OSError as error:
            errors.append(f"{relative_path}: {error}")
            continue

        try:
            all_names, optional_names = _imports_from_ast(content)
        except SyntaxError:
            # No AST context in the regex fallback, so never emit a false
            # optional tag: every recovered import counts as a hard need.
            all_names = _imports_from_regex(content)
            optional_names = set()
            errors.append(f"{relative_path}: syntax error; used regex import fallback")

        for import_name in all_names:
            imports_by_name[import_name].add(relative_path)
        optional_import_names |= optional_names
        hard_import_names |= all_names - optional_names

    findings = []
    for import_name, source_files in sorted(imports_by_name.items()):
        top_level = import_name.split(".", 1)[0]
        if top_level in project_local_modules:
            classification = "project_local"
        elif top_level in stdlib_modules:
            classification = "stdlib"
        else:
            classification = "external"
        optional = import_name in optional_import_names and import_name not in hard_import_names
        findings.append(
            ImportFinding(
                import_name=top_level,
                classification=classification,
                source_files=tuple(sorted(source_files)),
                optional=optional,
            )
        )

    deduped = _dedupe_findings(findings)
    return deduped, sorted(project_local_modules), errors


def collect_pydeps_evidence(repo_path: str | Path) -> dict[str, object]:
    """Record pydeps availability without making it required for Phase 1."""
    executable = shutil.which("pydeps")
    if not executable:
        return {
            "available": False,
            "ran": False,
            "reason": "pydeps executable not found",
        }
    return {
        "available": True,
        "ran": False,
        "reason": "Phase 1 uses AST scanning as primary evidence; pydeps is optional supplemental evidence",
        "executable": executable,
    }


def _candidate_source_roots(root: Path) -> list[Path]:
    roots = [root]
    for name in SOURCE_ROOT_NAMES:
        candidate = root / name
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _looks_like_python_module_dir(path: Path) -> bool:
    if (path / "__init__.py").is_file():
        return True
    try:
        return any(child.suffix == ".py" for child in path.iterdir() if child.is_file())
    except OSError:
        return False


def _iter_python_files(root: Path):
    yielded = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in EXCLUDED_DIRS and not directory.startswith(".")
        ]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = Path(current_root) / filename
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield path
            yielded += 1
            if yielded >= MAX_PYTHON_FILES:
                return


# Except-handler identifiers that mark a guarded import "optional": the
# ImportError family, the broad ``Exception`` base, or a bare ``except:``.
_OPTIONAL_HANDLER_NAMES = frozenset({"ImportError", "ModuleNotFoundError", "Exception"})


def _import_node_top_level_names(node: ast.AST) -> set[str]:
    """Top-level module names contributed by a single Import / ImportFrom node.

    Mirrors the historical scan behaviour: ``import a.b`` -> ``a``; relative
    ``from . import x`` (``node.level`` set) contributes nothing."""
    names: set[str] = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.name.split(".", 1)[0])
    elif isinstance(node, ast.ImportFrom):
        if node.level:
            return names
        if node.module:
            names.add(node.module.split(".", 1)[0])
    return names


def _handler_leaf_names(expr: ast.expr) -> set[str]:
    """Identifier leaves of an ``except`` handler type expression.

    ``ImportError`` (Name) -> ``{"ImportError"}``; ``builtins.ImportError``
    (Attribute) uses the trailing attribute; ``(ImportError, OSError)`` (Tuple)
    contributes every element. Anything else contributes nothing (conservative:
    unrecognised shapes are treated as non-guarding)."""
    names: set[str] = set()
    targets = expr.elts if isinstance(expr, ast.Tuple) else [expr]
    for target in targets:
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def _try_guards_imports(node: ast.Try) -> bool:
    """True when any handler makes the try body's imports optional: a bare
    ``except:``, or one catching ImportError / ModuleNotFoundError / Exception.

    Conservative on ambiguity — a narrower handler (e.g. ``except ValueError``)
    does NOT count, and an unrecognised handler shape is ignored. A tuple that
    *includes* an ImportError-family / Exception name DOES count (that error is
    caught)."""
    for handler in node.handlers:
        if handler.type is None:  # bare ``except:`` catches everything
            return True
        if _handler_leaf_names(handler.type) & _OPTIONAL_HANDLER_NAMES:
            return True
    return False


def _guarded_import_ids(tree: ast.AST) -> set[int]:
    """Ids of Import/ImportFrom nodes that are *conditionally* executed, so a name
    imported ONLY at such a site is optional rather than a hard runtime need.

    Two guard shapes qualify — both env/availability conditionals whose real
    authority is the resolver's PEP 508 marker evaluation against the TARGET, not
    this static scan (which runs before the target is even detected):

    * a ``try`` body guarded by an ImportError-family / bare / ``Exception``
      handler — the import may be ABSENT and the code handles it; and
    * either branch of an ``if`` (``body`` and ``orelse``, so ``elif``/``else``
      too) — the import runs only under a condition
      (``if sys.version_info < (3, 11):`` -> ``exceptiongroup``/``tomli``,
      ``if sys.platform == 'win32':`` -> ``winloop``/``colorama``,
      ``if TYPE_CHECKING:`` -> a type-only import, …).

    The ``if`` predicate is deliberately NOT inspected: ANY conditional branch
    qualifies. Enumerating predicates (``sys.version_info``, ``sys.platform``, …)
    would be whack-a-mole — the next guard form (``os.name``,
    ``platform.system()``, a feature flag) would slip through as a hard import the
    Phase-A audit then wrongly re-adds as a root even though the resolver
    correctly marker-pruned its declared, target-excluded provider.

    Returns node identities only; the dominance rule (a name that ALSO appears
    unguarded stays hard) is applied by the caller via ``guarded_names -
    hard_names``.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and _try_guards_imports(node):
            branches = (node.body,)
        elif isinstance(node, ast.If):
            branches = (node.body, node.orelse)
        else:
            continue
        for branch in branches:
            for stmt in branch:
                for inner in ast.walk(stmt):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        ids.add(id(inner))
    return ids


def _imports_from_ast(content: str) -> tuple[set[str], set[str]]:
    """Return ``(all_top_level_names, optional_names)`` for a source file.

    ``optional_names`` are the names imported *only* under a conditional guard —
    an ImportError-family / bare / ``Exception``-guarded ``try`` body, or either
    branch of an ``if`` (see :func:`_guarded_import_ids`). A name imported both
    under such a guard and unguarded (a hard need) is NOT optional — the hard
    occurrence dominates within this file."""
    tree = ast.parse(content)

    # Import/ImportFrom nodes (by identity) that live under a conditional guard.
    guarded_node_ids = _guarded_import_ids(tree)

    all_names: set[str] = set()
    hard_names: set[str] = set()
    guarded_names: set[str] = set()
    for node in ast.walk(tree):
        names = _import_node_top_level_names(node)
        if not names:
            continue
        all_names |= names
        (guarded_names if id(node) in guarded_node_ids else hard_names).update(names)

    # Within this file a name is optional only if it never appears unguarded.
    return all_names, guarded_names - hard_names


def _imports_from_regex(content: str) -> set[str]:
    imports: set[str] = set()
    for line in content.splitlines():
        import_match = re.match(r"^\s*import\s+([A-Za-z_][\w.]*)", line)
        if import_match:
            imports.add(import_match.group(1).split(".", 1)[0])
            continue
        from_match = re.match(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b", line)
        if from_match:
            imports.add(from_match.group(1).split(".", 1)[0])
    return imports


def _stdlib_module_names() -> set[str]:
    modules = set(getattr(sys, "stdlib_module_names", set()))
    modules.update(sys.builtin_module_names)
    modules.update({"typing", "pathlib", "dataclasses", "unittest"})
    return modules


def _dedupe_findings(findings: list[ImportFinding]) -> list[ImportFinding]:
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    # Dominance: a name is optional only if it is optional in ALL findings for it
    # (a hard runtime need dominates a guarded one).
    optional: dict[tuple[str, str], bool] = {}
    for finding in findings:
        key = (finding.import_name, finding.classification)
        grouped[key].update(finding.source_files)
        optional[key] = finding.optional if key not in optional else optional[key] and finding.optional
    return [
        ImportFinding(
            import_name=name,
            classification=classification,
            source_files=tuple(sorted(files)),
            optional=optional[(name, classification)],
        )
        for (name, classification), files in sorted(grouped.items())
    ]
