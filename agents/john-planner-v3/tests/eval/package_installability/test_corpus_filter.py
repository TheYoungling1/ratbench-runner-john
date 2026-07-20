"""Characterization of the corpus filter (corpus.select_corpus) from Task 0.1.

Covers name selection, stratum selection, empty=full-corpus, and the fail-fast
ValueError paths for an unknown stratum and an unknown --only name.
"""
import pytest

from src.eval.package_installability.corpus import CORPUS, select_corpus


def test_select_by_name():
    picked = select_corpus(only=frozenset({"psycopg2", "pyodbc"}))
    assert {s.name for s in picked} == {"psycopg2", "pyodbc"}


def test_select_by_stratum():
    picked = select_corpus(strata=frozenset({"S1"}))
    assert picked  # S1 is populated
    assert all(s.stratum == "S1" for s in picked)


def test_empty_selection_is_full_corpus():
    assert len(select_corpus()) == len(CORPUS)


def test_unknown_stratum_raises():
    with pytest.raises(ValueError):
        select_corpus(strata=frozenset({"S99"}))


def test_unknown_only_name_raises():
    with pytest.raises(ValueError):
        select_corpus(only=frozenset({"not-a-real-package"}))
