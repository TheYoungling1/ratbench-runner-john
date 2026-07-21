from __future__ import annotations

from dataclasses import dataclass

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # uv pip install tomli


@dataclass(frozen=True)
class VarietySpec:
    name: str
    model: str
    branch: str | None   # None => baseline (rat/repo2run), no agent checkout
    venv: str | None
    is_baseline: bool
    llm: str | None = None   # per-variety LLM slug forwarded to the runner; None => runner default
    measure: str | None = None   # "conforming" | "rehome" | "none"; None => harvest (back-compat)


def load_registry(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_variety(registry: dict, name: str) -> VarietySpec:
    varieties = registry.get("variety", {})
    if name not in varieties:
        raise KeyError(f"unknown variety {name!r}; known: {sorted(varieties)}")
    spec = varieties[name]
    branch = spec.get("branch")
    return VarietySpec(
        name=name,
        model=spec["model"],
        branch=branch,
        venv=spec.get("venv"),
        is_baseline=branch is None,
        llm=spec.get("llm"),
        measure=spec.get("measure"),
    )
