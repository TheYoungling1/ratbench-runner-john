"""Fix 2 — derive exactly one project install for the repo's own package.

The pinned closure (``build_pin_instructions``) installs the full dependency set and
excludes the project's own package by name. The project itself therefore needs ONE
explicit install, derived from achieved state:

  * prefer the editable/local install the agent actually ran (ledger), or
  * fall back to an editable install when the project is present in the captured
    closure and packaging metadata exists.

``--no-deps`` is appended so the install only adds the project package and never
re-resolves (and so never disturbs) the authoritative pinned closure.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import List, Optional, Sequence

from src.envstate.ledger import ActionLedger

_INSTALLER_HEADS = (
    ("pip", "install"),
    ("pip2", "install"),
    ("pip3", "install"),
    ("uv", "pip", "install"),
)
_PACKAGING_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower().replace("_", "-")


_INSTALLER_NAMES = {"pip", "setuptools", "wheel"}


def name_in_closure(installed: Sequence, project_name: Optional[str]) -> bool:
    """True if the project's own package is in the captured pip closure."""
    proj = _norm(project_name)
    if not proj:
        return False
    for fact in installed or ():
        if _norm(getattr(fact, "name", "")) == proj:
            return True
    return False


def closure_has_pins(installed: Sequence, project_name: Optional[str]) -> bool:
    """True if the pinned closure will emit at least one DEPENDENCY pin.

    Mirrors ``build_pin_instructions``: excludes the project's own package (it carries
    no ``==`` and is installed separately) and the installer trio. When this is False
    the closure is empty/partial, so ``--no-deps`` on the project install would leave
    the project with ZERO dependencies — guaranteed ModuleNotFoundError (MEDIUM 1)."""
    proj = _norm(project_name)
    for fact in installed or ():
        name = getattr(fact, "name", "") or ""
        ver = getattr(fact, "detail", "") or ""
        if not name or not ver:
            continue
        norm = _norm(name)
        if norm in _INSTALLER_NAMES or (proj and norm == proj):
            continue
        return True
    return False


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        return tok[1:-1]
    return tok


def _is_local_target(tok: str) -> bool:
    base = _unquote(tok).split("[", 1)[0]
    if not base or "://" in base or base.startswith("git+"):
        return False
    if base in (".", "./") or base.startswith(("./", "../", "/")):
        return True
    # Monorepo sub-dir install (LOW): a bare relative path with a separator, e.g.
    # `pip install -e packages/foo`. A PyPI requirement never contains "/" (a bare
    # name without a separator stays ambiguous and is treated as PyPI, not local).
    return "/" in base


def _norm_target(tok: str) -> str:
    base = _unquote(tok).split("[", 1)[0].rstrip("/")
    if base in ("", ".", "/app") or base.startswith("/"):
        return "."
    return base


def _install_args(tokens: List[str]) -> Optional[List[str]]:
    if tokens and tokens[0] == "sudo":
        tokens = tokens[1:]
    if len(tokens) >= 4 and tokens[0] in ("python", "python2", "python3") \
            and tokens[1] == "-m" and tokens[2] == "pip" and tokens[3] == "install":
        return tokens[4:]
    for head in _INSTALLER_HEADS:
        if tuple(tokens[: len(head)]) == head:
            return tokens[len(head):]
    return None


