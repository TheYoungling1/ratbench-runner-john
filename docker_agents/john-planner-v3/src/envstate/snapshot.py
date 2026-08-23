# src/envstate/snapshot.py
"""Read-only env probe -> EnvSnapshot(installed, env). Never raises.

Wraps extractor.run_extractor via sandbox.exec_readonly (no ledger / no
Dockerfile leak). env is empty ONLY on total probe failure (arch reads on
any healthy container), which apply_deterministic uses as the degrade signal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from src.envstate.extractor import run_extractor, LIGHTWEIGHT_FIELDS
from src.envstate.world_model import Fact

_SNAPSHOT_FIELDS = LIGHTWEIGHT_FIELDS + (
    "which_python", "venv", "dpkg_packages", "pkg_config_modules", "system_tools", "os_release",
    "dep_tree",
)


@dataclass(frozen=True)
class EnvSnapshot:
    installed: tuple[Fact, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    system_installed: tuple[Fact, ...] = ()
    import_results: tuple[tuple[str, bool], ...] = ()   # (import_name, ok); set by import sweep, not extractor
    dep_tree: str = ""                                   # raw output of `python -m pip inspect`


def _parse_installed(freeze_text: str) -> tuple[Fact, ...]:
    facts: list[Fact] = []
    for raw in freeze_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if "==" in line:
            name, _, ver = line.partition("==")
            name = name.strip()
            if name:
                facts.append(Fact(name=name, detail=ver.strip()))
    return tuple(facts)


def _names(text: str, *, first_token: bool) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        out.append(line.split()[0] if first_token else line)
    return out


def probe_env(exec_readonly: Callable[[str], tuple[int, str]]) -> EnvSnapshot:
    try:
        result = run_extractor(exec_readonly, _SNAPSHOT_FIELDS)
    except Exception:
        return EnvSnapshot()
    fields = result.fields
    installed = _parse_installed(fields.get("installed_pip", ""))

    # system providers: apt names + pkg-config module names + tools on PATH
    sys_facts: list[Fact] = []
    for name in _names(fields.get("dpkg_packages", ""), first_token=True):
        sys_facts.append(Fact(name=name, detail="dpkg"))
    for name in _names(fields.get("pkg_config_modules", ""), first_token=True):
        sys_facts.append(Fact(name=name, detail="pkgconfig"))
    tools = _names(fields.get("system_tools", ""), first_token=True)
    for name in tools:
        sys_facts.append(Fact(name=name, detail="tool"))

    dep_tree = fields.get("dep_tree", "")

    # env: keep ONLY compact, prompt-friendly scalars; drop bulky list fields
    bulky = {"installed_pip", "dpkg_packages", "pkg_config_modules", "system_tools", "dep_tree"}
    env = {k: v for k, v in fields.items() if k not in bulky}
    if tools:
        env["build_tools"] = ",".join(tools)
    return EnvSnapshot(installed=installed, env=env, system_installed=tuple(sys_facts),
                       dep_tree=dep_tree)
