# producers/tests/test_measure_config.py — the `measure=` config contract (design §4).
#
# Every [variety.*] block must carry a measure in {conforming, rehome, none}, and that tag must be
# CONSISTENT with the variety's producer: measure=="none" <=> producer.measurable is False (a
# native-lane method with no rebuildable artifact); measure in {conforming, rehome} <=> measurable
# is True. registry.resolve_variety must also surface the measure field.
import os
import sys

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import producers

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_HERE))          # producers/tests -> producers -> repo root
_HARNESS_DIR = os.path.join(_REPO_ROOT, "harness")
_VARIETIES_TOML = os.path.join(_HARNESS_DIR, "varieties.toml")

# registry lives at <repo>/harness/harness_cli/registry.py — put `harness` on the path.
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)
from harness_cli.registry import load_registry, resolve_variety  # noqa: E402

_VALID_MEASURE = {"conforming", "rehome", "none"}


def _variety_items():
    with open(_VARIETIES_TOML, "rb") as f:
        cfg = tomllib.load(f)
    return sorted(cfg.get("variety", {}).items())


def test_toml_loads_and_has_varieties():
    items = _variety_items()
    assert items, "varieties.toml has no [variety.*] blocks"


@pytest.mark.parametrize("name,block", _variety_items())
def test_every_variety_has_valid_measure(name, block):
    assert "measure" in block, f"variety {name!r} is missing a measure= key"
    assert block["measure"] in _VALID_MEASURE, (
        f"variety {name!r} has measure={block['measure']!r}; must be one of {sorted(_VALID_MEASURE)}")


@pytest.mark.parametrize("name,block", _variety_items())
def test_measure_matches_producer_measurability(name, block):
    model = block["model"]
    # Every current varieties.toml model is a registered producer; assert it rather than skip so a
    # future config/registry omission fails loudly instead of vanishing into a silent skip (FIX 3).
    assert model in producers.PRODUCERS, (
        f"variety {name!r} model {model!r} has no registered producer")
    producer = producers.PRODUCERS[model]
    measure = block["measure"]
    if measure == "none":
        assert producer.measurable is False, (
            f"variety {name!r}: measure='none' but producer {model!r}.measurable is True "
            f"(a measurable producer must be harvested, not gated out)")
    else:  # conforming | rehome
        assert producer.measurable is True, (
            f"variety {name!r}: measure={measure!r} but producer {model!r}.measurable is False "
            f"(a non-producer has no rebuildable artifact to harvest; use measure='none')")


def test_resolve_variety_surfaces_measure():
    registry = load_registry(_VARIETIES_TOML)
    assert resolve_variety(registry, "rat").measure == "none"
    assert resolve_variety(registry, "john-planner-v3").measure == "conforming"
