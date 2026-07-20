# src/python_deps/depgraph/project_native_scan.py
"""Static native-build-surface scanner for the repo-under-test's OWN manifest.

Source #3 of the project-native-build-obligations design (R1
``docs/superpowers/research/R1-native-build-requirements.md`` §2.4/§2.5): reads
the ONE artifact every native-extension repo must have — its own build
declaration — to recover the linker libraries it needs even when the project
is unpublished / un-Debian-packaged / PEP-725-silent (closes the pygraphviz
stratum: Debian never packaged it, so §2.2/§2.3 structurally miss it, but its
``setup.py`` states ``libraries=["cdt", "cgraph", "gvc"]`` directly).

Host-side static parse only: ``setup.py`` is read via ``ast.parse``, NEVER
``exec``/``import`` (repos are untrusted input) — a walk for ``Extension`` /
``Pybind11Extension`` / ``CppExtension`` / ``cythonize`` call nodes, extracting
only the ``libraries=[...]`` keyword's string-literal elements. A computed
value (``libraries=get_libs()``, or any non-literal list element) is a silent
miss, never a wrong guess: recall bound, not a precision risk. ``pyproject.toml``
adds the newer ``[[tool.setuptools.ext-modules]]`` TOML schema (no AST needed).

Two entry points:

* ``scan_native_build_surface`` -> precise ``ObservedNeed(kind="linker_lib", ...)``
  list, funneled through the ALREADY ecosystem-neutral ``os_resolver.resolve()``
  (zero new resolver code — see os_resolver.py's ``linker_lib`` branches in
  ``filter_by_kind``/``rank``/``check_command_for``).
* ``has_native_build_signal`` -> coarse "this repo compiles something" bool
  (an ``Extension``/``cythonize`` call, a ``.pyx``/``.pxd`` file, or a
  Meson/CMake-backed build-backend) that drives §2.5's unconditional
  build-essential floor even when no specific library was statically
  extractable — precision is irrelevant there, only recall matters.
"""

from __future__ import annotations

import ast
import os

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

from python_deps.depgraph.os_resolver import ObservedNeed

# setuptools/pybind11/torch/Cython call names whose ``libraries=[...]`` keyword
# (or mere presence, for the coarse signal) marks a native build.
_NATIVE_CALL_NAMES: frozenset[str] = frozenset(
    {"Extension", "Pybind11Extension", "CppExtension", "cythonize"}
)

# Build-backend names that mean "definitely native, no per-library extraction
# attempted in v1" (CMake/Meson-driven; see R1 §2.4 point 2, deferred to a
# follow-up CMakeLists.txt/meson.build scanner).
_NATIVE_BUILD_BACKENDS: tuple[str, ...] = (
    "mesonpy", "meson-python", "scikit_build_core", "scikit-build-core",
)

# Directories never worth descending into for the coarse .pyx/.pxd sweep
# (vendored/third-party trees, VCS metadata, caches) — mirrors config_scan.py's
# exclusion shape.
_EXCLUDED_SEGMENTS: frozenset[str] = frozenset(
    {
        "build", "dist", ".git", ".hg", ".svn", "__pycache__",
        ".venv", "venv", "node_modules", ".tox", ".mypy_cache",
        ".pytest_cache", "site-packages",
    }
)


