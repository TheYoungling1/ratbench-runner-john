"""Container-derived minimal answer key (design §5). The key is NOT looked up —
it is derived by ablation (ddmin) against the same gate the eval uses, so every
apt name is proven load-bearing and the set is proven sufficient. Derivation is
offline (docker); the committed answer_keys.json is what the eval reads."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AnswerKey:
    name: str
    version: str
    mode: str
    base_image: str
    required_apt: tuple[str, ...]
    gate_level: str
    superset: tuple[str, ...]
    derived_at: str
    notes: str = ""


def load_answer_keys(path: Path) -> list[AnswerKey]:
    items = json.loads(Path(path).read_text(encoding="utf-8"))
    return [AnswerKey(
        name=d["name"], version=d["version"], mode=d["mode"],
        base_image=d["base_image"], required_apt=tuple(d.get("required_apt", ())),
        gate_level=d.get("gate_level", "deep_import"),
        superset=tuple(d.get("superset", ())),
        derived_at=d["derived_at"], notes=d.get("notes", ""),
    ) for d in items]


def save_answer_keys(path: Path, keys: list[AnswerKey]) -> None:
    Path(path).write_text(
        json.dumps([asdict(k) for k in keys], indent=2) + "\n", encoding="utf-8")


def answer_key_of(keys, name: str, version: str, mode: str) -> AnswerKey | None:
    low = name.lower()
    for k in keys:
        if k.name.lower() == low and k.version == version and k.mode == mode:
            return k
    return None


def minimize(superset, gate_fn) -> list[str]:
    """Delta-minimize ``superset`` to a subset where every element is load-bearing.

    ``gate_fn(frozenset) -> bool`` is green iff the subset builds+passes the gate.
    Sufficiency is checked first: the FULL set must be green, else ValueError
    (the superset is insufficient — design §5 step 2 fails loudly). Then each
    element is dropped in turn; a drop that stays green is removed permanently.
    Result is 1-minimal (removing any remaining element breaks the gate)."""
    keep = list(superset)
    if not gate_fn(frozenset(keep)):
        raise ValueError("insufficient superset: gate not green with the full set")
    for cand in list(superset):
        trial = [x for x in keep if x != cand]
        if gate_fn(frozenset(trial)):
            keep = trial
    return keep


def derive_answer_key(spec, mode: str, superset, *, base_image: str, platform: str | None = None) -> AnswerKey:
    """Offline (docker) derivation: ddmin the superset to the minimal required
    apt set, evaluating EACH subset in a FRESH container. apt state is cumulative,
    so a single shared container would make every candidate-drop look green and
    collapse S* to []. Imported lazily by the CLI --derive path; not unit-tested."""
    from datetime import datetime, timezone

    from python_deps.depgraph.executor import DockerExecutor
    from src.eval.package_installability.gate import run_gate

    def gate_fn(subset: frozenset) -> bool:
        apt = " ".join(sorted(subset))
        try:
            with DockerExecutor(base_image, platform=platform) as ex:
                if ex.run("apt-get update", timeout=600).returncode != 0:
                    return False
                if apt and ex.run(f"apt-get install -y {apt}", timeout=1200).returncode != 0:
                    return False
                nb = f"--no-binary {spec.name} " if mode == "forced_sdist" else ""
                if ex.run(f"pip install {nb}{spec.name}=={spec.version}", timeout=1800).returncode != 0:
                    return False
                res = run_gate(ex, spec, timeout=600)
                return res.deep_ok and (res.tail_ok is not False)
        except Exception:
            return False

    required = minimize(list(superset), gate_fn)
    return AnswerKey(
        name=spec.name, version=spec.version, mode=mode, base_image=base_image,
        required_apt=tuple(sorted(required)), gate_level="deep_import+tail",
        superset=tuple(sorted(superset)),
        derived_at=datetime.now(timezone.utc).date().isoformat(),
        notes="ddmin-derived (fresh container per trial)",
    )
