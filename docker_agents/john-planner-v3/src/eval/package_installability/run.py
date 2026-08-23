"""Phase-1 loop (design §3): predict our apt set, install it + the package in a
fresh container, run the gate, assemble an InstallRecord. A fresh DockerExecutor
per row guarantees clean-container isolation. `evaluate` never raises — any
exception becomes status 'error'; `run_corpus` turns a container-start or
apt-update failure into an 'error' row too, so one bad row never aborts the run."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from python_deps.depgraph.executor import DockerExecutor

from src.eval.package_installability.answer_key import answer_key_of
from src.eval.package_installability.gate import run_gate
from src.eval.package_installability.predict import predict_apt_deps
from src.eval.package_installability.score import InstallRecord, record_from_dict

logger = logging.getLogger(__name__)


def observed_branch(pip_stdout: str) -> str:
    """Classify what the resolver actually did from pip's output (natural mode)."""
    if "Building wheel for" in pip_stdout or "Running setup.py" in pip_stdout:
        return "sdist"
    if ".whl" in pip_stdout:
        return "wheel"
    return "unknown"


def _record(spec, mode, *, predicted_apt=(), answer_apt=(), install_rc, gate_ok,
            tail_gate_ok, status, branch_pred, branch_real, apt_rc=None, pip_rc=None) -> InstallRecord:
    return InstallRecord(
        name=spec.name, version=spec.version, mode=mode, stratum=spec.stratum,
        predicted_apt=predicted_apt, answer_apt=answer_apt, install_rc=install_rc,
        gate_ok=gate_ok, tail_gate_ok=tail_gate_ok, status=status,
        branch_pred=branch_pred, branch_real=branch_real, apt_rc=apt_rc, pip_rc=pip_rc)


def _error_record(spec, mode, answer_apt=()) -> InstallRecord:
    return _record(spec, mode, answer_apt=answer_apt, install_rc=-1, gate_ok=False,
                   tail_gate_ok=None, status="error", branch_pred="unknown",
                   branch_real=None)


def evaluate(spec, mode: str, executor, keys) -> InstallRecord:
    """Run one (spec, mode) row against a live executor and return its record.
    Never raises — any failure yields status='error'."""
    answer_apt: tuple[str, ...] = ()
    try:
        key = answer_key_of(keys, spec.name, spec.version, mode)
        answer_apt = key.required_apt if key else ()

        pred = predict_apt_deps(spec.name, spec.version, mode, executor)
        apt = tuple(sorted(pred.apt))
        apt_rc: int | None = None
        if apt:
            r = executor.run(f"apt-get install -y {' '.join(apt)}", timeout=1200)
            apt_rc = r.returncode
            if r.returncode != 0:
                return _record(spec, mode, predicted_apt=apt, answer_apt=answer_apt,
                               install_rc=r.returncode, gate_ok=False, tail_gate_ok=None,
                               status="fail", branch_pred=pred.branch, branch_real=None,
                               apt_rc=apt_rc, pip_rc=None)

        nb = f"--no-binary {spec.name} " if mode == "forced_sdist" else ""
        pip = executor.run(f"pip install {nb}{spec.name}=={spec.version}", timeout=1800)
        branch_real = observed_branch(pip.stdout) if mode == "natural" else None
        if pip.returncode != 0:
            return _record(spec, mode, predicted_apt=apt, answer_apt=answer_apt,
                           install_rc=pip.returncode, gate_ok=False, tail_gate_ok=None,
                           status="fail", branch_pred=pred.branch, branch_real=branch_real,
                           apt_rc=apt_rc, pip_rc=pip.returncode)

        gate = run_gate(executor, spec, timeout=600)
        gate_ok = gate.deep_ok and (gate.tail_ok is not False)
        return _record(spec, mode, predicted_apt=apt, answer_apt=answer_apt, install_rc=0,
                       gate_ok=gate_ok, tail_gate_ok=gate.tail_ok,
                       status="pass" if gate_ok else "fail",
                       branch_pred=pred.branch, branch_real=branch_real,
                       apt_rc=apt_rc, pip_rc=pip.returncode)
    except Exception:  # noqa: BLE001 — infra failure is an error bucket, not a crash
        logger.exception("evaluate(%s, %s) errored", spec.name, mode)
        return _error_record(spec, mode, answer_apt)


def _load_checkpoint(cp) -> tuple[list[InstallRecord], set]:
    """Read completed rows from a JSONL checkpoint (empty if absent) → (records, done-keys)."""
    records: list[InstallRecord] = []
    done: set = set()
    if cp is not None and cp.exists():
        for line in cp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = record_from_dict(json.loads(line))
            records.append(rec)
            done.add((rec.name.lower(), rec.version, rec.mode))
    return records, done


def _run_one(spec, mode: str, image: str, keys, platform: str | None = None) -> InstallRecord:
    """One (spec, mode) row in a fresh container; never raises."""
    try:
        with DockerExecutor(image, platform=platform) as ex:
            upd = ex.run("apt-get update", timeout=600)
            if upd.returncode != 0:
                logger.warning("apt-get update failed for %s/%s -> error row", spec.name, mode)
                return _error_record(spec, mode)
            return evaluate(spec, mode, ex, keys)
    except Exception:  # noqa: BLE001 — container failure is an error row
        logger.exception("container failed for %s/%s -> error row", spec.name, mode)
        return _error_record(spec, mode)


def run_corpus(specs, *, image: str, keys, modes=None, checkpoint=None, platform: str | None = None) -> list[InstallRecord]:
    """Evaluate each (spec, mode) in a FRESH container. If ``checkpoint`` (a JSONL
    path) is given, each completed row is appended immediately (crash-resilient)
    and any row already present is skipped (resume). Requires docker."""
    cp = Path(checkpoint) if checkpoint else None
    records, done = _load_checkpoint(cp)
    for spec in specs:
        for mode in (modes or spec.modes):
            if mode not in spec.modes:
                continue
            if (spec.name.lower(), spec.version, mode) in done:
                continue
            rec = _run_one(spec, mode, image, keys, platform)
            records.append(rec)
            if cp is not None:
                with cp.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(rec)) + "\n")
    return records
