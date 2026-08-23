"""Arch/env sanitizers for an LLM-produced provisioning plan (clean tier Inc1).

Pure, deterministic post-processors over a plan ``dict`` (the JSON the LLM emits
for a compose-service translation: ``install``/``post`` lists + ``start``/``probe``
strings). Ported from the validated scratchpad PoC (`poc_translate_diverse.py`,
``_apply_arch``/``_apply_env``) with two deliberate changes:

  1. ``arch`` is a plain ``dict`` PARAMETER (``{"dpkg": ..., "uname": ...}``), not a
     module-global filled by a ``docker``/``subprocess`` probe. This module is
     ``python_deps``-pure: stdlib only, no Docker, no network, no arch detection.
  2. Neither function mutates its input. Each returns a NEW plan dict with fresh
     ``install``/``post`` lists and ``start``/``probe`` strings; the caller's dict
     and its nested containers are left untouched (repo immutability rule).

Nothing imports this module yet; a later increment (CR5) will consume it.
"""

from __future__ import annotations

import re as _re

# The keys whose values carry shell strings to sanitize.
_LIST_KEYS = ("install", "post")
_STR_KEYS = ("start", "probe")

# Word-boundary matches for a bare CPU arch token (so ``amd64`` in
# ``app-linux-amd64.tar.gz`` is rewritten but not ``foo-amd64bar``).
_AMD64_RE = _re.compile(r"(?<![A-Za-z0-9])amd64(?![A-Za-z0-9])")
_X86_64_RE = _re.compile(r"(?<![A-Za-z0-9])x86_64(?![A-Za-z0-9])")


def _map_plan(plan: dict, fix) -> dict:
    """Return a new plan with ``fix`` applied to each shell string, input untouched.

    ``install``/``post`` list values yield fresh lists; ``start``/``probe`` string
    values are replaced with their fixed form. Every other key is copied verbatim.
    """
    out = dict(plan)
    for key in _LIST_KEYS:
        value = plan.get(key)
        if isinstance(value, list):
            out[key] = [fix(item) for item in value]
    for key in _STR_KEYS:
        value = plan.get(key)
        if value:
            out[key] = fix(value)
    return out


def apply_arch(plan: dict, arch: dict) -> dict:
    """Fill the target arch into a plan: substitute ``{ARCH_*}`` tokens, and (safety
    net) rewrite any hardcoded ``amd64``/``x86_64`` left in a download URL.

    ``arch`` is ``{"dpkg": <apt arch e.g. arm64>, "uname": <machine e.g. aarch64>}``.
    The URL safety-net only fires for a non-amd64 target and only inside strings that
    look like a URL (contain ``http`` or ``://``).
    """
    dpkg = arch["dpkg"]
    uname = arch["uname"]

    def fix(s):
        if not isinstance(s, str):
            return s
        s = s.replace("{ARCH_DPKG}", dpkg).replace("{ARCH_UNAME}", uname)
        if dpkg != "amd64" and ("http" in s or "://" in s):
            s = _AMD64_RE.sub(dpkg, s)
            s = _X86_64_RE.sub(uname, s)
        return s

    return _map_plan(plan, fix)


def apply_env(plan: dict) -> dict:
    """Enforce the root-container reality: strip a leading ``sudo `` and any chained
    ``(&& |; )sudo `` from every command string (root container: ``sudo`` is often
    absent, so ``sudo X`` fails).
    """

    def fix(s):
        if not isinstance(s, str):
            return s
        s = _re.sub(r"^sudo ", "", s)
        s = _re.sub(r"(&& |; )sudo ", r"\1", s)
        return s

    return _map_plan(plan, fix)
