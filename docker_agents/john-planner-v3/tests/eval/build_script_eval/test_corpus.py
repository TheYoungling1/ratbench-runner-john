import pytest
from src.eval.build_script_eval.corpus import CORPUS, STRATA, RepoSpec, select


def test_strata_are_control_and_syslib():
    assert STRATA == frozenset({"S_control", "S_syslib"})


def test_every_row_has_a_valid_stratum_and_unique_name():
    names = [r.name for r in CORPUS]
    assert len(names) == len(set(names)), "duplicate repo dir names"
    assert all(r.stratum in STRATA for r in CORPUS)
    assert any(r.stratum == "S_control" for r in CORPUS)
    assert any(r.stratum == "S_syslib" for r in CORPUS)


def test_select_by_stratum():
    rows = select(strata=frozenset({"S_syslib"}))
    assert rows and all(r.stratum == "S_syslib" for r in rows)


def test_select_by_name():
    one = CORPUS[0]
    assert {r.name for r in select(only=frozenset({one.name}))} == {one.name}


def test_select_empty_is_full_corpus():
    assert len(select()) == len(CORPUS)


def test_select_unknown_stratum_raises():
    with pytest.raises(ValueError):
        select(strata=frozenset({"S_bogus"}))


def test_select_unknown_name_raises():
    with pytest.raises(ValueError):
        select(only=frozenset({"no-such-repo"}))
