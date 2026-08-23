"""Tests for the two new runtime sub-parsers in failure_classifier.py."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from python_deps.failure_classifier import classify_config_error, classify_tool_error


# ── classify_config_error ────────────────────────────────────────────────────

def test_config_keyerror_single_quotes():
    assert classify_config_error("python app.py", "KeyError: 'DATABASE_URL'") == "DATABASE_URL"

def test_config_keyerror_double_quotes():
    assert classify_config_error("python app.py", 'KeyError: "SECRET_KEY"') == "SECRET_KEY"

def test_config_pydantic_field_required():
    output = (
        "pydantic.error_wrappers.ValidationError: 1 validation error for Settings\n"
        "REDIS_URL\n"
        "  field required (type=value_error.missing)"
    )
    assert classify_config_error("python -c 'from app.config import settings'", output) == "REDIS_URL"

def test_config_pydantic_v2_field_required():
    output = (
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for Config\n"
        "API_KEY\n"
        "  Field required [type=missing, input_url=https://errors.pydantic.dev/2.0/v/missing]"
    )
    assert classify_config_error("python app.py", output) == "API_KEY"

def test_config_no_match_returns_none():
    assert classify_config_error("pip install flask", "No module named 'flask'") is None

def test_config_empty_output_returns_none():
    assert classify_config_error("python app.py", "") is None

def test_config_non_env_keyerror_returns_none():
    # lowercase key — not an env-var pattern (all-caps or mixed with underscores)
    assert classify_config_error("python app.py", "KeyError: 'some_dict_key_lowercase'") is None


# ── classify_tool_error ──────────────────────────────────────────────────────

def test_tool_command_not_found():
    assert classify_tool_error("ffmpeg -i input.mp4 out.webm", "ffmpeg: command not found") == "ffmpeg"

def test_tool_command_not_found_sh_prefix():
    assert classify_tool_error("make all", "/bin/sh: 1: make: not found") == "make"

def test_tool_filenotfounderror():
    output = "FileNotFoundError: [Errno 2] No such file or directory: 'pandoc'"
    assert classify_tool_error("subprocess.run(['pandoc', '--version'])", output) == "pandoc"

def test_tool_no_match_returns_none():
    assert classify_tool_error("python app.py", "KeyError: 'DATABASE_URL'") is None

def test_tool_empty_output_returns_none():
    assert classify_tool_error("ls /tmp", "") is None


# ── classify_observation dispatch ────────────────────────────────────────────

from python_deps.depgraph.runtime_classify import classify_observation, Discovery
from python_deps.depgraph.schema import NodeType, Layer


def test_dispatch_module_not_found_returns_package_discovery():
    d = classify_observation("python app.py", "ModuleNotFoundError: No module named 'cv2'")
    assert d is not None
    assert d.node_type is NodeType.PACKAGE
    assert d.name == "opencv-python"          # curated mapping from import_mapping.py
    assert d.layer is Layer.PIP
    assert d.check_command == "python3 -c \"import cv2\""
    assert d.confidence == "runtime-deterministic"


def test_dispatch_module_not_found_unknown_import():
    # "mylib" has no curated-table entry and no declared match at this bare
    # dispatch layer -> unresolved (name=None), never guessed as itself.
    d = classify_observation("python app.py", "ModuleNotFoundError: No module named 'mylib'")
    assert d is not None
    assert d.node_type is NodeType.PACKAGE
    assert d.name is None
    assert d.layer is Layer.PIP


def test_dispatch_native_library_returns_syslib():
    d = classify_observation(
        "python app.py",
        "ImportError: libGL.so.1: cannot open shared object file: No such file or directory",
    )
    assert d is not None
    assert d.node_type is NodeType.SYSTEM_LIB
    assert d.name == "libGL.so.1"
    assert d.layer is Layer.SYSTEM
    assert d.check_command == "ldconfig -p | grep -q libGL.so.1"


def test_dispatch_service_error_returns_service_discovery():
    d = classify_observation(
        "python manage.py migrate",
        "psycopg2.OperationalError: could not connect to server: Connection refused",
    )
    assert d is not None
    assert d.node_type is NodeType.SERVICE
    assert d.name == "postgres"
    assert d.layer is Layer.SERVICES
    assert d.check_command is None          # services are advisory


def test_dispatch_config_error_returns_config_discovery():
    d = classify_observation("python app.py", "KeyError: 'DATABASE_URL'")
    assert d is not None
    assert d.node_type is NodeType.CONFIG
    assert d.name == "DATABASE_URL"
    assert d.layer is Layer.CONFIG
    assert d.check_command == "printenv DATABASE_URL"


def test_dispatch_tool_error_returns_tool_discovery():
    d = classify_observation("make all", "make: command not found")
    assert d is not None
    assert d.node_type is NodeType.TOOL
    assert d.name == "make"
    assert d.layer is Layer.TOOLCHAIN
    assert d.check_command == "command -v make"


def test_dispatch_ignored_build_time_failure_returns_none():
    # no_matching_distribution is a build-time install failure — not a runtime requirement
    d = classify_observation(
        "pip install flask",
        "No matching distribution found for flask==99.0",
    )
    assert d is None


def test_dispatch_not_dependency_related_returns_none():
    d = classify_observation("python app.py", "AssertionError: expected True to be False")
    assert d is None


def test_dispatch_priority_module_before_service():
    # Output has both a ModuleNotFoundError AND a service-style error:
    # module classification wins (priority 1 before priority 2).
    output = (
        "ModuleNotFoundError: No module named 'psycopg2'\n"
        "psycopg2.OperationalError: could not connect to server"
    )
    d = classify_observation("python app.py", output)
    assert d is not None
    assert d.node_type is NodeType.PACKAGE


def test_dispatch_import_name_error_returns_package():
    # 'yaml' is curated (-> PyYAML) so this exercises the import_name_error
    # dispatch path's positive (resolved) case, not the unresolved case
    # (covered separately by test_dispatch_unresolved_import_yields_none_package).
    d = classify_observation(
        "python app.py",
        "ImportError: cannot import name 'safe_load' from 'yaml'",
    )
    assert d is not None
    assert d.node_type is NodeType.PACKAGE
    assert d.name == "PyYAML"


def test_dispatch_unresolved_import_yields_none_package(monkeypatch):
    import python_deps.depgraph.runtime_classify as rc
    from python_deps.import_mapping import unresolved_result

    monkeypatch.setattr(
        rc, "map_import_to_package",
        lambda name, *a, **k: unresolved_result(name),
    )
    d = rc.classify_observation(
        "python app.py", "ModuleNotFoundError: No module named 'mystery'"
    )
    assert d is not None
    assert d.node_type is NodeType.PACKAGE
    assert d.name is None


# ── Discovery.requires_of field ───────────────────────────────────────────────

def test_discovery_requires_of_defaults_none_and_accepts_owner():
    d = Discovery(
        node_type=NodeType.SYSTEM_LIB, name="libpq.so.5", layer=Layer.SYSTEM,
        evidence="x", check_command="ldconfig -p | grep -q libpq.so.5",
    )
    assert d.requires_of is None                      # default
    d2 = Discovery(
        node_type=NodeType.SYSTEM_LIB, name="libpq.so.5", layer=Layer.SYSTEM,
        evidence="x", check_command="c", requires_of="pkg:psycopg2",
    )
    assert d2.requires_of == "pkg:psycopg2"           # carries the owner
