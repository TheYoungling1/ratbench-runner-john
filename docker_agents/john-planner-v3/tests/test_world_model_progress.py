# tests/test_world_model_progress.py
from src.envstate.world_model import (
    initial_map, merge_map, _derive_progress, _build_required, Fact, OpenProblem,
    _PROGRESS_LAYERS,
)


def _map(**kw):
    base = initial_map(
        base_image="python:3.12", workdir="/app", language="python",
        build_system="pip", repo_layout=(), )
    return merge_map(base, **kw)


def test_base_true_when_base_image_set():
    m = _map()
    p = _derive_progress(m.progress, m)
    assert p["base"] is True


def test_runtime_true_when_python_version_present():
    m = _map(env={"python_version": "Python 3.12.1"})
    assert _derive_progress(m.progress, m)["runtime"] is True


def test_deps_true_when_required_subset_of_installed():
    m = _map(required=(Fact("flask"),), installed=(Fact("flask", "3.0.0"),))
    assert _derive_progress(m.progress, m)["deps"] is True


def test_deps_false_when_required_not_satisfied():
    m = _map(required=(Fact("flask"),), installed=())
    assert _derive_progress(m.progress, m)["deps"] is False


def test_tests_true_when_done_flag():
    m = _map(done_flag=True)
    assert _derive_progress(m.progress, m)["tests"] is True


def test_system_false_when_unresolved_system_problem():
    m = _map(open_problems=(OpenProblem("libpq missing", "x", "system"),))
    assert _derive_progress(m.progress, m)["system"] is False


def test_progress_is_monotonic():
    prev = {layer: False for layer in _PROGRESS_LAYERS}
    prev["deps"] = True  # previously achieved
    m = _map(required=(Fact("flask"),), installed=())  # deps would compute False now
    assert _derive_progress(prev, m)["deps"] is True  # stays True


# ---------------------------------------------------------------------------
# Phase 3: _build_required unit tests
# ---------------------------------------------------------------------------

def test_build_required_makefile():
    assert _build_required(("Makefile", "src/main.c")) is True


def test_build_required_makefile_lowercase():
    assert _build_required(("makefile",)) is True


def test_build_required_gnumakefile():
    assert _build_required(("GNUmakefile",)) is True


def test_build_required_cmake():
    assert _build_required(("CMakeLists.txt",)) is True


def test_build_required_autoconf_configure():
    assert _build_required(("configure",)) is True


def test_build_required_configure_ac():
    assert _build_required(("configure.ac",)) is True


def test_build_required_meson():
    assert _build_required(("meson.build",)) is True


def test_build_required_cargo_toml():
    assert _build_required(("Cargo.toml",)) is True


def test_build_required_go_mod():
    assert _build_required(("go.mod",)) is True


def test_build_required_c_source():
    assert _build_required(("src/dumb_init.c",)) is True


def test_build_required_cc_source():
    assert _build_required(("src/foo.cc",)) is True


def test_build_required_cpp_source():
    assert _build_required(("src/bar.cpp",)) is True


def test_build_required_cxx_source():
    assert _build_required(("src/baz.cxx",)) is True


def test_build_required_go_source():
    assert _build_required(("main.go",)) is True


def test_build_required_rust_source():
    assert _build_required(("src/lib.rs",)) is True


def test_build_required_false_for_setup_py():
    assert _build_required(("setup.py",)) is False


def test_build_required_false_for_readme():
    assert _build_required(("README.md",)) is False


def test_build_required_false_for_python_file():
    assert _build_required(("src/foo.py",)) is False


def test_build_required_false_empty_layout():
    assert _build_required(()) is False


# ---------------------------------------------------------------------------
# Phase 3: _derive_progress build layer — build-required repos
# ---------------------------------------------------------------------------

def _map_with_layout(repo_layout: tuple, **kw):
    """Helper that produces a map with a specific repo_layout."""
    base = initial_map(
        base_image="python:3.12", workdir="/app", language="python",
        build_system="pip", repo_layout=repo_layout,
    )
    return merge_map(base, **kw)


def test_build_false_for_build_required_repo_with_no_open_problem():
    """A repo with Makefile + .c has no build open_problem → build must stay False.

    Without the fix, 'build' would be True (signal-less: no open_problem targets it).
    This is the dumb-init case: C binary never built, yet old logic said build=True.
    """
    layout = ("Makefile", "src/dumb_init.c", "tests/")
    m = _map_with_layout(layout)
    prev = {layer: False for layer in _PROGRESS_LAYERS}
    p = _derive_progress(prev, m)
    assert p["build"] is False


def test_build_true_for_pure_python_repo_with_no_open_problem():
    """Pure-Python repo with no open_problem → build stays True (signal-less, unchanged behavior)."""
    layout = ("setup.py", "tests/", "src/foo.py")
    m = _map_with_layout(layout)
    prev = {layer: False for layer in _PROGRESS_LAYERS}
    p = _derive_progress(prev, m)
    assert p["build"] is True


def test_build_monotonic_stays_true_for_build_required_when_prev_true():
    """Even for a build-required repo, if prev['build'] was True, it must stay True (monotonicity)."""
    layout = ("Makefile", "src/dumb_init.c")
    m = _map_with_layout(layout)
    prev = {layer: False for layer in _PROGRESS_LAYERS}
    prev["build"] = True  # previously established (e.g. make succeeded)
    p = _derive_progress(prev, m)
    assert p["build"] is True


def test_build_false_stays_false_for_build_required_after_new_build_open_problem():
    """build-required repo with an open build problem → still False."""
    layout = ("Makefile", "src/main.c")
    m = _map_with_layout(
        layout,
        open_problems=(OpenProblem("make: command not found", "no make", "build"),),
    )
    prev = {layer: False for layer in _PROGRESS_LAYERS}
    p = _derive_progress(prev, m)
    assert p["build"] is False
