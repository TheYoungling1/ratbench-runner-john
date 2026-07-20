#!/usr/bin/env python3
"""Force temperature=0.0 at every RAT-agent LLM call site, for reproducibility.

The RAT baseline (``run_rat_benchmark.py --model rat``) runs the official RunAnyThing agent,
whose code lives under ``<RAT_ROOT>/libkit`` (CodeAgent / SetupAgent + tools). Its core LLM
client (``libkit/llm.py``, ``libkit/tools/llm.py``) already defaults to ``temperature=0.0``,
but ~10 auxiliary tool call sites pass ``temperature=0.1``/``0.3`` and
``libkit/analysis/llm_analysis.py`` sets the module-global ``temperature = None`` (-> the
provider's default, often 1.0). This rewrites every non-zero / None temperature literal under
``libkit/`` to ``0.0`` so the RAT baseline is fully greedy-decoded.

Idempotent; re-run after any re-provision of the RAT tree (the libkit files are real, not
symlinked, so the edit is VM-local — this script is the reproducible record of it).

Scope: ``libkit/`` only (the RAT agent). Does NOT touch ``eval/models/*_model.py`` (the
zeroshot / other baselines own their own temperature). Greedy decoding (temperature 0) reduces
sampling variance but does NOT make the run deterministic — serving-stack FP/batching/MoE
nondeterminism and agentic trajectory chaos dominate; pin commit SHAs + report K-run
distributions for real reproducibility.

Usage:
  zero_rat_temperature.py [RAT_ROOT]      # default: $RAT_ROOT or /opt/runanything/src
"""
import os
import re
import sys

RAT_ROOT = (sys.argv[1] if len(sys.argv) > 1
            else os.environ.get("RAT_ROOT", "/opt/runanything/src"))
LIBKIT = os.path.join(RAT_ROOT, "libkit")

# Match `temperature = <number|None|temperature>`; the value is adjudicated in _repl so we
# never mis-handle the leading 0 of 0.1 (a value-anchored check, not a fragile lookahead).
PAT = re.compile(r'(temperature\s*=\s*)(\d+\.\d+|\d+|None|temperature)')
changed = []


def _repl(relpath):
    def f(m):
        prefix, val = m.group(1), m.group(2)
        if val == "temperature":          # passthrough kwarg (e.g. temperature=temperature)
            return m.group(0)
        try:
            if float(val) == 0.0:         # already greedy — leave as-is (idempotent)
                return m.group(0)
        except ValueError:
            pass                          # "None" -> force 0.0
        changed.append((relpath, m.group(0).strip()))
        return prefix + "0.0"
    return f


def main():
    if not os.path.isdir(LIBKIT):
        sys.exit(f"ERROR: libkit not found at {LIBKIT} (set RAT_ROOT or pass it as argv[1])")
    for dirpath, _, files in os.walk(LIBKIT):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            with open(path) as fh:
                src = fh.read()
            new = PAT.sub(_repl(os.path.relpath(path, RAT_ROOT)), src)
            if new != src:
                with open(path, "w") as fh:
                    fh.write(new)
    print(f"RAT_ROOT={RAT_ROOT}")
    print(f"temperature call sites set to 0.0: {len(changed)}")
    for rel, hit in changed:
        print(f"  {rel}: {hit} -> temperature=0.0")
    if not changed:
        print("  (none changed — already all 0.0; idempotent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
