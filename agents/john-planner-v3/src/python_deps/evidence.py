from __future__ import annotations

import ast
import configparser
import glob
import os
import re
from pathlib import Path
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement

from .import_graph import collect_pydeps_evidence, scan_imports
from .import_mapping import is_unresolved, map_import_to_package, normalize_package_name
from .models import (
    ImportPackageMapping,
    PythonDependencyEvidence,
    PythonRequirement,
    PythonVersionRequirement,
)

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 — fall back to the tomli backport.
    # WITHOUT this fallback the whole declared-dependency reader silently returns
    # ZERO requirements on a <3.11 interpreter (e.g. the 3.10 benchmark box):
    # `tomllib is None` -> pyproject parsing is skipped -> select_roots gets no
    # roots -> the entire declared closure (runtime AND dev/test groups) vanishes.
    # Every other tomllib site in this codebase already does this; evidence.py was
    # the lone exception. tomli is declared in requirements.txt for python<3.11.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - neither parser available
        tomllib = None


def collect_python_dependency_evidence(repo_path: str | Path) -> PythonDependencyEvidence:
    root = Path(repo_path)
    evidence = PythonDependencyEvidence(repo_path=str(root))

    collectors = (
        _collect_pyproject_metadata,
        _collect_dependency_groups,
        _collect_setup_cfg_metadata,
        _collect_setup_py_metadata,
        _collect_requirements_files,
        _collect_constraints_files,
    )
    for collector in collectors:
        try:
            collector(root, evidence)
        except Exception as error:  # Evidence collection must not abort an agent run.
            evidence.collection_errors.append(f"{collector.__name__}: {error}")

    try:
        imports, project_local_modules, import_errors = scan_imports(root)
        evidence.imports.extend(imports)
        evidence.project_local_modules.extend(project_local_modules)
        evidence.collection_errors.extend(import_errors)
    except Exception as error:
        evidence.collection_errors.append(f"scan_imports: {error}")

    evidence.pydeps = collect_pydeps_evidence(root)
    evidence.import_package_mappings.extend(_build_import_mappings(evidence))
    return evidence


def _collect_pyproject_metadata(root: Path, evidence: PythonDependencyEvidence) -> None:
    path = root / "pyproject.toml"
    if not path.is_file() or tomllib is None:
        return
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    requires_python = project.get("requires-python")
    if isinstance(requires_python, str) and requires_python.strip():
        evidence.python_requires.append(
            PythonVersionRequirement(
                specifier=requires_python.strip(),
                source="pyproject.toml:project.requires-python",
            )
        )
    for requirement in project.get("dependencies", []) or []:
        _add_requirement_line(evidence.declared_dependencies, requirement, "pyproject.toml:project.dependencies")
    optional_dependencies = project.get("optional-dependencies", {}) or {}
    if isinstance(optional_dependencies, dict):
        for group, requirements in optional_dependencies.items():
            for requirement in requirements or []:
                _add_requirement_line(
                    evidence.declared_dependencies,
                    requirement,
                    f"pyproject.toml:project.optional-dependencies.{group}",
                    kind="optional_dependency",
                    trust="medium",
                )

    poetry_dependencies = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    if isinstance(poetry_dependencies, dict):
        for name, value in poetry_dependencies.items():
            if name.lower() == "python":
                if isinstance(value, str):
                    evidence.python_requires.append(
                        PythonVersionRequirement(
                            specifier=value,
                            source="pyproject.toml:tool.poetry.dependencies.python",
                        )
                    )
                continue
            specifier = value if isinstance(value, str) else ""
            evidence.declared_dependencies.append(
                PythonRequirement(
                    name=name,
                    specifier=specifier,
                    source="pyproject.toml:tool.poetry.dependencies",
                )
            )


