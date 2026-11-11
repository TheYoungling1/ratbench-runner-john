"""tools/ebsr_variants.py: the variant matrix, the gate command each variant runs, and the summary."""
from tools.ebsr_variants import (FACTORS, NOW, OLD, gate_script, parse_collected, summarize,
                                 variant_matrix)


def test_ofat_matrix_has_both_baselines_and_every_single_flip():
    vs = variant_matrix("ofat")
    assert vs[0] == NOW and vs[1] == OLD
    assert len(vs) == 2 + 2 * len(FACTORS) and len({tuple(sorted(v.items())) for v in vs}) == len(vs)
    for v in vs[2:]:
        assert sum(v[f] != NOW[f] for f in FACTORS) == 1 or sum(v[f] != OLD[f] for f in FACTORS) == 1


def test_full_matrix_is_the_factorial():
    assert len(variant_matrix("full")) == 2 ** len(FACTORS)


def test_gate_script_reflects_each_factor():
    cmds = {"ensure_new": "ENSURE_NEW", "restore": "RESTORE"}
    old = gate_script(OLD, cmds)
    assert "pip install -q --break-system-packages pytest pytest-timeout" in old
    assert "--disable-warnings /testbed" in old and "RESTORE" not in old and "unset PYTEST_ADDOPTS" not in old
    new = gate_script(NOW, cmds)
    assert "ENSURE_NEW" in new and "RESTORE" in new and "unset PYTEST_ADDOPTS PYTEST_PLUGINS" in new
    assert "--disable-warnings; exit" in new          # no explicit path: pytest honours testpaths


def test_parse_collected_reads_pytest_summary():
    assert parse_collected("...\n1234 tests collected in 3.2s\n") == 1234
    assert parse_collected("1 test collected, 2 errors in 1s") == 1
    assert parse_collected("no tests ran in 0.1s") == 0
    # pytest -q --collect-only prints per-file counts and no summary line
    assert parse_collected("REF=abc (pinned)\ntests/test_a.py: 25\ntests/sub/test_b.py: 9\n") == 34


def test_summary_counts_passes_and_flips_against_current_rows():
    results = [{"repo": "a/x", "variant": "now", "rc": 0}, {"repo": "a/x", "variant": "old", "rc": 2},
               {"repo": "b/y", "variant": "now", "rc": 2}, {"repo": "b/y", "variant": "old", "rc": 5}]
    s = summarize(results, current_clean={"a/x": True, "b/y": False})
    assert s["now"]["pass"] == 1 and s["old"]["pass"] == 1
    assert s["old"]["fail_to_pass"] == ["b/y"] and s["old"]["pass_to_fail"] == ["a/x"]
