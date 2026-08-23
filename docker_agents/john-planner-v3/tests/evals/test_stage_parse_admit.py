"""Tests for eval stage 2 (parse-exactness) + stage 4 (probe-admissibility firewall).

Pure/CI: no Docker, no model. These assert that the eval — bound to the SHIPPED
clean-core (`parse_provisioning_spec`, `normalize_probe`, `render_probe_poll`,
`is_read_only`) and the E2 corpus — reports the honest parser contract and a 1.0
probe-admissibility rate (every probe becomes read-only, including the 9 exotic curls).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for _p in (str(_ROOT), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from python_deps.depgraph.patch_gate import is_read_only  # noqa: E402
from python_deps.depgraph.service_recipes import (  # noqa: E402
    normalize_probe,
    render_probe_poll,
)
from evals.service_config_detection.provision_corpus import PROVISION_CASES  # noqa: E402
from evals.service_config_detection.stage_parse_admit import (  # noqa: E402
    NINE_CURL_PROBES,
    measure_parse,
    measure_probe_admissibility,
)

_HEALTHCHECK_CASES = {"cockroachdb", "cassandra", "mariadb"}


# ---------------------------------------------------------------------------
# Stage 2 — parse exactness
# ---------------------------------------------------------------------------
def test_all_cases_parse():
    """Every corpus case parses to a ProvisioningSpec without raising (18/18)."""
    result = measure_parse()
    assert result["parsed_ok"] == len(PROVISION_CASES)


def test_ports_extracted():
    """Every case declares `ports`, so every parsed spec must carry a port."""
    result = measure_parse()
    assert all(c["port"] is not None for c in result["per_case"])


def test_probe_extracted_only_with_healthcheck():
    """`spec.probe` is present exactly for the cases whose compose has a healthcheck."""
    per_case = {c["name"]: c for c in measure_parse()["per_case"]}
    for name in _HEALTHCHECK_CASES:
        assert per_case[name]["probe"] is not None, name
    # a no-healthcheck case must have no probe (honest: not invented)
    assert per_case["redis"]["probe"] is None


def test_redis_image_kinded():
    """A standard image the parser recognizes gets its kind: `redis:7` -> "redis"."""
    per_case = {c["name"]: c for c in measure_parse()["per_case"]}
    assert per_case["redis"]["parsed_kind"] == "redis"


# ---------------------------------------------------------------------------
# Stage 4 — probe-admissibility firewall (target 1.0)
# ---------------------------------------------------------------------------
def test_probe_admissibility_100():
    """The firewall guarantees EVERY probe normalizes to a read-only command."""
    result = measure_probe_admissibility()
    assert result["admissibility_rate"] == 1.0
    assert result["non_admissible"] == []


def test_nine_curl_probes_become_nc():
    """Each exotic curl probe rewrites to `nc -z 127.0.0.1 <port>` and is read-only."""
    for probe, port in NINE_CURL_PROBES:
        out = normalize_probe(probe, port)
        assert out == f"nc -z 127.0.0.1 {port}", (probe, out)
        assert is_read_only(out) is True


def test_render_probe_poll_wraps_normalized():
    """`render_probe_poll` wraps a normalized probe in a bounded, exit-0 poll loop."""
    poll = render_probe_poll(normalize_probe("curl -f http://x:8080/h", 8080))
    assert "nc -z 127.0.0.1 8080" in poll
    assert "exit 0" in poll
