import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"
for p in (str(_ROOT), str(_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from python_deps.depgraph.schema import DepGraph, Node, NodeType, Layer, State, DiscoveredBy
from src.envstate.block_emit import block_emit
from src.envstate.ledger import ActionLedger


def _graph():
    return DepGraph().with_node(Node(id="syslib:libpq.so", type=NodeType.SYSTEM_LIB,
        name="libpq.so", layer=Layer.SYSTEM, discovered_by=DiscoveredBy.RESOLVER,
        state=State.MISSING, check_command="ldconfig -p | grep -q libpq",
        chosen_fix="apt:libpq-dev"))


def test_runs_blocks_certifies_and_dual_writes_ledger():
    led = ActionLedger()
    def sandbox(cmd): return (True, "installed")
    def ro(cmd): return (0, "libpq") if "ldconfig" in cmd else (1, "")
    graph, bundle, failed = block_emit(_graph(), sandbox, ro, led, cycle=1)
    assert failed is None
    assert graph.get("syslib:libpq.so").state is State.SATISFIED      # host check certified
    assert len(bundle.items) >= 1                                     # typed evidence emitted
    # dual-write: the install command is mirrored into the ledger with rc 0
    assert any("libpq-dev" in e.cmd and e.rc == 0 for e in led.events())


def test_failed_block_is_recorded_in_ledger_with_rc_nonzero():
    led = ActionLedger()
    def sandbox(cmd): return (False, "E: package not found")
    def ro(cmd): return (1, "")
    graph, bundle, failed = block_emit(_graph(), sandbox, ro, led, cycle=1)
    assert failed == "system.libpq.so"
    assert any(e.rc != 0 for e in led.events())                      # failures feed runtime_ingest


def test_check_fails_so_node_not_certified():
    led = ActionLedger()
    def sandbox(cmd): return (True, "ok")          # install "succeeds" ...
    def ro(cmd): return (1, "absent")              # ... but the host check fails
    graph, _b, _f = block_emit(_graph(), sandbox, ro, led, cycle=1)
    assert graph.get("syslib:libpq.so").state is not State.SATISFIED   # block rc=0 never certifies
