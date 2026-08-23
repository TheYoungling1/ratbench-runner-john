"""Translate router — finish the service tier's clean replacement (Inc 2).

Given a :class:`ProvisioningSpec`, produce a pinned local setup:

* **Known kind** (redis/postgres/mysql/…): the deterministic, LLM-free recipe
  from :func:`service_recipes.render_setup`. Its probe is already normalized.
* **Exotic image** (no recipe kind): a bounded LLM translation
  (:func:`full_translate`) → arch/env sanitize (``apply_arch``/``apply_env``) →
  cheap static URL verify (``verify_plan``) → probe normalized through the
  admissibility firewall (``normalize_probe``).

The return contract of :func:`translate_service` is consumed by
``classify_services_clean`` (the construction-time default since the Inc 5 flip) and
must stay exactly as documented below.

``full_translate`` is a thin, parameterized port of the validated scratchpad PoC
(``poc_translate_diverse.py``: ``_FULL_SYS``/``full_translate``) with two changes:
the model + target arch are parameters (not module globals), and it does NOT
sanitize its output — the raw parsed object is returned and the router owns
``apply_arch``/``apply_env``.
"""

from __future__ import annotations

import json

from python_deps.depgraph.service_recipes import normalize_probe, render_setup
from python_deps.depgraph.translate_sanitize import apply_arch, apply_env
from src.envstate.jsonutil import extract_json_object
from src.envstate.llm_response import complete_with_retry
from src.envstate.provision_verify import verify_plan

# Ported VERBATIM from the PoC (poc_translate_diverse.py:_FULL_SYS).
_FULL_SYS = (
    "You translate ONE docker-compose service into a plan to run it as a LOCAL DAEMON on a "
    "single Debian/Ubuntu image (NO Docker, NO docker-compose). Output ONLY JSON: "
    "{\"install\":[shell steps to install it], \"start\":\"shell to launch the daemon (backgrounded)\", "
    "\"probe\":\"a read-only liveness check\", \"post\":[optional setup steps e.g. create bucket/topic], "
    "\"feasible\":true|false, \"note\":\"one line\"}. "
    "Prefer an official apt package; if none exists, download the official static binary from the "
    "vendor. Set feasible=false ONLY if it genuinely cannot run as a single local daemon (needs an "
    "orchestrated multi-node cluster, or is distributed solely as a container image with no binary). "
    "Reuse the given probe if present. Never use docker/docker-compose. Be concrete and runnable. "
    "In shell strings prefer SINGLE quotes; avoid embedded double quotes so the JSON stays valid. "
    "ARCHITECTURE: NEVER hardcode a CPU arch in a download URL. Use the literal token {ARCH_DPKG} where "
    "the project names assets amd64/arm64, or {ARCH_UNAME} where it uses x86_64/aarch64 — the host fills them. "
    "ENVIRONMENT: the target is a minimal debian:bookworm container running as ROOT, with NO systemd/init "
    "system, and non-interactive apt. Never prefix commands with sudo — sudo is not installed and you are "
    "already root. Never start daemons with systemctl or service; instead launch them directly in the "
    "background, e.g. 'nohup <daemon> ... &' or the daemon's own --daemonize/-d flag. Prefix apt-get installs "
    "with DEBIAN_FRONTEND=noninteractive whenever the package might prompt. When adding a third-party apt "
    "repository's signing key, use 'gpg --dearmor -o /usr/share/keyrings/<name>.gpg' plus a matching "
    "signed-by= entry in the repo's sources.list line — never the deprecated apt-key. "
    "CRITICAL: respond with ONLY the JSON object — NO reasoning, NO prose, NO markdown fences. "
    "Your entire response must start with { and end with }.")

_RETRY_NUDGE = "Respond with ONLY the JSON object, starting with '{'. No reasoning, no prose."


