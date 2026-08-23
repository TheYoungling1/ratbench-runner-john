"""P2.1 — the clean two-phase / two-oracle sequence in ``build_dep_graph``.

Phase A is the resolve -> install -> look -> repair fixpoint whose loop condition
is the RECORD-union coverage oracle (``resolved_record_coverage``); Phase B is the
tier descent that STARTS with the certified relink and then derives system deps
from the same converged closure:

    Phase B:  certified_import_links -> ldd_probe -> import_probe
              -> reconcile_apt_names -> certify_all

These tests pin four properties:
  1. relink runs BEFORE ldd in Phase B (the load-bearing reorder);
  2. ldd runs on the POST-repair closure (a Phase-A repair's .so is ldd-probed);
  3. a ``magic``/``libmagic`` native-load gap surfaces in Phase B, never as a
     Phase-A under-declaration repair (metadata-present => not in the missing set);
  4. the Phase-A loop is driven by ``resolved_record_coverage`` (consulted once
     per round), NOT by a per-round ``packages_distributions()`` probe (which is
     called exactly once, for the single Phase-B relink).

All hermetic: a fake ``record_provider`` is injected (so the production composite's
installed leg — which itself calls ``packages_distributions`` — is never built),
and the executor is a ``FakeExecutor`` / ``SequencedFakeExecutor``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from conftest import FakeExecutor, SequencedFakeExecutor  # type: ignore

from python_deps.depgraph.build import build_dep_graph
from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.ids import import_id, package_id, syslib_id
from python_deps.depgraph.schema import DiscoveredBy, NodeType
from python_deps.import_mapping import normalize_package_name


def _r(returncode=0, stdout="", stderr=""):
    return CommandResult(command="", returncode=returncode, stdout=stdout, stderr=stderr)


def _provider(mapping):
    """Fake RECORD provider: canon-dist -> {top-level modules} (else None)."""
    norm = {normalize_package_name(k): set(v) for k, v in mapping.items()}

    def provider(dist):
        return norm.get(normalize_package_name(dist))

    return provider


def _audit_packages(graph):
    return [
        n
        for n in graph.nodes
        if n.type is NodeType.PACKAGE and n.discovered_by is DiscoveredBy.AUDIT
    ]


# --------------------------------------------------------------------------- #
# 1. relink precedes ldd in Phase B (the reorder)                             #
# --------------------------------------------------------------------------- #
def test_relink_runs_before_ldd_in_phase_b(tmp_path):
    """The certified relink (``packages_distributions``) is issued BEFORE the ldd
    stage (``locate_file`` — the EXT_SO_MAP round-trip that opens ldd_probe).

    With a fake ``record_provider`` injected, the ONLY ``packages_distributions``
    call in the whole build is the Phase-B relink, so keying on that substring is
    unambiguous. RED before the reorder: today ldd runs first, so the ldd command
    precedes the relink command.
    """
    (tmp_path / "app.py").write_text("import requests\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fx"\nversion="0"\ndependencies=["requests"]\n'
    )
    ex = FakeExecutor(
        responses={"uv pip compile": _r(stdout="requests==2.31.0\n    # via -r -\n")},
        default=_r(0),
    )
    provider = _provider({"requests": {"requests"}})

    build_dep_graph(
        str(tmp_path),
        ex,
        host_executor=ex,
        record_provider=provider,
        exclude_newer="2024-01-01",
    )

    relink_idx = next(i for i, c in enumerate(ex.calls) if "packages_distributions" in c)
    ldd_stage_idx = next(i for i, c in enumerate(ex.calls) if "locate_file" in c)
    assert relink_idx < ldd_stage_idx, (
        "Phase B must run the certified relink before ldd_probe "
        f"(relink@{relink_idx}, ldd@{ldd_stage_idx})"
    )


# --------------------------------------------------------------------------- #
# 2. ldd runs on the POST-repair closure                                      #
# --------------------------------------------------------------------------- #
def test_ldd_probes_the_post_repair_closure(tmp_path):
    """An under-declared ``yaml`` is repaired to ``PyYAML`` in Phase A; ldd of the
    repaired dist's ``_yaml*.so`` reports ``libyaml.so.2 => not found``. The
    ``libyaml`` SystemLib exists only because ldd ran AFTER the repair add (Phase B
    on the converged closure), not before convergence.
    """
    so_path = "/usr/lib/python3.11/site-packages/_yaml.cpython-311-x86_64-linux-gnu.so"
    ex = SequencedFakeExecutor(
        responses={
            "uv lock": [_r(1, stderr="lock unavailable")],  # force pip-compile fallback
            "uv pip compile": [_r(0, stdout="PyYAML==6.0\n    # via -r -\n")],
            "locate_file": [_r(0, stdout=json.dumps({"pyyaml": [so_path]}))],
            "ldd ": [_r(0, stdout=f"{so_path}:\n\tlibyaml.so.2 => not found\n")],
        },
        default=_r(0),
    )
    (tmp_path / "app.py").write_text("import yaml\n")  # declares nothing
    provider = _provider({"PyYAML": {"yaml"}})

    graph = build_dep_graph(str(tmp_path), ex, host_executor=ex, record_provider=provider)

    pyyaml = graph.get(package_id("PyYAML", "6.0"))
    assert pyyaml is not None and pyyaml.discovered_by is DiscoveredBy.AUDIT

    libyaml = graph.get(syslib_id("libyaml.so.2"))
    assert libyaml is not None, "ldd must run on the post-repair closure"
    assert libyaml.type is NodeType.SYSTEM_LIB
    assert (package_id("PyYAML", "6.0"), syslib_id("libyaml.so.2")) in {
        (e.src, e.dst) for e in graph.edges
    }


# --------------------------------------------------------------------------- #
# 3. magic/libmagic routes to Phase B, not Phase A                            #
# --------------------------------------------------------------------------- #
def test_magic_native_gap_surfaces_in_phase_b_not_phase_a(tmp_path):
    """``python-magic`` is metadata-present (RECORD provides ``magic``) => ``magic``
    is NOT in the Phase-A missing set => zero repair rounds, no AUDIT package. The
    real gap (native ``libmagic``) surfaces from the Phase-B import probe.
    """
    (tmp_path / "app.py").write_text("import magic\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="fx"\nversion="0"\ndependencies=["python-magic"]\n'
    )
    ex = FakeExecutor(
        responses={
            "uv pip compile": _r(stdout="python-magic==0.4.27\n    # via -r -\n"),
            # Phase-B relink certifies import ``magic`` -> dist ``python-magic``
            # (the real container reports this), so ``magic`` is not falsely flagged
            # under-declared — it is provided, its only gap is the native lib.
            "packages_distributions": _r(stdout=json.dumps({"magic": ["python-magic"]})),
            "import magic": _r(
                returncode=1,
                stderr=(
                    "ImportError: libmagic.so.1: cannot open shared object file: "
                    "No such file or directory"
                ),
            ),
        },
        default=_r(0),
    )
    provider = _provider({"python-magic": {"magic"}})

    graph = build_dep_graph(
        str(tmp_path),
        ex,
        host_executor=ex,
        record_provider=provider,
        exclude_newer="2024-01-01",
    )

    # Metadata-present => no Phase-A under-declaration repair.
    assert _audit_packages(graph) == []
    assert graph.get(import_id("magic")).data.get("unresolved") is not True

    # Phase-B import probe surfaced the native libmagic gap.
    libmagic = graph.get(syslib_id("libmagic.so.1"))
    assert libmagic is not None, "libmagic must surface from the Phase-B import probe"
    assert libmagic.type is NodeType.SYSTEM_LIB
    assert libmagic.discovered_by is DiscoveredBy.PROBE


# --------------------------------------------------------------------------- #
# 4. RECORD drives the loop; no per-round packages_distributions              #
# --------------------------------------------------------------------------- #
def test_record_coverage_drives_loop_not_per_round_packages_distributions(tmp_path):
    """Over a multi-round repair build, ``resolved_record_coverage`` is consulted
    once per round (it is the loop condition), while ``packages_distributions`` is
    called EXACTLY once — the single Phase-B relink — never once per round.

    A fake ``record_provider`` is injected, so the production composite's installed
    leg (which would add one memoized ``packages_distributions`` call) is never
    built; the count is therefore exactly the single relink call, independent of
    the round count.
    """
    import python_deps.depgraph.build as build_mod

    (tmp_path / "app.py").write_text("import yaml\n")  # under-declared -> >=1 repair round
    ex = SequencedFakeExecutor(
        responses={
            "uv lock": [_r(1, stderr="lock unavailable")],
            "uv pip compile": [_r(0, stdout="PyYAML==6.0\n    # via -r -\n")],
        },
        default=_r(0),
    )
    provider = _provider({"PyYAML": {"yaml"}})

    resolve_calls = {"n": 0}
    coverage_calls = {"n": 0}
    orig_resolve = build_mod.resolve_closure
    orig_cov = build_mod.resolved_record_coverage

    def spy_resolve(*a, **k):
        resolve_calls["n"] += 1
        return orig_resolve(*a, **k)

    def spy_cov(*a, **k):
        coverage_calls["n"] += 1
        return orig_cov(*a, **k)

    with patch.object(build_mod, "resolve_closure", side_effect=spy_resolve), patch.object(
        build_mod, "resolved_record_coverage", side_effect=spy_cov
    ):
        build_dep_graph(str(tmp_path), ex, host_executor=ex, record_provider=provider)

    # A repair round happened (>= 2 resolves), and coverage was consulted each round.
    assert resolve_calls["n"] >= 2
    assert coverage_calls["n"] == resolve_calls["n"]

    # packages_distributions is the single Phase-B relink call, NOT per-round.
    pd_calls = sum(1 for c in ex.calls if "packages_distributions" in c)
    assert pd_calls == 1
