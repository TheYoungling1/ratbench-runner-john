# tests/test_runtime_base.py
"""Runtime-tier base selection: derive a concrete python minor from the
project's declared constraint, and pin a python base-image tag to it.

Policy (v2): defer to the ImageSelector's chosen python (``prefer``); only
clamp to satisfy ``requires-python``. Without a ``prefer``, use ``default``
if it satisfies the constraint, else the nearest supported minor to ``default``
within the constraint. Unparseable or undeclared -> keep ``prefer`` if given,
else ``default`` (byte-identical to today's behavior).
"""
import pytest

from src.envstate.runtime_base import (
    RuntimeBaseDecision,
    _base_image_minor,
    choose_python_minor,
    pin_base_python,
    resolve_runtime_base,
)


# ---- choose_python_minor -------------------------------------------------

def test_pep621_range_default_in_range():
    # Policy v2: no prefer -> keep default (3.11) when it satisfies, not the floor
    minor, _ = choose_python_minor(">=3.10,<3.13")
    assert minor == "3.11"  # was "3.10" (old floor policy)


def test_floor_only_picks_floor():
    minor, _ = choose_python_minor(">=3.12")
    assert minor == "3.12"


def test_poetry_caret_normalized():
    # Policy v2: no prefer -> default (3.11) satisfies ^3.10 (>=3.10,<4.0)
    minor, _ = choose_python_minor("^3.10")
    assert minor == "3.11"  # was "3.10" (old floor policy)


def test_poetry_tilde_normalized():
    minor, _ = choose_python_minor("~3.10")
    assert minor == "3.10"


def test_below_supported_floor_default_in_range():
    # Policy v2: no prefer, constraint >=3.8 -> default (3.11) satisfies -> keep default
    # (old policy: clamped up to lowest supported minor = 3.9; now we don't go below default)
    minor, _ = choose_python_minor(">=3.8")
    assert minor == "3.11"  # was "3.9" (old floor policy)


def test_star_pin():
    minor, _ = choose_python_minor("==3.11.*")
    assert minor == "3.11"


def test_none_returns_default_with_reason():
    minor, reason = choose_python_minor(None)
    assert minor == "3.11"
    assert "declared" in reason.lower()


def test_unparseable_returns_default():
    minor, reason = choose_python_minor("not a spec !!")
    assert minor == "3.11"
    assert "unparse" in reason.lower() or "default" in reason.lower()


def test_no_supported_minor_satisfies_returns_default():
    minor, reason = choose_python_minor(">=3.99")
    assert minor == "3.11"
    assert "satisf" in reason.lower() or "default" in reason.lower()


def test_custom_default_honored():
    minor, _ = choose_python_minor(None, default="3.12")
    assert minor == "3.12"


# ---- pin_base_python -----------------------------------------------------

def test_pin_plain_python_tag():
    assert pin_base_python("python:3.11", "3.10") == "python:3.10"


def test_pin_slim_variant_preserved():
    assert pin_base_python("python:3.11-slim", "3.10") == "python:3.10-slim"


def test_pin_multi_suffix_variant_preserved():
    assert pin_base_python("python:3.11-slim-bookworm", "3.10") == "python:3.10-slim-bookworm"


def test_pin_drops_patch_to_minor():
    assert pin_base_python("python:3.11.4-slim", "3.10") == "python:3.10-slim"


def test_pin_namespaced_python_image():
    assert pin_base_python("docker.io/library/python:3.11-slim", "3.10") == \
        "docker.io/library/python:3.10-slim"


def test_non_python_base_unchanged():
    assert pin_base_python("ubuntu:22.04", "3.10") == "ubuntu:22.04"


def test_python_tag_without_version_unchanged():
    # conservative: no recognizable version component -> leave it alone
    assert pin_base_python("python:latest", "3.10") == "python:latest"


# ---- resolve_runtime_base (seam entry point) -----------------------------