def _collect_dependency_groups(root: Path, evidence: PythonDependencyEvidence) -> None:
    """PEP 735 ``[dependency-groups]`` reader.

    Each group maps to a list whose members are requirement strings and/or
    ``{include-group = "<name>"}`` reference objects. include-group references are
    resolved transitively (a group may include another group) with cycle
    detection; the flattened requirements are attributed to the TOP-LEVEL group
    being expanded and tagged ``kind="dev_group"``.
    """
    path = root / "pyproject.toml"
    if not path.is_file() or tomllib is None:
        return
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    groups = data.get("dependency-groups", {})
    if not isinstance(groups, dict):
        return
    for group_name in groups:
        if not isinstance(group_name, str):
            continue
        requirements, cycle = _resolve_dependency_group(group_name, groups, ())
        if cycle:
            evidence.collection_errors.append(
                f"_collect_dependency_groups: include-group cycle involving '{group_name}'"
            )
        for requirement in requirements:
            _add_requirement_line(
                evidence.declared_dependencies,
                requirement,
                f"pyproject.toml:dependency-groups.{group_name}",
                kind="dev_group",
                trust="medium",
            )


def _resolve_dependency_group(
    name: str, groups: dict, seen: tuple[str, ...]
) -> tuple[list[str], bool]:
    """Flatten a dependency-group's members to concrete requirement strings.

    Returns ``(requirement_strings, cycle_detected)``. ``include-group`` refs are
    expanded depth-first; a group already on the current ``seen`` path is a cycle:
    its expansion is truncated (skipped) and ``cycle_detected`` is set True.
    """
    if name in seen:
        return [], True
    members = groups.get(name)
    if not isinstance(members, list):
        return [], False
    out: list[str] = []
    cycle = False
    for member in members:
        if isinstance(member, str):
            out.append(member)
        elif isinstance(member, dict) and isinstance(member.get("include-group"), str):
            sub, sub_cycle = _resolve_dependency_group(member["include-group"], groups, seen + (name,))
            out.extend(sub)
            cycle = cycle or sub_cycle
    return out, cycle


def _collect_setup_cfg_metadata(root: Path, evidence: PythonDependencyEvidence) -> None:
    path = root / "setup.cfg"
    if not path.is_file():
        return
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if parser.has_option("options", "python_requires"):
        evidence.python_requires.append(
            PythonVersionRequirement(
                specifier=parser.get("options", "python_requires").strip(),
                source="setup.cfg:options.python_requires",
            )
        )
    if parser.has_option("options", "install_requires"):
        for line in _split_multiline_value(parser.get("options", "install_requires")):
            _add_requirement_line(evidence.declared_dependencies, line, "setup.cfg:options.install_requires")
    if parser.has_section("options.extras_require"):
        for group, value in parser.items("options.extras_require"):
            for line in _split_multiline_value(value):
                _add_requirement_line(
                    evidence.declared_dependencies,
                    line,
                    f"setup.cfg:options.extras_require.{group}",
                    kind="optional_dependency",
                    trust="medium",
                )


def _collect_setup_py_metadata(root: Path, evidence: PythonDependencyEvidence) -> None:
    path = root / "setup.py"
    if not path.is_file():
        return
    content = path.read_text(encoding="utf-8")
    if len(content) > 250_000:
        evidence.collection_errors.append("setup.py: skipped metadata parse because file is too large")
        return
    try:
        tree = ast.parse(content)
    except SyntaxError as error:
        evidence.collection_errors.append(f"setup.py: syntax error while parsing metadata: {error}")
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if func_name != "setup":
            continue
        for keyword in node.keywords:
            if keyword.arg == "python_requires":
                value = _literal_string(keyword.value)
                if value:
                    evidence.python_requires.append(
                        PythonVersionRequirement(
                            specifier=value,
                            source="setup.py:setup.python_requires",
                        )
                    )
            elif keyword.arg == "install_requires":
                for requirement in _literal_string_list(keyword.value):
                    _add_requirement_line(
                        evidence.declared_dependencies,
                        requirement,
                        "setup.py:setup.install_requires",
                    )
            elif keyword.arg == "extras_require":
                for group, requirements in _literal_extras_require(keyword.value).items():
                    for requirement in requirements:
                        _add_requirement_line(
                            evidence.declared_dependencies,
                            requirement,
                            f"setup.py:setup.extras_require.{group}",
                            kind="optional_dependency",
                            trust="medium",
                        )


# Editable self-install with extras: ``-e .[http2,socks]`` / ``--editable .[...]``.
_EDITABLE_SELF_EXTRAS_RE = re.compile(r"^(?:-e|--editable)\s+\.\s*\[([^\]]*)\]\s*$")
# Include directives: ``-r other.txt`` / ``--requirement other.txt`` (deps) and
# ``-c other.txt`` / ``--constraint other.txt`` (constraints). Optional ``=``.
_INCLUDE_RE = re.compile(r"^(-r|--requirement|-c|--constraint)\s*=?\s*(\S+)")
_MAX_INCLUDE_DEPTH = 5