def _call_name(node: ast.Call) -> str | None:
    """Bare call name: ``Extension(...)`` -> ``"Extension"``; ``m.Pybind11Extension(...)``
    -> ``"Pybind11Extension"``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literal_str_list(value: ast.AST) -> list[str]:
    """String-literal elements of a ``[...]``/``(...)`` node; ``[]`` for anything
    else (a call, a name, a comprehension) — a computed ``libraries=`` value is a
    silent miss, never a wrong guess."""
    if not isinstance(value, (ast.List, ast.Tuple)):
        return []
    return [
        elt.value for elt in value.elts
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
    ]


def _read_setup_py(repo_path: str) -> ast.Module | None:
    """Parsed ``setup.py`` AST, or ``None`` on any absence/read/syntax failure.
    ``ast.parse`` only — never ``exec``/``import`` (repos are untrusted input)."""
    path = os.path.join(repo_path, "setup.py")
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError:
        return None
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def _has_native_call(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call) and _call_name(node) in _NATIVE_CALL_NAMES
        for node in ast.walk(tree)
    )


def _setup_py_library_names(tree: ast.Module) -> list[str]:
    """Order-preserving ``libraries=[...]`` literal names from every
    ``Extension``/``Pybind11Extension``/``CppExtension``/``cythonize`` call."""
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _NATIVE_CALL_NAMES:
            continue
        for kw in node.keywords:
            if kw.arg == "libraries":
                out.extend(_literal_str_list(kw.value))
    return out


def _load_pyproject(repo_path: str) -> dict:
    """Parsed ``pyproject.toml``, or ``{}`` on any absence/decode failure."""
    path = os.path.join(repo_path, "pyproject.toml")
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _pyproject_library_names(data: dict) -> list[str]:
    """Order-preserving ``libraries`` names from every
    ``[[tool.setuptools.ext-modules]]`` table."""
    tool = data.get("tool")
    setuptools_cfg = tool.get("setuptools") if isinstance(tool, dict) else None
    ext_modules = (
        setuptools_cfg.get("ext-modules") if isinstance(setuptools_cfg, dict) else None
    )
    if not isinstance(ext_modules, list):
        return []
    out: list[str] = []
    for mod in ext_modules:
        if not isinstance(mod, dict):
            continue
        libs = mod.get("libraries")
        if isinstance(libs, list):
            out.extend(lib for lib in libs if isinstance(lib, str))
    return out


def _has_native_build_backend(data: dict) -> bool:
    build_system = data.get("build-system")
    backend = build_system.get("build-backend") if isinstance(build_system, dict) else None
    if not isinstance(backend, str):
        return False
    return any(name in backend for name in _NATIVE_BUILD_BACKENDS)


def _has_pyx_file(repo_path: str) -> bool:
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d.lower() not in _EXCLUDED_SEGMENTS]
        if any(fname.endswith((".pyx", ".pxd")) for fname in filenames):
            return True
    return False


def scan_native_build_surface(repo_path: str) -> list[ObservedNeed]:
    """The repo-under-test's OWN declared linker libraries, as ``ObservedNeed``s.

    Reads ``setup.py`` (``Extension``/``Pybind11Extension``/``CppExtension``/
    ``cythonize`` calls' literal ``libraries=[...]``) and ``pyproject.toml``
    (``[[tool.setuptools.ext-modules]]``'s ``libraries``), de-duplicated by name
    in first-seen order (setup.py before pyproject.toml). ``[]`` when neither
    manifest is present/parseable or no literal ``libraries`` were declared —
    never a wrong guess, only a recall miss (matches the module docstring's
    computed-value contract). Each hit funnels straight through
    ``os_resolver.resolve()`` unchanged (``linker_lib`` is already
    ecosystem-neutral there).
    """
    ordered: list[tuple[str, str]] = []
    tree = _read_setup_py(repo_path)
    if tree is not None:
        ordered.extend(
            (name, "setup.py:Extension.libraries")
            for name in _setup_py_library_names(tree)
        )
    ordered.extend(
        (name, "pyproject.toml:tool.setuptools.ext-modules.libraries")
        for name in _pyproject_library_names(_load_pyproject(repo_path))
    )
    seen: set[str] = set()
    needs: list[ObservedNeed] = []
    for name, evidence in ordered:
        if name in seen:
            continue
        seen.add(name)
        needs.append(
            ObservedNeed(
                "linker_lib", name, context="build", strength="curated",
                evidence=evidence,
            )
        )
    return needs


def has_native_build_signal(repo_path: str) -> bool:
    """Coarse "this repo compiles something" signal — drives the §2.5
    unconditional build-essential floor even when no specific library was
    statically extractable.

    True iff ANY of: an ``Extension(...)``/``cythonize(...)`` (etc.) call in
    ``setup.py`` (regardless of whether its ``libraries=`` was a literal); any
    ``*.pyx``/``*.pxd`` file anywhere in the repo (Cython, even with no external
    lib); or a ``[build-system] build-backend`` naming a Meson/CMake-backed
    backend (``mesonpy``/``meson-python``/``scikit_build_core``/
    ``scikit-build-core``) in ``pyproject.toml``. ``False`` for a pure-Python
    repo, an absent/unparseable manifest, or garbage input — precision is
    irrelevant here (build-essential is always safe to add); only recall
    matters, so this is deliberately broader than ``scan_native_build_surface``.
    """
    tree = _read_setup_py(repo_path)
    if tree is not None and _has_native_call(tree):
        return True
    if _has_pyx_file(repo_path):
        return True
    return _has_native_build_backend(_load_pyproject(repo_path))