def test_resolve_pins_base_and_reports_minor(tmp_path):
    # Policy v2: selector's base is python:3.11-slim -> prefer=3.11 -> 3.11 satisfies
    # >=3.10,<3.13 -> keep 3.11 (base_image unchanged); was: stomped to 3.10 (floor)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.10,<3.13"\n'
    )
    d = resolve_runtime_base(str(tmp_path), "python:3.11-slim")
    assert isinstance(d, RuntimeBaseDecision)
    assert d.minor == "3.11"  # was "3.10" (old floor policy)
    assert d.base_image == "python:3.11-slim"  # was "python:3.10-slim" (old floor policy)
    assert d.requires_python == ">=3.10,<3.13"


def test_resolve_undeclared_leaves_base_at_default(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    d = resolve_runtime_base(str(tmp_path), "python:3.11-slim")
    assert d.minor == "3.11"
    assert d.base_image == "python:3.11-slim"  # unchanged
    assert d.requires_python is None


def test_resolve_computes_minor_even_for_nonpython_base(tmp_path):
    # base can't be pinned, but the chosen minor still flows to the resolve target
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.12"\n'
    )
    d = resolve_runtime_base(str(tmp_path), "ubuntu:22.04")
    assert d.minor == "3.12"
    assert d.base_image == "ubuntu:22.04"  # unchanged


# ---- _base_image_minor -------------------------------------------------------

def test_base_image_minor_slim():
    assert _base_image_minor("python:3.12-slim") == "3.12"


def test_base_image_minor_full_patch():
    assert _base_image_minor("python:3.11.4") == "3.11"


def test_base_image_minor_non_python():
    assert _base_image_minor("golang:1.21") is None


def test_base_image_minor_latest_tag():
    assert _base_image_minor("python:latest") is None


def test_base_image_minor_unsupported_version():
    # 3.8 is below SUPPORTED_MINORS floor
    assert _base_image_minor("python:3.8-slim") is None


# ---- choose_python_minor with prefer (new keep/clamp policy) -----------------

def test_keep_selector_crawl4ai_red():
    # crawl4ai: selector chose 3.12, old code stomped to 3.9 -> fix keeps 3.12
    minor, _ = choose_python_minor(">=3.9", prefer="3.12")
    assert minor == "3.12"


def test_keep_selector_ingestr_red():
    # ingestr: selector chose 3.11, old code stomped to 3.9 -> fix keeps 3.11
    minor, _ = choose_python_minor(">=3.7", prefer="3.11")
    assert minor == "3.11"


def test_clamp_selector_above_ceiling():
    # prefer=3.12 but constraint says <3.11 -> clamp down to 3.10
    minor, reason = choose_python_minor(">=3.9,<3.11", prefer="3.12")
    assert minor == "3.10"
    assert "clamp" in reason.lower()


def test_clamp_selector_below_floor():
    # prefer=3.9 but constraint says >=3.10 -> clamp up to 3.10
    minor, reason = choose_python_minor(">=3.10", prefer="3.9")
    assert minor == "3.10"
    assert "clamp" in reason.lower()


def test_no_prefer_default_in_range():
    # no prefer, constraint >=3.9: default (3.11) satisfies -> keep default, not floor
    minor, _ = choose_python_minor(">=3.9", prefer=None)
    assert minor == "3.11"


def test_no_prefer_no_constraint_returns_default():
    minor, _ = choose_python_minor(None, prefer=None)
    assert minor == "3.11"


def test_prefer_kept_no_constraint():
    # no requires-python: keep the selector's choice
    minor, _ = choose_python_minor(None, prefer="3.12")
    assert minor == "3.12"


def test_no_prefer_high_floor():
    # no prefer, floor 3.13: default (3.11) does NOT satisfy -> nearest to default
    minor, _ = choose_python_minor(">=3.13", prefer=None)
    assert minor == "3.13"


# ---- resolve_runtime_base defers to selector's base python -------------------

def test_resolve_keeps_selector_python_when_satisfies(tmp_path):
    # selector chose python:3.12-slim, requires-python >=3.9 -> keep 3.12, image unchanged
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.9"\n'
    )
    d = resolve_runtime_base(str(tmp_path), "python:3.12-slim")
    assert d.minor == "3.12"
    assert d.base_image == "python:3.12-slim"  # pin kept selector's choice
