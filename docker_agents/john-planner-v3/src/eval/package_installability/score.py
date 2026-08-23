"""Pure scorer (design §7) — no docker/network. Headline = installability;
fidelity/branch/tail are diagnostics reported beside it."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class InstallRecord:
    name: str
    version: str
    mode: str
    stratum: str
    predicted_apt: tuple[str, ...]
    answer_apt: tuple[str, ...]
    install_rc: int
    gate_ok: bool
    tail_gate_ok: bool | None
    status: str            # "pass" | "fail" | "error"
    branch_pred: str
    branch_real: str | None
    apt_rc: int | None = None
    pip_rc: int | None = None


@dataclass(frozen=True)
class InstallabilityMetric:
    n_rows: int
    installable_rate: float
    by_mode: dict = field(default_factory=dict)
    by_stratum: dict = field(default_factory=dict)
    fidelity: dict = field(default_factory=dict)
    harmful_overpred: tuple[str, ...] = ()
    branch_accuracy: dict = field(default_factory=dict)
    failure_phase: dict = field(default_factory=dict)
    tail_catches: tuple[str, ...] = ()


def _as_apt_tuple(value, field: str) -> tuple[str, ...]:
    """Coerce a JSON apt-name field to a tuple, rejecting a bare string (which
    tuple() would silently explode into per-character entries) and other
    non-sequence types. None -> ()."""
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{field} must be a list of apt names, got {type(value).__name__}: {value!r}")
    return tuple(value)


def record_from_dict(d: dict) -> InstallRecord:
    return InstallRecord(
        name=d["name"], version=d["version"], mode=d["mode"], stratum=d["stratum"],
        predicted_apt=_as_apt_tuple(d.get("predicted_apt"), "predicted_apt"),
        answer_apt=_as_apt_tuple(d.get("answer_apt"), "answer_apt"),
        install_rc=int(d.get("install_rc", 1)),
        gate_ok=bool(d.get("gate_ok", False)),
        tail_gate_ok=d.get("tail_gate_ok"),
        status=d.get("status", "error"),
        branch_pred=d.get("branch_pred", "unknown"),
        branch_real=d.get("branch_real"),
        apt_rc=d.get("apt_rc"),
        pip_rc=d.get("pip_rc"),
    )


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def _installable_rate(rows: Sequence[InstallRecord]) -> float:
    scored = [r for r in rows if r.status != "error"]
    return _rate(sum(1 for r in scored if r.status == "pass"), len(scored))


def _failure_phase(r: InstallRecord) -> str | None:
    if r.status != "fail":
        return None
    if r.apt_rc not in (None, 0):
        return "apt"
    if r.pip_rc not in (None, 0):
        return "pip"
    return "gate"


def score_records(records: Sequence[InstallRecord]) -> InstallabilityMetric:
    rows = list(records)

    by_mode = {}
    for mode in ("natural", "forced_sdist"):
        sub = [r for r in rows if r.mode == mode]
        by_mode[mode] = {"n": len(sub), "installable_rate": _installable_rate(sub)}
    by_stratum = {}
    for stratum in sorted({r.stratum for r in rows}):
        sub = [r for r in rows if r.stratum == stratum]
        by_stratum[stratum] = {"n": len(sub), "installable_rate": _installable_rate(sub)}

    # Fidelity over rows with a non-empty answer key.
    labelled = [r for r in rows if r.answer_apt]
    recalls, precisions = [], []
    for r in labelled:
        p, s = set(r.predicted_apt), set(r.answer_apt)
        recalls.append(_rate(len(p & s), len(s)))
        precisions.append(_rate(len(p & s), len(p)))
    fidelity = {
        "n_scored": len(labelled),
        "recall": round(sum(recalls) / len(recalls), 4) if recalls else 0.0,
        "precision": round(sum(precisions) / len(precisions), 4) if precisions else 0.0,
    }

    # Harmful over-prediction: a FAILED row whose prediction carries an extra
    # not in the answer key (the pycurl-gnutls class). Harmless extras on a
    # PASSING row are free wheels — not counted.
    harmful = tuple(
        r.name for r in rows
        if r.status == "fail" and (set(r.predicted_apt) - set(r.answer_apt))
    )

    natural = [r for r in rows if r.mode == "natural" and r.branch_real is not None]
    scored = [r for r in natural if r.branch_pred != "unknown" and r.branch_real != "unknown"]
    branch_accuracy = {
        "n": len(scored),
        "rate": _rate(sum(1 for r in scored if r.branch_pred == r.branch_real), len(scored)),
        "n_unknown": len(natural) - len(scored),
    }

    tail_catches = tuple(
        r.name for r in rows
        if r.stratum == "S4" and r.tail_gate_ok is False
    )

    failure_phase = {"apt": 0, "pip": 0, "gate": 0}
    for r in rows:
        ph = _failure_phase(r)
        if ph:
            failure_phase[ph] += 1

    return InstallabilityMetric(
        n_rows=len(rows),
        installable_rate=_installable_rate(rows),
        by_mode=by_mode,
        by_stratum=by_stratum,
        fidelity=fidelity,
        harmful_overpred=harmful,
        branch_accuracy=branch_accuracy,
        failure_phase=failure_phase,
        tail_catches=tail_catches,
    )