def _detect_local_install(cmd: str) -> Optional[str]:
    """Canonical project install command (``pip install -e .`` / ``pip install .`` /
    ``pip install -e <subdir>``) for a single command, else None."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    args = _install_args(tokens)
    if args is None:
        return None

    # Editable local install: -e/--editable followed by a local path.
    for i, tok in enumerate(args):
        if tok in ("-e", "--editable") and i + 1 < len(args) and _is_local_target(args[i + 1]):
            return f"pip install -e {_norm_target(args[i + 1])}"

    # Non-editable local install: a bare local path argument.
    skip_next = False
    for tok in args:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-r", "--requirement", "-c", "--constraint"):
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if _is_local_target(tok):
            return f"pip install {_norm_target(tok)}"
    return None


def project_install_from_ledger(ledger: Optional[ActionLedger]) -> Optional[str]:
    """The LAST successful local/editable project install in the ledger, canonicalized."""
    if ledger is None:
        return None
    found: Optional[str] = None
    for event in ledger.events():
        if event.rc != 0:
            continue
        detected = _detect_local_install(event.cmd or "")
        if detected:
            found = detected
    return found


def _closure_project_pin(installed: Sequence, project_name: Optional[str]) -> Optional[str]:
    """`<name>==<version>` for the project from the captured closure (host pip-freeze), or None."""
    proj = _norm(project_name)
    if not proj:
        return None
    for fact in installed or ():
        if _norm(getattr(fact, "name", "")) == proj:
            ver = (getattr(fact, "detail", "") or "").strip()
            name = getattr(fact, "name", "") or project_name
            return f"{name}=={ver}" if ver else None
    return None


def project_installed_by_name(ledger: Optional[ActionLedger], project_name: Optional[str]) -> bool:
    """True iff the agent SUCCESSFULLY (rc==0) installed the project BY NAME from an index
    (e.g. `pip install pyads` / `pip install pyads==3.6.0`), as opposed to a local/editable
    install. Anchored on the project name so an UNRELATED successful install never matches."""
    if ledger is None:
        return False
    proj = _norm(project_name)
    if not proj:
        return False
    for event in ledger.events():
        if event.rc != 0:
            continue
        try:
            tokens = shlex.split(event.cmd or "")
        except ValueError:
            continue
        args = _install_args(tokens)
        if args is None:
            continue
        skip_next = False
        for tok in args:
            if skip_next:
                skip_next = False
                continue
            if tok in ("-r", "--requirement", "-c", "--constraint"):
                skip_next = True
                continue
            if tok.startswith("-"):
                continue
            if _is_local_target(tok):
                continue
            base = re.split(r"[<>=!~\[ ]", _unquote(tok), maxsplit=1)[0]
            if _norm(base) == proj:
                return True
    return False


def _has_packaging_metadata(project_root: Optional[str]) -> bool:
    if not project_root or not os.path.isdir(project_root):
        return False
    return any(os.path.isfile(os.path.join(project_root, f)) for f in _PACKAGING_FILES)


def _ensure_no_deps(cmd: str) -> str:
    return cmd if "--no-deps" in cmd.split() else f"{cmd} --no-deps"


def resolve_project_install(
    installed: Sequence,
    project_name: Optional[str],
    *,
    ledger: Optional[ActionLedger] = None,
    project_root: Optional[str] = None,
) -> Optional[str]:
    """Return the single project-install command or None.

    Emits an install only when the project was actually installed in-sandbox — either
    the agent ran a local/editable install (ledger), or the project package is present
    in the captured closure. Never fabricates an install for a package that was never
    set up.

    ``--no-deps`` is appended ONLY when the pinned closure carries at least one
    dependency (``closure_has_pins``); on an empty/partial closure we allow normal
    resolution so the project does not end up with zero deps (MEDIUM 1).

    MEDIUM 3 (follow-up, not fixed here): a poetry/flit/hatch project with no ledger
    install is re-emitted as ``pip install -e .``. That works for any PEP 517 backend,
    but a manager-specific install (e.g. ``poetry install``) is not yet derived.
    """
    add_no_deps = closure_has_pins(installed, project_name)

    def _finalize(cmd: str) -> str:
        return _ensure_no_deps(cmd) if add_no_deps else cmd

    ledger_cmd = project_install_from_ledger(ledger)
    if ledger_cmd:
        return _finalize(ledger_cmd)
    # Prefer a host-verified SUCCESSFUL by-name install of the project over fabricating an
    # editable install (the editable may have FAILED in-sandbox — e.g. pyads' meson build).
    # Fires only when the project is in the closure (so we have its pinned version) AND a
    # successful by-name install of THIS project is recorded (rc==0).
    if (
        name_in_closure(installed, project_name)
        and project_installed_by_name(ledger, project_name)
    ):
        pin = _closure_project_pin(installed, project_name)
        if pin:
            return _finalize(f"pip install {pin}")
    if name_in_closure(installed, project_name) and _has_packaging_metadata(project_root):
        return _finalize("pip install -e .")
    if name_in_closure(installed, project_name) and project_root is None:
        # Closure proves the project was installed but we cannot inspect files
        # (e.g. unit test path) — default to editable.
        return _finalize("pip install -e .")
    return None
