from src.eval.build_script_eval.scorecard import (
    LadderResult, attribute_failure, classify_pytest_result, env_works_passed, extract_gaps,
)


def _ladder(**kw):
    base = dict(install_ok=True, env_works=True, tests_ran=True, tests_passed=True,
                highest_rung="tests_passed", reason=None, first_failure=None, gaps=())
    base.update(kw)
    return LadderResult(**base)


# --- classify_pytest_result ---
def test_pytest_rc0_ran_and_passed():
    assert classify_pytest_result(0) == (True, True, None)

def test_pytest_rc1_ran_not_passed():
    assert classify_pytest_result(1) == (True, False, "tests_failed")

def test_pytest_rc5_no_tests_collected():
    assert classify_pytest_result(5) == (False, False, "no_tests_collected")

def test_pytest_rc2_collection_error():
    assert classify_pytest_result(2) == (False, False, "collection_or_usage_error")

def test_pytest_timeout():
    assert classify_pytest_result(124) == (False, False, "timeout")


# --- env_works_passed (headline gate) ---
def test_env_works_gate_true():
    assert env_works_passed(_ladder(install_ok=True, env_works=True)) is True

def test_env_works_gate_false_when_install_failed():
    assert env_works_passed(_ladder(install_ok=False, env_works=False)) is False

def test_env_works_gate_false_when_env_broken():
    assert env_works_passed(_ladder(install_ok=True, env_works=False)) is False


# --- extract_gaps ---
def test_extract_gaps_splits_language_and_system_and_drops_service():
    gaps = (
        {"tier": "PACKAGE", "id": "requests", "evidence": "..."},
        {"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": "..."},
        {"tier": "TOOL", "id": "pg_config", "evidence": "..."},
        {"tier": "SERVICE", "id": "unknown", "evidence": "..."},
    )
    lang, sys_ = extract_gaps(gaps)
    assert [g["id"] for g in lang] == ["requests"]
    assert {g["id"] for g in sys_} == {"libpq.so.5", "pg_config"}


# --- attribute_failure ---
def test_attribute_infeasible_shortcircuits():
    assert attribute_failure(_ladder(), static_ok=True, top_import="x", feasible=False) == "infeasible"

def test_attribute_pass_when_env_works():
    assert attribute_failure(_ladder(install_ok=True, env_works=True),
                             static_ok=True, top_import="x", feasible=True) == "pass"

def test_attribute_render_bug_when_static_fails():
    lad = _ladder(install_ok=True, env_works=False, gaps=())
    assert attribute_failure(lad, static_ok=False, top_import="x", feasible=True) == "render_bug"

def test_attribute_system_gap_wins_over_package():
    lad = _ladder(install_ok=True, env_works=False, gaps=(
        {"tier": "PACKAGE", "id": "foo", "evidence": ""},
        {"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": ""},
    ))
    assert attribute_failure(lad, static_ok=True, top_import="app", feasible=True) == "system_gap"

def test_attribute_own_package_is_render_bug():
    lad = _ladder(install_ok=True, env_works=False, gaps=(
        {"tier": "PACKAGE", "id": "myapp", "evidence": ""},
    ))
    assert attribute_failure(lad, static_ok=True, top_import="myapp", feasible=True) == "render_bug"

def test_attribute_language_gap_for_third_party_missing():
    lad = _ladder(install_ok=True, env_works=False, gaps=(
        {"tier": "PACKAGE", "id": "requests", "evidence": ""},
    ))
    assert attribute_failure(lad, static_ok=True, top_import="myapp", feasible=True) == "language_gap"

def test_attribute_install_failure_apt_is_system_gap():
    lad = _ladder(install_ok=False, env_works=False, gaps=(),
                  first_failure={"command": "apt-get install -y libpq-dev",
                                 "stderr_tail": "E: Unable to locate package libpq-dev"})
    assert attribute_failure(lad, static_ok=True, top_import="app", feasible=True) == "system_gap"

def test_attribute_install_failure_pip_is_language_gap():
    lad = _ladder(install_ok=False, env_works=False, gaps=(),
                  first_failure={"command": "pip install foo",
                                 "stderr_tail": "ERROR: Could not find a version that satisfies foo"})
    assert attribute_failure(lad, static_ok=True, top_import="app", feasible=True) == "language_gap"


from src.eval.build_script_eval.scorecard import _assemble_scorecard


class _FakeGraph:
    nodes = ()


def test_assemble_scorecard_pass_row(monkeypatch):
    import src.eval.build_script_eval.scorecard as sc
    monkeypatch.setattr(sc, "apt_names_in_graph", lambda g: frozenset({"libpq-dev"}))
    monkeypatch.setattr(sc, "package_versions_in_graph", lambda g: {"psycopg2": "2.9.9"})
    ladder = _ladder(install_ok=True, env_works=True, tests_ran=True, tests_passed=False,
                     highest_rung="tests_ran", reason="tests_failed")
    row = _assemble_scorecard(
        "psycopg/psycopg2", "S_syslib", True, "python:3.11-slim", "3.11",
        _FakeGraph(), True, "psycopg2", ladder,
    )
    assert row["repo"] == "psycopg/psycopg2"
    assert row["stratum"] == "S_syslib"
    assert row["first_pass_env_works"] is True          # headline gate
    assert row["attribution"] == "pass"
    assert row["highest_rung"] == "tests_ran"
    assert row["predicted_apt"] == ["libpq-dev"]
    assert row["feasible"] is True
    # coverage.missing_node_clusters reads this exact key:
    assert "execution_missing" in row


def test_assemble_scorecard_system_gap_row(monkeypatch):
    import src.eval.build_script_eval.scorecard as sc
    monkeypatch.setattr(sc, "apt_names_in_graph", lambda g: frozenset())
    monkeypatch.setattr(sc, "package_versions_in_graph", lambda g: {})
    ladder = _ladder(install_ok=True, env_works=False, tests_ran=False, tests_passed=False,
                     highest_rung="install", reason="env_broken",
                     gaps=({"tier": "SYSTEM_LIB", "id": "libpq.so.5", "evidence": "cannot open"},
                           {"tier": "SERVICE", "id": "unknown", "evidence": ""}))
    row = _assemble_scorecard("x/y", "S_syslib", True, "python:3.11-slim", "3.11",
                              _FakeGraph(), True, "y", ladder)
    assert row["first_pass_env_works"] is False
    assert row["attribution"] == "system_gap"
    assert [g["id"] for g in row["system_gaps"]] == ["libpq.so.5"]
    # execution_missing EXCLUDES the SERVICE gap (out of scope), even though
    # ladder.gaps (the raw, unfiltered ladder output) still contains it.
    assert {g["tier"] for g in row["execution_missing"]} == {"SYSTEM_LIB"}
    assert any(g["tier"] == "SERVICE" for g in ladder.gaps)
