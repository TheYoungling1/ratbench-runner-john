"""Task CR7 (Inc 4a) — certify the clean setup-shape Service + demote counter.

CR6 (Inc 3) landed the clean setup-shape Service node: a node carrying
``data["setup"]`` (the provisioning recipe) and a probe-poll ``check_command``.
CR7 makes ``certify`` actually certify it — the SERVICE gate qualifies a node iff
it carries a ``setup`` recipe — and adds the anti-deadlock demote counter
``certify_fail_count`` (SERVICE MISSING increments, SERVICE SATISFIED resets) so a
never-provisionable service demotes instead of deadlocking the "done" gate.

Pure unit tests — no Docker/network. All use the conftest ``FakeExecutor``.
"""

from __future__ import annotations

from python_deps.depgraph.certify import certify
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Layer,
    Node,
    NodeType,
    State,
)

_REDIS_ID = "service:redis"
_REDIS_PROBE = "for i in $(seq 1 15); do nc -z 127.0.0.1 6379 && exit 0; sleep 2; done; exit 1"


def _setup_service(state: State = State.MISSING) -> Node:
    """A clean setup-shape Service (CR6): ``data['setup']`` present and a
    probe-poll check_command — the SERVICE gate admits it on the ``setup`` key."""
    return Node(
        id=_REDIS_ID,
        type=NodeType.SERVICE,
        name="redis",
        layer=Layer.SERVICES,
        discovered_by=DiscoveredBy.CLASSIFIER,
        state=state,
        check_command=_REDIS_PROBE,
        data={
            "setup": {"image": "redis:7-alpine", "ports": [6379]},
            "service_kind": "redis",
        },
    )


def test_setup_service_certifies_when_allowed(fake_executor, make_result_fixture):
    # The SERVICE gate admits the setup node on the ``setup`` key alone — an ok
    # probe flips it SATISFIED.
    fake_executor.default = make_result_fixture(returncode=0)
    g = DepGraph().with_node(_setup_service())

    out = certify(g, _REDIS_ID, fake_executor, allow_service_certify=True)

    assert out.get(_REDIS_ID).state is State.SATISFIED
    assert fake_executor.calls == [_REDIS_PROBE]  # the probe actually ran


def test_setup_service_skipped_when_not_allowed(fake_executor, make_result_fixture):
    # allow_service_certify=False → gate returns the graph early; nothing ran.
    fake_executor.default = make_result_fixture(returncode=0)
    g = DepGraph().with_node(_setup_service())

    out = certify(g, _REDIS_ID, fake_executor)  # default: not allowed

    assert out.get(_REDIS_ID).state is State.MISSING  # unchanged
    assert fake_executor.calls == []  # scratch container never probed


def test_failing_setup_service_increments_fail_count(make_result_fixture):
    # The must-verify invariant: a never-provisionable service accrues a fail
    # count across cycles so it can DEMOTE instead of deadlocking "done".
    from conftest import FakeExecutor  # type: ignore

    ex = FakeExecutor(default=make_result_fixture(returncode=1, stderr="redis down"))
    g = DepGraph().with_node(_setup_service())

    g = certify(g, _REDIS_ID, ex, cycle=1, allow_service_certify=True)
    assert g.get(_REDIS_ID).state is State.MISSING
    assert g.get(_REDIS_ID).data["certify_fail_count"] == 1

    g = certify(g, _REDIS_ID, ex, cycle=2, allow_service_certify=True)
    assert g.get(_REDIS_ID).data["certify_fail_count"] == 2

    g = certify(g, _REDIS_ID, ex, cycle=3, allow_service_certify=True)
    assert g.get(_REDIS_ID).data["certify_fail_count"] == 3
    # the setup recipe survives the immutable data update
    assert g.get(_REDIS_ID).data["setup"]["ports"] == [6379]


def test_success_resets_fail_count(make_result_fixture):
    from conftest import FakeExecutor  # type: ignore

    red = FakeExecutor(default=make_result_fixture(returncode=1, stderr="down"))
    green = FakeExecutor(default=make_result_fixture(returncode=0))
    g = DepGraph().with_node(_setup_service())

    g = certify(g, _REDIS_ID, red, cycle=1, allow_service_certify=True)
    g = certify(g, _REDIS_ID, red, cycle=2, allow_service_certify=True)
    assert g.get(_REDIS_ID).data["certify_fail_count"] == 2

    g = certify(g, _REDIS_ID, green, cycle=3, allow_service_certify=True)
    assert g.get(_REDIS_ID).state is State.SATISFIED
    assert g.get(_REDIS_ID).data.get("certify_fail_count", 0) == 0
