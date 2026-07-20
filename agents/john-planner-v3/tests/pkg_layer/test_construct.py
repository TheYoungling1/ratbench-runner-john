# tests/pkg_layer/test_construct.py
"""TDD tests for the pkg_layer construction wiring (Task 7).

``build_package_layer`` wires Contract (T3) + Usage (T2) into one
``PackageLayer`` (T1), then runs Alignment (T5) over it. It is a thin, pure
wiring function: no resolve source is consulted, so ``closure`` is always
``()``; ``provided`` is only ever an injected fixed input (never computed).
Real ``read_contract``/``scan_usage`` do real filesystem I/O on ``tmp_path``
fixtures, but nothing here talks to Docker, PyPI, or an LLM.
"""
from __future__ import annotations

from python_deps.pkg_layer.align import Alignment
from python_deps.pkg_layer.construct import build_package_layer
from python_deps.pkg_layer.planes import ImportContext, PackageLayer, ProvidedEdge


def _write_pyproject(tmp_path, *, deps=(), optional=None):
    lines = ["[project]", 'name = "demo"', 'version = "0.1.0"']
    if deps:
        lines.append("dependencies = [")
        lines.extend(f'  "{dep}",' for dep in deps)
        lines.append("]")
    if optional:
        lines.append("")
        lines.append("[project.optional-dependencies]")
        for group, group_deps in optional.items():
            rendered = ", ".join(f'"{d}"' for d in group_deps)
            lines.append(f"{group} = [{rendered}]")
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n")


def test_build_package_layer_returns_layer_and_alignment(tmp_path):
    _write_pyproject(tmp_path, deps=["requests>=2.0"])
    (tmp_path / "app.py").write_text("import requests\n")

    layer, alignment = build_package_layer(str(tmp_path))

    assert isinstance(layer, PackageLayer)
    assert isinstance(alignment, Alignment)


def test_build_package_layer_contract_is_in_scope_runtime_deps(tmp_path):
    _write_pyproject(tmp_path, deps=["requests>=2.0"])
    (tmp_path / "app.py").write_text("import requests\n")

    layer, _alignment = build_package_layer(str(tmp_path))

    assert {dep.name for dep in layer.contract} == {"requests"}


def test_build_package_layer_closure_is_always_empty(tmp_path):
    _write_pyproject(tmp_path, deps=["requests>=2.0"])
    (tmp_path / "app.py").write_text("import requests\n")

    layer, _alignment = build_package_layer(str(tmp_path))

    assert layer.closure == ()


def test_build_package_layer_includes_scanned_imports(tmp_path):
    _write_pyproject(tmp_path, deps=["requests>=2.0"])
    (tmp_path / "app.py").write_text("import requests\n")

    layer, _alignment = build_package_layer(str(tmp_path))

    assert any(
        node.name == "requests" and node.context is ImportContext.RUNTIME
        for node in layer.imports
    )


def test_build_package_layer_excludes_ungated_optional_group(tmp_path):
    _write_pyproject(
        tmp_path, deps=["requests>=2.0"], optional={"speedups": ["brotli"]}
    )
    (tmp_path / "app.py").write_text("import requests\n")

    layer, _alignment = build_package_layer(str(tmp_path))

    names = {dep.name for dep in layer.contract}
    assert "requests" in names
    assert "brotli" not in names


def test_build_package_layer_excludes_row_declared_in_ungated_optional_group_even_when_also_a_base_dep(
    tmp_path,
):
    # Regression: a dist declared BOTH as a base dependency AND inside an
    # EXCLUDED optional group used to leak the excluded optional-group ROW
    # into layer.contract, because the old implementation filtered the full
    # contract on NAME membership against the selected roots -- and the
    # base-dependency row's presence made the shared name pass that filter.
    # The fix uses row-level scope (`contract.in_scope_deps`) instead.
    _write_pyproject(
        tmp_path,
        deps=["widget>=1.0"],
        optional={"extras": ["widget>=2.0"]},
    )
    (tmp_path / "app.py").write_text("import widget\n")

    layer, _alignment = build_package_layer(str(tmp_path))

    widget_rows = [dep for dep in layer.contract if dep.name == "widget"]
    assert len(widget_rows) == 1
    assert widget_rows[0].kind == "dependency"
    assert not any(
        dep.name == "widget" and dep.kind == "optional_dependency"
        for dep in layer.contract
    )


def test_build_package_layer_includes_gated_optional_group(tmp_path):
    _write_pyproject(
        tmp_path, deps=["requests>=2.0"], optional={"speedups": ["brotli"]}
    )
    (tmp_path / "app.py").write_text("import requests\n")

    layer, _alignment = build_package_layer(
        str(tmp_path), needed_extras=frozenset({"speedups"})
    )

    names = {dep.name for dep in layer.contract}
    assert {"requests", "brotli"} <= names


def test_build_package_layer_injects_provided_edges(tmp_path):
    _write_pyproject(tmp_path)
    (tmp_path / "app.py").write_text("import numpy\n")

    provided = (ProvidedEdge(import_name="numpy", dist="numpy", origin="certified"),)

    layer, alignment = build_package_layer(str(tmp_path), provided=provided)

    assert layer.provided == provided
    assert alignment.under_declared == ()  # the ProvidedEdge certifies the import


def test_build_package_layer_flags_under_declared_runtime_import(tmp_path):
    _write_pyproject(tmp_path)
    (tmp_path / "app.py").write_text("import numpy\n")

    _layer, alignment = build_package_layer(str(tmp_path))

    assert alignment.under_declared == ("numpy",)


def test_build_package_layer_empty_repo_returns_empty_layer_and_alignment(tmp_path):
    layer, alignment = build_package_layer(str(tmp_path))

    assert layer == PackageLayer(contract=(), closure=(), imports=(), provided=())
    assert alignment == Alignment(
        under_declared=(),
        resolution_anomaly=(),
        transitive_only=(),
        installed_unused=(),
    )


def test_build_package_layer_provided_defaults_to_empty_tuple(tmp_path):
    _write_pyproject(tmp_path, deps=["requests>=2.0"])

    layer, _alignment = build_package_layer(str(tmp_path))

    assert layer.provided == ()
