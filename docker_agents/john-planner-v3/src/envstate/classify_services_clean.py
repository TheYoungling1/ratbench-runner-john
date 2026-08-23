"""Deterministic Service + Config classifier (clean tier). The construction default.

The LLM-free replacement for the env-classifier's Service+Config output. It reads the
repo, translates each declared compose service to a CLEAN setup-shape Service node (via
the shipped :func:`service_translate.translate_service`), repoints config DSNs into
``setup["bind"]``, and emits advisory Config hint nodes — all admitted through the pure
:func:`patch_gate.admit_proposal`.

Kept in its OWN module (not folded into the now-deleted ``env_classifier.py``). Inc 5B
wired this in as the construction-time default; ``make_construction_classifier`` (below)
is the entrypoint ``run_v3_e2e`` / ``build_advisory_for_repo`` call.

Best-effort, exactly like the old classify: the whole body is wrapped in a try/except
that returns the input ``graph`` on any error — this NEVER raises into the build. Every
admitted ``NodeSpec.evidence_ref`` is a real ``collect_static_evidence`` bundle id (else
``validate_proposal`` would reject the batch); this mirrors the old classify's
``known_evidence_ids=bundle_ids`` handling.
"""
from __future__ import annotations

import logging

from python_deps.depgraph.config_scan import (
    parse_env_example,
    scan_env_defaults,
    scan_env_reads,
    scan_framework_config_reads,
)
from python_deps.depgraph.patch import NodeSpec, PatchProposal
from python_deps.depgraph.patch_gate import admit_proposal
from python_deps.depgraph.provisioning_spec import iter_provisioning_specs
from python_deps.depgraph.repoint import render_bind_steps
from python_deps.depgraph.service_scan import service_from_url
from python_deps.depgraph.static_collect import collect_static_evidence

# Module-level so tests can monkeypatch it (no client is ever called with canned results).
from src.envstate.service_translate import translate_service

logger = logging.getLogger(__name__)

# Evidence kinds that name a concrete service/kind — preferred anchor for a Service node.
_STRONG_SERVICE_KINDS = frozenset({"compose_service", "service_binding", "ci_service"})
# Evidence kinds that name a read/declared env var — preferred anchor for a Config node.
_CONFIG_EVIDENCE_KINDS = frozenset({"env_read", "env_var"})


def _dsn_configs(repo_path: str) -> list[tuple[str, str]]:
    """`(var, dsn)` pairs from env defaults + `.env.example` whose value is a service DSN.

    The repoint source: only values ``service_from_url`` recognizes as a DSN are kept.
    Order is deterministic (defaults first, then example) and each var appears once.
    """
    configs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source in (scan_env_defaults(repo_path), parse_env_example(repo_path)):
        for var, value in source.items():
            if var in seen or service_from_url(value) is None:
                continue
            configs.append((var, value))
            seen.add(var)
    return configs


def _service_evidence(hits, service_name: str, kind: str | None) -> str:
    """A bundle id anchoring this service: a strong hit naming it, else any bundle id."""
    for h in hits:
        if h.kind in _STRONG_SERVICE_KINDS and h.name in (service_name, kind):
            return h.evidence_id
    return hits[0].evidence_id


def _config_evidence(hits, var: str) -> str:
    """A bundle id anchoring this config var: a hit naming it, else any bundle id."""
    for h in hits:
        if h.kind in _CONFIG_EVIDENCE_KINDS and h.name == var:
            return h.evidence_id
    return hits[0].evidence_id


