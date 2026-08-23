from ecosystems.base import CertifyMode, ClosureMode
from ecosystems.python import provider as provmod
from ecosystems.python.provider import PythonProvider


def test_name_and_certify_mode():
    assert PythonProvider().name == "python"
    assert PythonProvider().certify_mode is CertifyMode.INSTALL


def test_detect_manifest_repo_is_1(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\n')
    assert PythonProvider().detect(str(tmp_path)) == 1.0


def test_detect_imports_only_repo_clears_threshold(tmp_path):
    (tmp_path / "app.py").write_text("import os\n")  # no manifest; import evidence
    assert PythonProvider().detect(str(tmp_path)) >= 0.5


def test_detect_requirements_only_repo_clears_threshold(tmp_path):
    # No manifest, no *.py — Python-ness is ONLY in requirements.txt. The dropped
    # rglob heuristic would score this 0.0 (regression); evidence-based detect
    # scores it positive via collect_python_dependency_evidence.
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    assert PythonProvider().detect(str(tmp_path)) >= 0.5


def test_detect_non_python_repo_is_0(tmp_path):
    (tmp_path / "README.md").write_text("hi\n")
    assert PythonProvider().detect(str(tmp_path)) == 0.0


def test_closure_mode_resolve_without_lock(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0"\n')
    assert PythonProvider().closure_mode_for(str(tmp_path)) is ClosureMode.RESOLVE


def test_closure_mode_lock_with_uv_lock(tmp_path):
    (tmp_path / "uv.lock").write_text("")
    assert PythonProvider().closure_mode_for(str(tmp_path)) is ClosureMode.LOCK


def test_package_obligations_delegates_and_threads_record_provider(monkeypatch):
    seen = {}
    sentinel = object()

    def fake_helper(repo, ce, **kw):
        seen["repo"] = repo
        seen["ce"] = ce
        seen["kw"] = kw
        return sentinel

    monkeypatch.setattr(provmod, "_python_package_obligations", fake_helper)
    out = PythonProvider().package_obligations(
        "/r", "CE",
        host_executor="HE", target_python="3.11", target_platform="linux-x86_64",
        exclude_newer="2024-01-01", needed_extras=frozenset({"test"}),
        record_provider="RP",
    )
    assert out is sentinel
    assert seen["repo"] == "/r" and seen["ce"] == "CE"
    assert seen["kw"]["host_executor"] == "HE"
    assert seen["kw"]["needed_extras"] == frozenset({"test"})
    assert seen["kw"]["record_provider"] == "RP"          # threaded (INV signature-stability)


def test_native_obligations_delegates(monkeypatch):
    seen = {}
    sentinel = object()

    def fake_native(graph, ce):
        seen["graph"] = graph
        seen["ce"] = ce
        return sentinel

    monkeypatch.setattr(provmod, "_python_native_obligations", fake_native)
    out = PythonProvider().native_obligations("G", "CE")
    assert out is sentinel and seen["graph"] == "G" and seen["ce"] == "CE"


def test_provider_preserves_hermeticity_symbols():
    """INV-8: importing the provider must NOT fork the record-provider symbols the
    autouse _no_pypi_network stub patches (build.py's module imports are untouched;
    the composite default is still built at the old 569-571 site inside the helper).
    End-to-end hermeticity is re-proven by oracle (a) after Task 7."""
    from python_deps.depgraph import build, coverage, relink

    assert build.pypi_record_provider is coverage.pypi_record_provider
    assert build.composite_record_provider is coverage.composite_record_provider
    assert build.default_record_provider is coverage.default_record_provider
    assert "fetch" in coverage.pypi_record_provider.__kwdefaults__  # the patched lever
    assert relink.PACKAGES_DIST_CMD is not None                     # unchanged object


def test_degenerate_repo_still_dispatches_to_python(tmp_path):
    """No manifest, no *.py, no evidence -> detect()==0.0, but the dispatch
    default (Task 7) must STILL route to PythonProvider, never raise LookupError.
    Reference: test_build_empty_repo_yields_only_test_node (in the frozen 1111)
    must also still pass — its stdlib-import repo has a *.py so it clears the
    threshold directly; this test covers the truly-degenerate case the `default`
    seam guards, which oracle (a) does not otherwise exercise."""
    from ecosystems.registry import PROVIDERS, select_provider

    (tmp_path / "README.md").write_text("no python here\n")
    picked = select_provider(str(tmp_path), PROVIDERS, default=PROVIDERS[0])
    assert picked is PROVIDERS[0] and picked.name == "python"
