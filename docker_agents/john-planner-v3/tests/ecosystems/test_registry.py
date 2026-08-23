import pytest

from ecosystems.base import CertifyMode
from ecosystems.registry import select_provider


class _Stub:
    def __init__(self, name, score):
        self.name = name
        self._score = score
        self.certify_mode = CertifyMode.INSTALL

    def detect(self, repo):
        return self._score

    def closure_mode_for(self, repo): ...
    def package_obligations(self, *a, **k): ...
    def native_obligations(self, *a, **k): ...


def test_selects_highest_above_threshold():
    assert select_provider("/r", [_Stub("b", 0.4), _Stub("a", 0.9)]).name == "a"


def test_ties_first_registered_wins():
    assert select_provider("/r", [_Stub("a", 0.7), _Stub("b", 0.7)]).name == "a"


def test_below_threshold_raises_lookup_error():
    with pytest.raises(LookupError):
        select_provider("/r", [_Stub("a", 0.2)], threshold=0.5)


def test_threshold_boundary_is_inclusive():
    assert select_provider("/r", [_Stub("a", 0.5)]).name == "a"


def test_default_returned_when_none_clears_threshold():
    fallback = _Stub("fallback", 0.0)
    assert select_provider("/r", [_Stub("a", 0.2)], default=fallback) is fallback
