"""TDD tests for the pkg_layer Usage plane: context-tagged import scan (Task 2).

``scan_usage`` reuses ``python_deps.import_graph.scan_imports`` for the raw
external import set + provenance, then runs its own focused AST pass to
assign a runtime-relevance CONTEXT per import site. Only
``classification == "external"`` imports become nodes.
"""
from __future__ import annotations

from python_deps.pkg_layer.planes import ImportContext
from python_deps.pkg_layer.usage import scan_usage


def _nodes_by_name(repo_path):
    return {node.name: node for node in scan_usage(str(repo_path))}


# --------------------------------------------------------------------------- #
# Per-context fixtures (one real assertion per context)
# --------------------------------------------------------------------------- #

def test_plain_runtime_import_is_runtime(tmp_path):
    (tmp_path / "app.py").write_text("import requests\n")

    by_name = _nodes_by_name(tmp_path)

    assert by_name["requests"].context is ImportContext.RUNTIME
    assert by_name["requests"].provenance == ("app.py",)


def test_guarded_import_error_is_optional(tmp_path):
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import simplejson\n"
        "except ImportError:\n"
        "    simplejson = None\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["simplejson"].context is ImportContext.OPTIONAL
    assert by_name["simplejson"].provenance == ("app.py",)


def test_guarded_module_not_found_error_is_optional(tmp_path):
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import cchardet\n"
        "except ModuleNotFoundError:\n"
        "    cchardet = None\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["cchardet"].context is ImportContext.OPTIONAL


def test_generic_except_does_not_count_as_optional(tmp_path):
    # The rule is specific to ImportError/ModuleNotFoundError -- a broad
    # `except Exception` guard is not the "optional dependency" pattern.
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import speedups_pkg\n"
        "except Exception:\n"
        "    speedups_pkg = None\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["speedups_pkg"].context is ImportContext.RUNTIME


