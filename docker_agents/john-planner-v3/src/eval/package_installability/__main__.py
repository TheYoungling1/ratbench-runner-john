"""CLI for the package-installability eval.

    python3 -m src.eval.package_installability --seed
    python3 -m src.eval.package_installability --score records.json
    python3 -m src.eval.package_installability --run  --image python:3.11-slim --out out.json
    python3 -m src.eval.package_installability --derive --image python:3.11-slim   # docker, offline
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

# Dual bootstrap: make both the detector path (python_deps.depgraph.*, under src/)
# and this package (src.eval.*, under repo root) importable when run as
# `python3 -m src.eval.package_installability`. parents[3] = repo root
# (src/eval/package_installability/__main__.py -> depth 3; was parents[2] under evals/).
_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_ROOT / "src"), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_OUT_DIR = _ROOT / "outputs" / "package_installability"  # gitignored artifacts

from src.eval.package_installability.corpus import CORPUS, select_corpus
from src.eval.package_installability.score import (
    record_from_dict, score_records,
)

_KEYS = Path(__file__).resolve().parent / "answer_keys.json"
_SEED = Path(__file__).resolve().parent / "seed_records.json"


def _parse_csv(raw: str) -> frozenset[str]:
    """Split a comma-separated CLI value into a frozenset, dropping blanks."""
    return frozenset(t for t in (x.strip() for x in raw.split(",")) if t)


def summarize(m) -> str:
    lines = [
        "== package-installability (headline = installable_rate; rest DIAGNOSTIC) ==",
        f"  installable_rate : {m.installable_rate}  (n={m.n_rows})",
        f"    by_mode        : {m.by_mode}",
        f"    by_stratum     : {m.by_stratum}",
        f"  fidelity (DIAGNOSTIC)      : {m.fidelity}",
        f"  harmful_overpred           : {list(m.harmful_overpred)}",
        f"  branch_accuracy (natural)  : {m.branch_accuracy}",
        f"  failure_phase (fail rows)  : {m.failure_phase}",
        f"  dlopen_tail_catches        : {list(m.tail_catches)}",
    ]
    return "\n".join(lines)


def _score(path: Path) -> int:
    recs = [record_from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]
    print(summarize(score_records(recs)))
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--seed", action="store_true", help="score the synthetic seed records")
    g.add_argument("--score", type=Path, help="score a records JSON file")
    g.add_argument("--run", action="store_true", help="run the corpus in docker")
    g.add_argument("--derive", action="store_true", help="derive answer keys (docker, offline)")
    g.add_argument("--vet", action="store_true",
                   help="vet corpus candidates in docker (curation; writes results JSON)")
    ap.add_argument("--image", default="python:3.11-slim-bookworm",
                    help="base image for docker paths (v2 pins bookworm for a stable apt namespace)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--checkpoint", type=Path, default=None,
                     help="JSONL checkpoint for --run (crash-resilient + resume)")
    ap.add_argument("--platform", default="linux/amd64",
                    help="docker --platform for run/derive containers (default linux/amd64)")
    ap.add_argument("--only", default="", help="comma-sep package names to run (subset of the corpus)")
    ap.add_argument("--stratum", default="", help="comma-sep strata to run, e.g. S1,S4")
    args = ap.parse_args(argv[1:])

    if args.seed:
        return _score(_SEED)
    if args.score:
        return _score(args.score)

    # docker paths — import lazily so unit tests never need docker.
    from src.eval.package_installability.answer_key import (
        derive_answer_key, load_answer_keys, save_answer_keys,
    )
    from src.eval.package_installability.run import run_corpus

    only, strata = _parse_csv(args.only), _parse_csv(args.stratum)
    keys = load_answer_keys(_KEYS)
    if args.run:
        selected = select_corpus(only, strata)  # raises on unknown --only/--stratum
        print(f"[corpus] {len(selected)} row(s) selected (only={sorted(only)}, strata={sorted(strata)})")
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = args.out or (_OUT_DIR / "records.json")
        checkpoint = args.checkpoint  # opt-in: no shared checkpoint unless the user asks
        records = run_corpus(selected, image=args.image, keys=keys, checkpoint=checkpoint, platform=args.platform)
        payload = [asdict(r) for r in records]
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(summarize(score_records(records)))
        return 0

    if args.vet:
        from src.eval.package_installability.curation import run_vetting
        results = run_vetting(image=args.image, platform=args.platform)
        payload = [asdict(r) for r in results]
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = args.out or (_OUT_DIR / "vetting.json")
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        n_ok = sum(1 for r in results if r.ok)
        print(f"vetting: {n_ok}/{len(results)} (candidate, mode) rows passed")
        for r in results:
            print(f"  {'OK ' if r.ok else 'XX '} {r.name}=={r.version} [{r.mode}] {r.detail}")
        return 0

    if args.derive:  # regenerate answer_keys.json via ablation (human-review the diff)
        if (only or strata) and args.out is None:
            ap.error("--derive with --only/--stratum requires an explicit --out "
                     "to avoid clobbering answer_keys.json")
        selected = select_corpus(only, strata)  # raises on unknown --only/--stratum
        print(f"[corpus] {len(selected)} row(s) selected (only={sorted(only)}, strata={sorted(strata)})")
        derived = []
        for spec in selected:
            for mode in spec.modes:
                superset = [k.superset for k in keys
                            if k.name.lower() == spec.name.lower()
                            and k.version == spec.version and k.mode == mode]
                seed = list(superset[0]) if superset else []
                try:
                    derived.append(derive_answer_key(spec, mode, seed, base_image=args.image, platform=args.platform))
                except Exception as exc:  # noqa: BLE001
                    print(f"[derive] {spec.name} {mode}: {exc}", file=sys.stderr)
        out = args.out or _KEYS
        save_answer_keys(out, derived)
        print(f"wrote {len(derived)} keys -> {out}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