def full_translate(client, model: str, spec, arch: dict) -> dict:
    """Bounded LLM translation of one exotic compose service into a local-daemon plan.

    Returns the RAW parsed plan dict (``install``/``start``/``probe``/``post``/
    ``feasible``/``note``) exactly as the model emitted it — sanitizing is the
    router's job (``apply_arch``/``apply_env`` in :func:`translate_service`), not
    this function's. On a parse failure returns the sentinel
    ``{"feasible": None, "note": "parse-failed"}``.

    :param client: OpenAI-compatible chat client (never called in the None-plan path).
    :param model: Model id forwarded to ``complete_with_retry``.
    :param spec: The :class:`ProvisioningSpec` for the exotic service.
    :param arch: ``{"dpkg": ..., "uname": ...}`` target arch, surfaced to the model
        as ``target_arch`` (advisory; the router still fills ``{ARCH_*}`` tokens).
    """
    base_probe = spec.probe or ""
    user = {"image": spec.image, "kind_hint": spec.kind, "port": spec.port,
            "env_params": spec.params, "healthcheck_probe": base_probe, "target_arch": arch}
    msgs = [{"role": "system", "content": _FULL_SYS},
            {"role": "user", "content": json.dumps(user)}]
    accept = lambda t: extract_json_object(t) is not None  # noqa: E731 (retry if not JSON)
    kw = dict(temperature=0, max_tokens=1500, accept=accept, retry_nudge=_RETRY_NUDGE)
    try:
        text, _usage, _resp = complete_with_retry(
            client, model, msgs, response_format={"type": "json_object"}, **kw)
    except Exception:  # response_format unsupported -> retry as a plain call
        text, _usage, _resp = complete_with_retry(client, model, msgs, **kw)
    obj = extract_json_object(text)
    if obj is None:
        return {"feasible": None, "note": "parse-failed"}
    return obj


def _image_basename(image: str | None) -> str:
    """Bare image name for an exotic kind label: ``qdrant/qdrant:v1.9.0`` -> ``qdrant``.

    Take the last path segment FIRST, then strip the tag — so a registry with a
    port (``localhost:5000/foo:v1``) yields ``foo``, not ``localhost``.
    """
    if not image:
        return ""
    return image.split("/")[-1].split(":")[0]


def translate_service(client, model: str, spec, arch: dict, repo=None) -> dict:
    """Route a :class:`ProvisioningSpec` to a pinned local setup (Inc 2 integration).

    Return contract (consumed by Inc 5 — keep the keys/shape exact)::

        {
          "service_name": spec.service_name,
          "kind": spec.kind or _image_basename(spec.image),
          "route": "known" | "exotic",
          "feasible": bool,
          "setup": {"install": [...], "start": str, "probe": str,
                    "createdb": str | None, "post": [...]} | None,
          "verify": <verify_plan dict> | None,
          "note": str,
        }

    Known kind → deterministic recipe (``verify=None``). Exotic → bounded LLM,
    sanitized, statically verified, with the probe forced through the read-only
    firewall so an admitted probe can never mutate the host. A setup dict is still
    returned when the exotic plan is infeasible (the node is created and demotes at
    certify); only a parse failure yields ``setup=None``.
    """
    # Known kind: LLM-free deterministic recipe.
    setup = render_setup(spec.kind, spec.params)
    if setup is not None:
        return {
            "service_name": spec.service_name,
            "kind": spec.kind,
            "route": "known",
            "feasible": True,
            "setup": setup,
            "verify": None,
            "note": "known-kind",
        }

    # Exotic: bounded LLM translation.
    kind = spec.kind or _image_basename(spec.image)
    plan = full_translate(client, model, spec, arch)
    if plan.get("feasible") is None:  # parse-failed sentinel — no verify, no setup.
        return {
            "service_name": spec.service_name,
            "kind": kind,
            "route": "exotic",
            "feasible": False,
            "setup": None,
            "verify": None,
            "note": "parse-failed",
        }

    plan = apply_env(apply_arch(plan, arch))
    verdict = verify_plan(plan)  # plan is a dict here — guarded past parse-failed above.
    # Probe firewall: a non-read-only probe (e.g. curl) becomes 'nc -z 127.0.0.1 <port>'
    # or '' (kind omitted -> exotic, never a recipe probe).
    probe = normalize_probe(plan.get("probe"), spec.port)
    setup = {
        "install": list(plan.get("install") or []),
        "start": plan.get("start") or "",
        "probe": probe,
        "createdb": None,
        "post": list(plan.get("post") or []),
    }
    feasible = bool(plan.get("feasible")) and verdict["all_ok"]
    return {
        "service_name": spec.service_name,
        "kind": kind,
        "route": "exotic",
        "feasible": feasible,
        "setup": setup,
        "verify": verdict,
        "note": "ok" if feasible else "could-not-provision",
    }