def _collect_requirements_files(root: Path, evidence: PythonDependencyEvidence) -> None:
    visited: set[Path] = set()
    for path in _discover_requirements_files(root):
        _ingest_requirements_file(root, path, evidence, visited, depth=0)


def _discover_requirements_files(root: Path) -> list[Path]:
    """Root-level ``requirements*.txt`` plus files in allowlisted nested dirs.

    Bounded: only ``requirements/`` (all ``*.txt``) and ``tests/``, ``test/``,
    ``docs/`` (only ``*requirements*.txt``) — never a full-tree walk.
    """
    found: set[Path] = set(_glob_metadata_files(root, "requirements*.txt"))
    for sub in ("requirements", "tests", "test", "docs"):
        directory = root / sub
        if not directory.is_dir():
            continue
        for candidate in sorted(directory.glob("*.txt")):
            if not candidate.is_file():
                continue
            if sub == "requirements" or "requirement" in candidate.name.lower():
                found.add(candidate)
    return sorted(found)


def _requirements_role(root: Path, path: Path) -> str:
    """Role for a requirements file from its dir/basename tokens.

    Returns one of ``"docs"``, ``"test"``, ``"dev"``, ``"runtime"`` (checked in
    that precedence). Token/segment matching (not raw substring) keeps false
    positives low.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    dir_segments = {segment.lower() for segment in relative.parts[:-1]}
    stem_tokens = {tok for tok in re.split(r"[-_.]", path.stem.lower()) if tok}
    docs_markers = {"docs", "doc", "documentation"}
    test_markers = {"test", "tests", "testing"}
    if stem_tokens & docs_markers or dir_segments & docs_markers:
        return "docs"
    if stem_tokens & test_markers or dir_segments & test_markers:
        return "test"
    if "dev" in stem_tokens or "dev" in dir_segments:
        return "dev"
    return "runtime"


def _role_kind_source(role: str, root: Path, path: Path) -> tuple[str, str]:
    """Map a requirements-file role to ``(kind, source)`` for its rows."""
    if role == "runtime":
        return "dependency", _relative_source(root, path)
    return "dev_group", f"requirements-file.{role}"


def _ingest_requirements_file(
    root: Path,
    path: Path,
    evidence: PythonDependencyEvidence,
    visited: set[Path],
    depth: int,
) -> None:
    resolved = path.resolve()
    if depth > _MAX_INCLUDE_DEPTH or resolved in visited or not resolved.is_file():
        return
    visited.add(resolved)
    role = _requirements_role(root, resolved)
    kind, source = _role_kind_source(role, root, resolved)
    for raw_line in _iter_raw_requirement_lines(resolved):
        line = _strip_inline_comment(raw_line).strip()
        if not line:
            continue
        editable = _EDITABLE_SELF_EXTRAS_RE.match(line)
        if editable:
            for extra in editable.group(1).split(","):
                stripped = extra.strip()
                if stripped:
                    # PEP 685: normalize separators (-/_/.) the same way as
                    # distribution names so `-e .[socks-extra]` matches an
                    # optional-dependencies group declared `socks_extra`.
                    evidence.used_extras.add(normalize_package_name(stripped))
            continue
        include = _INCLUDE_RE.match(line)
        if include:
            target = (resolved.parent / include.group(2)).resolve()
            if include.group(1) in ("-c", "--constraint"):
                for constraint_line in _read_requirement_lines(target) if target.is_file() else ():
                    _add_requirement_line(
                        evidence.constraint_dependencies,
                        constraint_line,
                        _relative_source(root, target),
                        kind="constraint",
                    )
            else:
                _ingest_requirements_file(root, target, evidence, visited, depth + 1)
            continue
        if line.startswith("-"):
            # any other option / editable form (``-i``, ``--hash``, bare ``-e .``,
            # ``-e <url>``) — ignored, matching prior behavior.
            continue
        _add_requirement_line(evidence.declared_dependencies, line, source, kind=kind)


def _iter_raw_requirement_lines(path: Path) -> Iterable[str]:
    """Yield every non-empty line (INCLUDING ``-``-prefixed directives)."""
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if stripped:
            yield raw_line


def _collect_constraints_files(root: Path, evidence: PythonDependencyEvidence) -> None:
    for path in _glob_metadata_files(root, "constraints*.txt"):
        for line in _read_requirement_lines(path):
            _add_requirement_line(
                evidence.constraint_dependencies,
                line,
                _relative_source(root, path),
                kind="constraint",
            )


def _build_import_mappings(evidence: PythonDependencyEvidence) -> list[ImportPackageMapping]:
    declared_package_names = {
        requirement.name for requirement in evidence.declared_dependencies if requirement.name
    }
    mappings: list[ImportPackageMapping] = []
    for finding in evidence.imports:
        if finding.classification != "external":
            continue
        mapping = map_import_to_package(finding.import_name, declared_package_names)
        if is_unresolved(mapping):
            continue
        mappings.append(
            ImportPackageMapping(
                import_name=mapping.import_name,
                package_name=mapping.package_name,
                source=mapping.source,
                trust=mapping.trust,
            )
        )
    return sorted(mappings, key=lambda item: item.import_name)


def _glob_metadata_files(root: Path, pattern: str) -> list[Path]:
    matches = [
        Path(path)
        for path in glob.glob(str(root / pattern))
        if Path(path).is_file()
    ]
    return sorted(matches)


def _read_requirement_lines(path: Path) -> Iterable[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="latin-1")
    for raw_line in content.splitlines():
        line = _strip_inline_comment(raw_line).strip()
        if not line or line.startswith(("-", "--")):
            continue
        yield line


def _add_requirement_line(
    target: list[PythonRequirement],
    line: object,
    source: str,
    *,
    kind: str = "dependency",
    trust: str = "high",
) -> None:
    if not isinstance(line, str):
        return
    parsed = _parse_requirement_line(line)
    if not parsed:
        return
    name, specifier, marker, extras = parsed
    target.append(
        PythonRequirement(
            name=name,
            specifier=specifier,
            marker=marker,
            extras=extras,
            source=source,
            kind=kind,
            trust=trust,
        )
    )


def _parse_requirement_line(line: str) -> tuple[str, str, str, tuple[str, ...]] | None:
    cleaned = _strip_inline_comment(line).strip()
    if not cleaned or cleaned.startswith(("-", "--")):
        return None
    if "://" in cleaned or cleaned.startswith(("git+", "hg+", "svn+")):
        egg_match = re.search(r"[#&]egg=([A-Za-z0-9_.-]+)", cleaned)
        if egg_match:
            return egg_match.group(1), cleaned, "", ()
        return None
    try:
        requirement = Requirement(cleaned)
    except InvalidRequirement:
        return None
    marker = str(requirement.marker) if requirement.marker is not None else ""
    extras = tuple(sorted(requirement.extras))
    return requirement.name, str(requirement.specifier), marker, extras


def _strip_inline_comment(line: str) -> str:
    if " #" not in line:
        return line
    return line.split(" #", 1)[0]


def _split_multiline_value(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _literal_string(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _literal_string_list(node: ast.AST) -> list[str]:
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _literal_extras_require(node: ast.AST) -> dict[str, list[str]]:
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for group, requirements in value.items():
        if not isinstance(group, str):
            continue
        if isinstance(requirements, str):
            result[group] = [requirements]
        elif isinstance(requirements, (list, tuple)):
            result[group] = [item for item in requirements if isinstance(item, str)]
    return result


def _relative_source(root: Path, path: Path) -> str:
    """Repo-relative provenance path, resolved on BOTH sides.

    ``path`` is frequently pre-resolved by the caller (e.g.
    ``_ingest_requirements_file`` resolves for cycle detection) while ``root``
    is left as given. On a repo root that resolves through a symlink (macOS
    ``/var`` -> ``/private/var`` is the common case), comparing a resolved
    ``path`` against an unresolved ``root`` makes ``os.path.relpath`` walk up
    through the divergent prefix and emit garbage like
    ``../../../../private/var/.../requirements.txt`` instead of the intended
    ``requirements.txt``. Resolving both sides here keeps this function
    correct regardless of what the caller already resolved.
    """
    return os.path.relpath(Path(path).resolve(), Path(root).resolve())