def _service_nodes(repo_path, arch, client, model, hits, configs) -> list[NodeSpec]:
    """One CLEAN setup-shape Service NodeSpec per declared, PROVISIONABLE compose
    service. Two guards keep the app itself and one bad service from poisoning the
    whole batch (real-repo e2e finding 2026-07-06):

    - A service with no recognized kind that is either ``build:``-based (locally
      built — the application under test, e.g. web/worker/js/css) or has no pulled
      ``image:`` at all is NOT a dependency to provision. Skip it BEFORE
      ``translate_service`` — routing it to the exotic LLM branch wastes a call
      (and, with no client, raises). ``build:`` is checked explicitly because the
      common ``build:`` + ``image:`` tag-a-local-build pattern still carries an
      image, so an image alone cannot distinguish the app from a pulled dependency.
    - Each service is translated in isolation: one failure (an exotic image with no
      client available, an LLM/parse error) skips only THAT service instead of
      unwinding the batch and discarding the others that translated cleanly.
    """
    nodes: list[NodeSpec] = []
    seen_ids: set[str] = set()
    for spec in iter_provisioning_specs(repo_path):
        node_id = f"service:{spec.service_name}"
        if node_id in seen_ids:
            continue
        # The app itself (locally built or image-less, unrecognized) — never a
        # backing dependency. Trace the skip so a future zero-services surprise is
        # diagnosable (the original silent-drop incident had no such trace).
        if spec.kind is None and (spec.build or not spec.image):
            logger.debug("skipping non-provisionable service %s (kind=None, build=%s, image=%r)",
                         spec.service_name, spec.build, spec.image)
            continue
        try:
            res = translate_service(client, model, spec, arch)
            setup = res.get("setup")
            # Skip a parse-failed (setup=None) or probe-less service: a probe-less setup
            # would render render_probe_poll("") — a broken shell that can never demote at
            # certify — and validate_proposal rejects it anyway. Never admit one.
            if setup is None or not setup.get("probe"):
                continue
            # Immutable: rebuild the setup dict to attach `bind` — never mutate translate's dict.
            new_setup = dict(setup)
            new_setup["bind"] = render_bind_steps([spec], configs)
            kind = res.get("kind")
            # Setup-shape Service nodes may carry an EXOTIC kind (couchdb, qdrant, …);
            # _requirement_errors relaxes the KNOWN_SERVICE_KINDS check for setup nodes.
            nodes.append(NodeSpec(
                id=node_id, type="Service", name=spec.service_name, layer="services",
                setup=new_setup, service_kind=kind,
                evidence_ref=_service_evidence(hits, spec.service_name, kind),
            ))
            seen_ids.add(node_id)
        except Exception as exc:  # noqa: BLE001 — best-effort, scoped to this one service
            logger.warning("clean service node skipped for %s: %s", spec.service_name, exc)
            continue
    return nodes


def _config_nodes(repo_path, hits) -> list[NodeSpec]:
    """One advisory Config hint NodeSpec per env var the tests read (never scheduled)."""
    read_vars = {**scan_env_reads(repo_path), **scan_framework_config_reads(repo_path)}
    return [
        NodeSpec(
            id=f"config:{var}", type="Config", name=var, layer="config",
            promotion="hint", check_command=None, evidence_ref=_config_evidence(hits, var),
        )
        for var in sorted(read_vars)
    ]


def classify_services_clean(graph, repo_path: str, client=None, model: str = "",
                            arch: dict | None = None):
    """Admit deterministic Service + Config nodes for ``repo_path`` into ``graph``.

    Returns a NEW graph with the admitted nodes, or the input ``graph`` unchanged on a
    rejected proposal or ANY error (best-effort — never crashes the build).
    """
    try:
        arch = arch or {}
        hits = collect_static_evidence(repo_path, graph)
        if not hits:
            return graph
        bundle_ids = frozenset(h.evidence_id for h in hits)

        configs = _dsn_configs(repo_path)
        service_nodes = _service_nodes(repo_path, arch, client, model, hits, configs)
        config_nodes = _config_nodes(repo_path, hits)

        proposal = PatchProposal(
            add_requirements=tuple(service_nodes + config_nodes), add_edges=())
        if proposal.is_empty():
            return graph
        result = admit_proposal(graph, proposal, known_evidence_ids=bundle_ids)
        if not result.accepted:
            logger.warning("clean service/config proposal rejected: %s", result.errors)
            return graph
        return result.graph
    except Exception as exc:                     # best-effort: never crash the build
        logger.warning("clean service/config classify skipped: %s", exc)
        return graph


def make_construction_classifier(client=None, model: str = "", arch: dict | None = None):
    """Return classify(graph, repo_path) -> graph: the deterministic construction-time
    Service+Config classifier (Inc 5 flip; replaces the deleted LLM env_classifier).
    Only translate_service (exotic images) calls the LLM; known kinds are LLM-free."""
    def classify(graph, repo_path: str):
        return classify_services_clean(graph, repo_path, client=client, model=model, arch=arch)
    return classify
