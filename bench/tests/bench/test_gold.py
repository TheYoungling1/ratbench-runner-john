# tests/bench/test_gold.py
from bench.gold import (
    norm,
    path_to_junit_key,
    junit_ids_to_paths,
    load_gold,
    gold_coverage,
    GoldRepo,
)
from bench.schema import MeasureRow


# ---- norm() : matches tier1b.norm exactly -------------------------------------------------

def test_norm_strips_known_prefixes():
    assert norm("src/pkg/test_x.py::t") == "pkg/test_x.py::t"
    assert norm("./tests/test_x.py::t") == "tests/test_x.py::t"
    assert norm("/src/pkg/test_x.py::t") == "pkg/test_x.py::t"
    assert norm("tests/test_x.py::t") == "tests/test_x.py::t"


# ---- path <-> junit key translation (the node-id-form landmine fix) -----------------------

def test_path_to_junit_key_class_toplevel_param():
    assert path_to_junit_key("tests/api/test_p.py::TestAll::test_empty") == \
        "tests.api.test_p.TestAll::test_empty"
    assert path_to_junit_key("tests/t_a.py::t1") == "tests.t_a::t1"
    assert path_to_junit_key("tests/test_s.py::test_param[1]") == "tests.test_s::test_param[1]"


def test_junit_ids_to_paths_maps_via_collected_authority():
    collected = ("tests/test_a.py::test_ok", "tests/api/test_p.py::TestAll::test_empty")
    junit = ("tests.test_a::test_ok", "tests.api.test_p.TestAll::test_empty")
    assert junit_ids_to_paths(junit, collected) == \
        ("tests/test_a.py::test_ok", "tests/api/test_p.py::TestAll::test_empty")


def test_junit_ids_to_paths_keeps_unmapped_verbatim():
    # a junit id whose test was not in the collected list stays as-is (won't match gold)
    assert junit_ids_to_paths(("x.y::z",), ("tests/a.py::t",)) == ("x.y::z",)


# ---- load_gold() : parse rat_python50_gold.json shape -------------------------------------

def _mock_gold():
    return {
        "dataset": "rat_python50",
        "summary": {"total": 3, "certified": 2, "rejected": 1, "error": 0},
        "repos": {
            "jhao104/proxy_pool": {
                "full_name": "jhao104/proxy_pool", "sha": "A" * 40, "status": "CERTIFIED",
                "manifest_size": 3,
                "node_ids": ["src/tests/t_a.py::t1", "tests/t_a.py::t2", "tests/t_b.py::t3"],
                "reject_reasons": [], "error": None, "base_image": "python:3.11-slim",
            },
            "microsoft/markitdown": {
                "full_name": "microsoft/markitdown", "sha": "b" * 40, "status": "CERTIFIED",
                "manifest_size": 2, "node_ids": ["tests/t_c.py::t1", "tests/t_c.py::t2"],
                "reject_reasons": [], "error": None, "base_image": "python:3.11-slim",
            },
            "some/testless": {
                "full_name": "some/testless", "sha": "c" * 40, "status": "REJECTED",
                "manifest_size": 0, "node_ids": [], "reject_reasons": ["no items collected"],
                "error": None, "base_image": "python:3.11-slim",
            },
        },
    }


def test_load_gold_normalizes_node_ids_lowercases_key_and_sha():
    g = load_gold(_mock_gold())
    pp = g["jhao104/proxy_pool"]
    assert isinstance(pp, GoldRepo)
    # src/ prefix stripped by norm() so it can intersect a src-less collected id
    assert pp.node_ids == frozenset({"tests/t_a.py::t1", "tests/t_a.py::t2", "tests/t_b.py::t3"})
    assert pp.sha == "a" * 40 and pp.status == "CERTIFIED"
    assert g["some/testless"].status == "REJECTED" and g["some/testless"].node_ids == frozenset()


# ---- gold_coverage() : the C / P metric over MeasureRows ----------------------------------

def _row(repo, sha, collected, passed, **kw):
    base = dict(agent="a", repo=repo, env_status="ok", build_ok=True, executed=True, ebsr=True,
                collected_node_ids=tuple(collected), passed_node_ids=tuple(passed),
                meta={"head_sha": sha})
    base.update(kw)
    return MeasureRow(**base)


def test_gold_coverage_C_and_P_over_fixed_denominator():
    g = load_gold(_mock_gold())
    # proxy_pool: D=3; collects 2 of 3 gold, passes 1 of 3  -> C=2/3, P=1/3
    r = _row("jhao104/proxy_pool", "a" * 40,
             collected=["tests/t_a.py::t1", "tests/t_a.py::t2", "extra/not_gold.py::z"],
             passed=["tests/t_a.py::t1"])
    out = gold_coverage([r], g)
    assert out["n_gold_included"] == 1 and out["n_gold_excluded"] == 0
    assert out["EBSR_improved"] == round(2 / 3, 4)
    assert out["ESSR_improved"] == round(1 / 3, 4)
    bd = out["gold_breakdown"][0]
    assert bd["D"] == 3 and bd["collected_hit"] == 2 and bd["passed_hit"] == 1


def test_gold_coverage_sha_misaligned_excluded():
    g = load_gold(_mock_gold())
    r = _row("jhao104/proxy_pool", "f" * 40, collected=["tests/t_a.py::t1"], passed=["tests/t_a.py::t1"])
    out = gold_coverage([r], g)
    assert out["n_gold_included"] == 0 and out["n_gold_excluded"] == 1
    assert out["gold_excluded"][0] == {"repo": "jhao104/proxy_pool", "reason": "sha_misaligned"}


def test_gold_coverage_rejected_gold_excluded():
    g = load_gold(_mock_gold())
    r = _row("some/testless", "c" * 40, collected=["tests/t.py::t"], passed=[])
    out = gold_coverage([r], g)
    assert out["gold_excluded"][0]["reason"] == "gold_not_certified"


def test_gold_coverage_pending_gold_excluded_with_distinct_reason():
    # partial gold file carries status="PENDING" (repo still building) -> exclude, but log
    # it distinctly from terminal REJECTED/ERROR so a partial run is legible.
    g = load_gold({"repos": {"o/p": {"full_name": "o/p", "sha": "a" * 40, "status": "PENDING",
                                     "manifest_size": 0, "node_ids": []}}})
    r = _row("o/p", "a" * 40, collected=["t/a.py::t"], passed=[])
    out = gold_coverage([r], g)
    assert out["gold_excluded"][0]["reason"] == "gold_pending"


def test_gold_coverage_missing_gold_excluded():
    g = load_gold(_mock_gold())
    r = _row("owner/unknown", "z" * 40, collected=[], passed=[])
    out = gold_coverage([r], g)
    assert out["gold_excluded"][0]["reason"] == "gold_missing"


def test_gold_coverage_broken_env_scores_zero_not_excluded():
    # aligned + certified but the env built nothing -> C=0/P=0 (the whole point), NOT excluded
    g = load_gold(_mock_gold())
    r = _row("microsoft/markitdown", "b" * 40, collected=[], passed=[], build_ok=False,
             executed=False, ebsr=False)
    out = gold_coverage([r], g)
    assert out["n_gold_included"] == 1 and out["EBSR_improved"] == 0.0 and out["ESSR_improved"] == 0.0
