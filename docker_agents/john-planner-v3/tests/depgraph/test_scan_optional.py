"""P0.2 — lexically guarded imports are tagged ``optional``.

The scan tags an Import node ``data["optional"] = True`` when the import is
lexically guarded — either by a ``try`` whose ``except`` catches ImportError-
family (or a bare / ``Exception`` handler), OR by any ``if`` branch
(``body``/``orelse``, so ``elif``/``else`` too).  The ``if`` predicate is not
inspected, so every conditional form — ``sys.version_info`` / ``sys.platform`` /
``os.name`` / ``platform.system()`` forks and ``TYPE_CHECKING`` — is covered by
the one structural rule (no per-predicate whack-a-mole).  This is a pure *tag*:
it changes no flagging behaviour (that is P0.3).  A hard runtime need for the
same name dominates a guarded one (mixed-dominance → NOT optional).
"""

from __future__ import annotations

from pathlib import Path

from python_deps.depgraph.ids import import_id
from python_deps.depgraph.scan import scan_to_nodes


def test_optional_import_tagged(tmp_path: Path) -> None:
    """``try: import ujson / except ImportError`` → Import node is optional."""
    (tmp_path / "opt.py").write_text(
        "try:\n"
        "    import ujson\n"
        "except ImportError:\n"
        "    ujson = None\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("ujson"))
    assert node is not None
    assert node.data.get("optional") is True


def test_optional_import_modulenotfounderror_and_from(tmp_path: Path) -> None:
    """``ModuleNotFoundError`` handler + ``from x import y`` shape also count."""
    (tmp_path / "opt.py").write_text(
        "try:\n"
        "    from ujson import dumps\n"
        "except ModuleNotFoundError:\n"
        "    dumps = None\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("ujson"))
    assert node is not None
    assert node.data.get("optional") is True


def test_hard_import_not_tagged(tmp_path: Path) -> None:
    """A plain module-top ``import requests`` leaves ``data`` falsy."""
    (tmp_path / "app.py").write_text("import requests\n", encoding="utf-8")

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("requests"))
    assert node is not None
    assert not node.data.get("optional")


def test_mixed_dominance_hard_wins(tmp_path: Path) -> None:
    """``requests`` imported hard in a.py and guarded in b.py → NOT optional."""
    (tmp_path / "a.py").write_text("import requests\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "try:\n"
        "    import requests\n"
        "except ImportError:\n"
        "    requests = None\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("requests"))
    assert node is not None
    assert not node.data.get("optional")


def test_version_guarded_import_tagged(tmp_path: Path) -> None:
    """``if sys.version_info < (3, 11): import tomli`` → tagged optional.

    The declared-with-marker provider (``tomli ; python_version < '3.11'``) is
    correctly marker-pruned on a 3.11 target; the guarded import must not read as
    a hard need the Phase-A audit would re-add."""
    (tmp_path / "app.py").write_text(
        "import sys\n"
        "if sys.version_info < (3, 11):\n"
        "    import tomli\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("tomli"))
    assert node is not None
    assert node.data.get("optional") is True


def test_platform_guarded_import_and_else_branch_tagged(tmp_path: Path) -> None:
    """``if sys.platform != 'win32': import uvloop / else: import winloop`` →
    BOTH branches tagged optional (mirrors anyio's asyncio backend)."""
    (tmp_path / "app.py").write_text(
        "import sys\n"
        "if sys.platform != 'win32':\n"
        "    import uvloop\n"
        "else:\n"
        "    import winloop\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    for name in ("uvloop", "winloop"):
        node = graph.get(import_id(name))
        assert node is not None, name
        assert node.data.get("optional") is True, name


def test_type_checking_guarded_import_tagged(tmp_path: Path) -> None:
    """``if TYPE_CHECKING: import numpy`` → optional. This is a guard form a
    naive ``sys.version_info``/``sys.platform``-only patch would MISS; the
    predicate-agnostic rule covers it for free."""
    (tmp_path / "app.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import numpy\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("numpy"))
    assert node is not None
    assert node.data.get("optional") is True


def test_platform_system_guarded_import_tagged(tmp_path: Path) -> None:
    """A ``platform.system()`` fork (another form the two-guard patch misses) →
    optional; confirms the rule never inspects the predicate."""
    (tmp_path / "app.py").write_text(
        "import platform\n"
        "if platform.system() == 'Windows':\n"
        "    import pywinstuff\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("pywinstuff"))
    assert node is not None
    assert node.data.get("optional") is True


def test_if_mixed_dominance_hard_wins(tmp_path: Path) -> None:
    """A name imported unconditionally in one file and under an ``if`` in another
    stays HARD — the hard occurrence dominates (dominance rule preserved)."""
    (tmp_path / "a.py").write_text("import reqx\n", encoding="utf-8")
    (tmp_path / "b.py").write_text(
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    import reqx\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("reqx"))
    assert node is not None
    assert not node.data.get("optional")


def test_function_level_unconditional_import_stays_hard(tmp_path: Path) -> None:
    """A deferred but UNCONDITIONAL import inside a function body is NOT under a
    conditional branch → stays hard (the rule broadens only conditional nesting,
    not every non-module-top import)."""
    (tmp_path / "app.py").write_text(
        "def loader():\n    import lxml\n    return lxml\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("lxml"))
    assert node is not None
    assert not node.data.get("optional")


def test_wrong_handler_not_tagged(tmp_path: Path) -> None:
    """``try/except ValueError`` around an import is NOT a guard → not optional."""
    (tmp_path / "app.py").write_text(
        "try:\n"
        "    import foo\n"
        "except ValueError:\n"
        "    foo = None\n",
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("foo"))
    assert node is not None
    assert not node.data.get("optional")


def test_regex_fallback_defaults_not_optional(tmp_path: Path) -> None:
    """A file with a syntax error falls back to regex import scanning; with no
    AST context the imports default to ``optional=False`` (never a false tag)."""
    (tmp_path / "broken.py").write_text(
        "import regexmod\n"
        "\n"
        "def broken(\n",  # unterminated def -> SyntaxError -> regex fallback
        encoding="utf-8",
    )

    graph = scan_to_nodes(str(tmp_path))

    node = graph.get(import_id("regexmod"))
    assert node is not None
    assert not node.data.get("optional")
