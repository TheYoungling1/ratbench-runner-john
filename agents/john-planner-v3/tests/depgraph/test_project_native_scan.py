# tests/depgraph/test_project_native_scan.py
"""Tests for project_native_scan.py — pure static native-build-surface scanner.

No Docker, no network, no exec/import of repo code: every fixture is a throwaway
``tmp_path`` tree written as text and read back via ``ast.parse``/``tomllib``.
"""

from __future__ import annotations

import textwrap

from python_deps.depgraph.os_resolver import ObservedNeed
from python_deps.depgraph.project_native_scan import (
    has_native_build_signal,
    scan_native_build_surface,
)


def _write(tmp_path, rel, src):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(src))
    return p


def test_extension_literal_libraries_extracted(tmp_path):
    _write(tmp_path, "setup.py", """
        from setuptools import setup, Extension
        setup(
            name="pygraphviz",
            ext_modules=[
                Extension(
                    "pygraphviz._graphviz",
                    sources=["pygraphviz/graphviz_wrap.c"],
                    libraries=["cgraph", "cdt", "gvc"],
                ),
            ],
        )
    """)
    needs = scan_native_build_surface(str(tmp_path))
    assert [n.name for n in needs] == ["cgraph", "cdt", "gvc"]
    assert all(isinstance(n, ObservedNeed) for n in needs)
    assert all(n.kind == "linker_lib" for n in needs)
    assert all(n.context == "build" for n in needs)
    assert all(n.strength == "curated" for n in needs)
    assert needs[0].evidence == "setup.py:Extension.libraries"
    assert has_native_build_signal(str(tmp_path)) is True


def test_computed_libraries_is_silent_miss_but_signal_still_true(tmp_path):
    _write(tmp_path, "setup.py", """
        from setuptools import setup, Extension

        def get_libs():
            return ["foo"]

        setup(
            name="pkg",
            ext_modules=[
                Extension("pkg._native", sources=["pkg/native.c"], libraries=get_libs()),
            ],
        )
    """)
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is True


def test_pyx_file_with_no_setup_py_extension(tmp_path):
    _write(tmp_path, "foo.pyx", "cdef int add(int a, int b):\n    return a + b\n")
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is True


def test_pxd_file_also_signals_native(tmp_path):
    _write(tmp_path, "src/foo.pxd", "cdef int add(int a, int b)\n")
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is True


def test_pure_python_repo_is_empty_and_false(tmp_path):
    _write(tmp_path, "setup.py", """
        from setuptools import setup
        setup(name="pkg", packages=["pkg"])
    """)
    _write(tmp_path, "pyproject.toml", """
        [project]
        name = "pkg"
        version = "0.1.0"
    """)
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is False


def test_meson_build_backend_signals_native_no_libraries(tmp_path):
    _write(tmp_path, "pyproject.toml", """
        [build-system]
        requires = ["meson-python"]
        build-backend = "mesonpy"
    """)
    assert has_native_build_signal(str(tmp_path)) is True
    assert scan_native_build_surface(str(tmp_path)) == []


def test_scikit_build_core_backend_signals_native(tmp_path):
    _write(tmp_path, "pyproject.toml", """
        [build-system]
        requires = ["scikit-build-core"]
        build-backend = "scikit_build_core.build"
    """)
    assert has_native_build_signal(str(tmp_path)) is True


def test_absent_setup_py_and_pyproject_no_crash(tmp_path):
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is False


def test_garbage_setup_py_no_crash(tmp_path):
    _write(tmp_path, "setup.py", "def (:\n")  # SyntaxError
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is False


def test_garbage_pyproject_toml_no_crash(tmp_path):
    _write(tmp_path, "pyproject.toml", "this is not [ valid toml\n")
    assert scan_native_build_surface(str(tmp_path)) == []
    assert has_native_build_signal(str(tmp_path)) is False


def test_pyproject_ext_modules_libraries_extracted(tmp_path):
    _write(tmp_path, "pyproject.toml", """
        [build-system]
        requires = ["setuptools"]
        build-backend = "setuptools.build_meta"

        [[tool.setuptools.ext-modules]]
        name = "pkg.speedups"
        sources = ["src/speedups.c"]
        libraries = ["m"]
    """)
    needs = scan_native_build_surface(str(tmp_path))
    assert [n.name for n in needs] == ["m"]
    assert needs[0].evidence.startswith("pyproject.toml")


def test_dedupe_by_name_across_setup_py_and_pyproject(tmp_path):
    _write(tmp_path, "setup.py", """
        from setuptools import setup, Extension
        setup(
            ext_modules=[Extension("m", libraries=["foo", "bar"])],
        )
    """)
    _write(tmp_path, "pyproject.toml", """
        [[tool.setuptools.ext-modules]]
        libraries = ["bar", "baz"]
    """)
    needs = scan_native_build_surface(str(tmp_path))
    assert [n.name for n in needs] == ["foo", "bar", "baz"]