def test_type_checking_guarded_import_is_typing(tmp_path):
    (tmp_path / "app.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import numpy\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["numpy"].context is ImportContext.TYPING
    assert by_name["numpy"].provenance == ("app.py",)


def test_test_dir_import_is_test(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("import hypothesis\n")

    by_name = _nodes_by_name(tmp_path)

    assert by_name["hypothesis"].context is ImportContext.TEST


def test_conftest_import_is_test(tmp_path):
    (tmp_path / "conftest.py").write_text("import faker\n")

    by_name = _nodes_by_name(tmp_path)

    assert by_name["faker"].context is ImportContext.TEST


def test_dynamic_import_module_literal_is_dynamic(tmp_path):
    (tmp_path / "app.py").write_text(
        "import importlib\n"
        "mod = importlib.import_module('totally_dynamic_pkg')\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["totally_dynamic_pkg"].context is ImportContext.DYNAMIC
    assert by_name["totally_dynamic_pkg"].provenance == ("app.py",)


def test_dunder_import_literal_is_dynamic(tmp_path):
    (tmp_path / "app.py").write_text("mod = __import__('another_dynamic_pkg')\n")

    by_name = _nodes_by_name(tmp_path)

    assert by_name["another_dynamic_pkg"].context is ImportContext.DYNAMIC


def test_dynamic_import_non_literal_argument_is_ignored(tmp_path):
    # Only a STRING LITERAL argument counts -- a variable can't be resolved
    # statically and must not be guessed into the graph.
    (tmp_path / "app.py").write_text(
        "import importlib\n"
        "name = 'whatever_pkg'\n"
        "mod = importlib.import_module(name)\n"
    )

    names = {node.name for node in scan_usage(str(tmp_path))}

    assert "whatever_pkg" not in names
    assert not any(
        node.context is ImportContext.DYNAMIC for node in scan_usage(str(tmp_path))
    )


def test_from_importlib_import_module_literal_is_dynamic(tmp_path):
    # A bare import_module("foo") IS dynamic when the file binds it from
    # importlib.
    (tmp_path / "app.py").write_text(
        "from importlib import import_module\n"
        "mod = import_module('foo')\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["foo"].context is ImportContext.DYNAMIC


def test_from_importlib_import_module_alias_literal_is_dynamic(tmp_path):
    # Honor an alias: `from importlib import import_module as im`.
    (tmp_path / "app.py").write_text(
        "from importlib import import_module as im\n"
        "mod = im('aliased_dynamic_pkg')\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["aliased_dynamic_pkg"].context is ImportContext.DYNAMIC


def test_importlib_alias_receiver_import_module_is_dynamic(tmp_path):
    # Honor `import importlib as il` on the receiver side.
    (tmp_path / "app.py").write_text(
        "import importlib as il\n"
        "mod = il.import_module('receiver_aliased_pkg')\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["receiver_aliased_pkg"].context is ImportContext.DYNAMIC


def test_user_defined_import_module_is_not_dynamic(tmp_path):
    # A local function named import_module is NOT importlib's -- a bare
    # import_module("requests") call must NOT fabricate a dynamic node for
    # requests (no `from importlib import import_module` binding present).
    (tmp_path / "app.py").write_text(
        "def import_module(x):\n"
        "    return x\n"
        "\n"
        "value = import_module('requests')\n"
    )

    names = {node.name for node in scan_usage(str(tmp_path))}

    assert "requests" not in names


# --------------------------------------------------------------------------- #
# Guard-clause scoping: OPTIONAL is the try BODY / ImportError handler only
# --------------------------------------------------------------------------- #

def test_else_clause_of_import_error_try_is_runtime(tmp_path):
    # An import in the `else:` clause only runs when the guarded import in the
    # `try:` body SUCCEEDED, so it is a hard runtime dependency, not optional.
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import guarded_pkg\n"
        "except ImportError:\n"
        "    guarded_pkg = None\n"
        "else:\n"
        "    import hard_runtime_pkg\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["guarded_pkg"].context is ImportContext.OPTIONAL
    assert by_name["hard_runtime_pkg"].context is ImportContext.RUNTIME


def test_finally_clause_of_import_error_try_is_runtime(tmp_path):
    # `finally:` always runs regardless of the guarded import — not optional.
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import guarded_pkg\n"
        "except ImportError:\n"
        "    guarded_pkg = None\n"
        "finally:\n"
        "    import always_pkg\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["always_pkg"].context is ImportContext.RUNTIME


def test_import_error_handler_body_is_optional(tmp_path):
    # A fallback import inside the ImportError handler body is itself optional.
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import fast_pkg\n"
        "except ImportError:\n"
        "    import slow_fallback_pkg\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["fast_pkg"].context is ImportContext.OPTIONAL
    assert by_name["slow_fallback_pkg"].context is ImportContext.OPTIONAL


# --------------------------------------------------------------------------- #
# Precedence: RUNTIME > OPTIONAL > TYPING > TEST > DYNAMIC
# --------------------------------------------------------------------------- #

def test_dynamic_literal_in_test_file_is_test(tmp_path):
    # A dynamic import literal inside a test file: labels {TEST, DYNAMIC};
    # TEST > DYNAMIC so the node is TEST, not DYNAMIC.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_dyn.py").write_text(
        "import importlib\n"
        "mod = importlib.import_module('dynamic_in_test_pkg')\n"
    )

    by_name = _nodes_by_name(tmp_path)

    assert by_name["dynamic_in_test_pkg"].context is ImportContext.TEST
    assert by_name["dynamic_in_test_pkg"].provenance == ("tests/test_dyn.py",)


def test_runtime_use_anywhere_wins_over_guarded_use(tmp_path):
    (tmp_path / "a.py").write_text(
        "try:\n"
        "    import ujson\n"
        "except ImportError:\n"
        "    ujson = None\n"
    )
    (tmp_path / "b.py").write_text("import ujson\n")

    by_name = _nodes_by_name(tmp_path)

    assert by_name["ujson"].context is ImportContext.RUNTIME
    assert set(by_name["ujson"].provenance) == {"a.py", "b.py"}


# --------------------------------------------------------------------------- #
# Classification gate + name-truncation agreement with scan_imports
# --------------------------------------------------------------------------- #

def test_stdlib_and_local_imports_are_excluded(tmp_path):
    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    (tmp_path / "app.py").write_text(
        "import os\n"
        "import helper\n"
        "import requests\n"
    )

    names = {node.name for node in scan_usage(str(tmp_path))}

    assert "os" not in names
    assert "helper" not in names
    assert "requests" in names


def test_dotted_import_truncated_to_top_level(tmp_path):
    (tmp_path / "app.py").write_text("import requests.adapters\n")

    names = {node.name for node in scan_usage(str(tmp_path))}

    assert names == {"requests"}


def test_from_import_form_is_scanned(tmp_path):
    (tmp_path / "app.py").write_text("from yaml import safe_load\n")

    names = {node.name for node in scan_usage(str(tmp_path))}

    assert "yaml" in names


def test_relative_imports_do_not_crash_and_produce_no_nodes(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text("from . import sibling\n")
    (pkg / "sibling.py").write_text("")

    assert scan_usage(str(tmp_path)) == ()


def test_empty_repo_returns_empty_tuple(tmp_path):
    assert scan_usage(str(tmp_path)) == ()
