from src.eval.graph_repair_ablation.inject import apply_injection
from src.eval.graph_repair_ablation.oracle import Injection


def _inj(mutation):
    return Injection("x", "r", "img", "SYSLIB_MISSING", mutation, {"kind": "install", "target": "t"})


def test_strip_line_removes_matching_line():
    script = "apt-get install -y libgraphviz-dev pkgconf\npip install -e .\n"
    out = apply_injection(script, _inj({"op": "strip_line", "match": "libgraphviz-dev"}))
    assert "libgraphviz-dev" not in out
    assert "pip install -e ." in out          # other lines preserved


def test_strip_line_absent_match_raises():
    import pytest
    with pytest.raises(ValueError):
        apply_injection("pip install -e .\n", _inj({"op": "strip_line", "match": "NOPE"}))


def test_add_install_pkg_appends_bad_pip_pkg():
    out = apply_injection("pip install -e .\n", _inj({"op": "add_install_pkg", "pkg": "badpkg==0.0.0"}))
    assert "badpkg==0.0.0" in out


def test_add_pin_appends_conflicting_pin():
    out = apply_injection("pip install -e .\n", _inj({"op": "add_pin", "pkg": "urllib3", "spec": "==1.20"}))
    assert "urllib3==1.20" in out


def test_unknown_op_raises():
    import pytest
    with pytest.raises(ValueError):
        apply_injection("x\n", _inj({"op": "frobnicate"}))
