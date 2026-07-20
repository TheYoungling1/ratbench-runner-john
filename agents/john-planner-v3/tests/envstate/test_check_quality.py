import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import DiscoveredBy, Layer, Node, NodeType, State
from src.envstate.check_quality import rewrite_syslib_check, check_can_detect_absence


def _syslib(check, *, node_id="syslib:libglib2.0-0", name="libglib2.0-0"):
    return Node(id=node_id, type=NodeType.SYSTEM_LIB, name=name,
                layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER, state=State.MISSING,
                check_command=check, chosen_fix="apt:libglib2.0-0")


def test_rewrite_dpkg_s_to_soname_capability_check():
    # Node carries a real soname (ldd-probe style) → dpkg check rewritten to an ldconfig
    # capability check that greps the actual soname literally.
    node = _syslib("dpkg -s libgl1", node_id="syslib:libGL.so.1", name="libGL.so.1")
    out = rewrite_syslib_check(node)
    assert out is not None and "dpkg -s" not in out
    assert "ldconfig" in out and "libGL.so.1" in out
    assert "grep -qF" in out  # fixed-string so '.' is literal


def test_rewrite_returns_none_for_package_name_only_node():
    # apt PACKAGE name (no soname) → cannot map to the real soname deterministically →
    # return None rather than emit a check that would false-negative.
    assert rewrite_syslib_check(_syslib("dpkg -s libglib2.0-0")) is None


def test_rewrite_returns_none_for_already_capability_check():
    node = _syslib("ldconfig -p | grep -q libGL", node_id="syslib:libGL.so.1", name="libGL.so.1")
    assert rewrite_syslib_check(node) is None


def test_can_detect_absence_true_for_real_check():
    assert check_can_detect_absence("dpkg -s libgl1") is True
    assert check_can_detect_absence("python -c 'import cv2'") is True
    assert check_can_detect_absence("test -f fixtures.db") is True   # file-test operator
    assert check_can_detect_absence("[ -e /usr/lib/libfoo.so ]") is True


def test_can_detect_absence_false_for_trivial():
    assert check_can_detect_absence("true") is False
    assert check_can_detect_absence("echo ok") is False
    assert check_can_detect_absence("ls /") is False
    assert check_can_detect_absence("test 1 = 1") is False           # constant predicate
    assert check_can_detect_absence("test -n ok") is False           # string test, not file
    assert check_can_detect_absence("[ 1 = 1 ]") is False
