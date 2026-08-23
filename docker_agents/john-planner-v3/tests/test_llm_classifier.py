import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.depgraph.schema import NodeType, Layer  # noqa: E402
from src.envstate.llm_classifier import make_llm_classifier  # noqa: E402


def _fixed(json_text):
    return lambda messages: json_text


def test_package_kind_with_check_becomes_discovery():
    j = '{"kind":"PACKAGE","name":"pytest-asyncio","check_command":"python3 -c \'import pytest_asyncio\'","requires_of":"","confidence":0.9,"rationale":"missing test dep"}'
    clf = make_llm_classifier(_fixed(j))
    d = clf("pytest -q", "ModuleNotFoundError: No module named 'pytest_asyncio'")
    assert d is not None
    assert d.node_type is NodeType.PACKAGE and d.layer is Layer.PIP
    assert d.name == "pytest-asyncio"
    assert d.check_command == "python3 -c 'import pytest_asyncio'"
    assert d.confidence == "runtime-llm"


def test_system_lib_carries_owner_via_requires_of():
    j = '{"kind":"SYSTEM_LIB","name":"libpq-dev","check_command":"dpkg -s libpq-dev","requires_of":"pkg:psycopg2","confidence":0.8,"rationale":"pg_config"}'
    d = make_llm_classifier(_fixed(j))("pip install psycopg2", "pg_config not found")
    assert d is not None and d.node_type is NodeType.SYSTEM_LIB
    assert d.requires_of == "pkg:psycopg2"


def test_repo_bug_returns_none_and_notes_out_of_scope():
    notes = []
    j = '{"kind":"REPO_BUG","name":"","check_command":"","confidence":0.95,"rationale":"assertion failure in app logic"}'
    d = make_llm_classifier(_fixed(j), note_out_of_scope=lambda c, r: notes.append((c, r)))("pytest", "AssertionError: 1 != 2")
    assert d is None
    assert notes and "assertion" in notes[0][1].lower()


def test_env_kind_without_check_returns_none():
    j = '{"kind":"PACKAGE","name":"mystery","check_command":"","confidence":0.5,"rationale":"unsure"}'
    assert make_llm_classifier(_fixed(j))("x", "weird error") is None


def test_malformed_output_returns_none_no_raise():
    assert make_llm_classifier(_fixed("not json at all"))("x", "y") is None
    def _boom(messages):
        raise RuntimeError("llm down")
    assert make_llm_classifier(_boom)("x", "y") is None
