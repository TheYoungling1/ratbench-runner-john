#!/usr/bin/env python3
"""
Does LLM + PyPI-RECORD grounding solve import->dist WITHOUT the install step?

Real OpenRouter LLM proposes candidate distributions. Each candidate is judged
two independent ways:

  GROUNDING (cheap, no install): does the candidate's own wheel RECORD list the
                                 module?  -> confirm / deny / blind(no wheel)
  INSTALL  (backstop, truth):    install in a throwaway env, does `import X` run?

We then compare the two verdicts per import to locate the residue: where does
grounding-alone disagree with the install backstop, and who is right?

  agree                    grounding == install, no install needed
  GROUNDING-FALSE-POS      RECORD says provides, but import FAILS (system-lib) -> install wins
  INSTALL-FALSE-POS        RECORD denies, but import works transitively (shim)  -> grounding wins
  SDIST                    no wheel to read, import works                       -> install-only

Key: read from env (never printed). Model via OPENROUTER_MODEL.
Reuses the verify primitives from underdeclaration_repair_poc.py.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from underdeclaration_repair_poc import (  # noqa: E402
    ACCEPT, AMBIGUOUS, UNRESOLVED, canon, import_top, install_and_import,
    load_pipreqs_table, normalize_candidates, table_candidates, wheel_provides,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat")

CORPUS = [
    # import,     gold (intended),   note
    ("yaml",     "pyyaml",           "opaque single provider"),
    ("jwt",      "pyjwt",            "opaque single provider"),
    ("serial",   "pyserial",         "opaque single provider"),
    ("slugify",  "python-slugify",   "prefix-opaque"),
    ("bs4",      "beautifulsoup4",   "SHIM trap: bs4 dist is transitive-only"),
    ("Crypto",   "pycryptodome",     "ABANDONED-variant trap: pycrypto"),
    ("attr",     "attrs",            "variant AMBIGUITY: attrs vs attr"),
    ("magic",    "python-magic",     "runtime SYSTEM-LIB: needs libmagic"),
]


def _clean_dist(s: str) -> str:
    s = str(s).strip()
    m = re.match(r"[A-Za-z0-9._-]+", s)
    return m.group(0) if m else ""


def llm_candidates(import_name: str) -> tuple[list[str], str]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set in environment")
    prompt = (
        f"For the Python statement `import {import_top(import_name)}`, which PyPI "
        f"distribution package(s) provide that importable top-level module? Give the "
        f"pip-install names, most-likely first. If more than one distribution provides "
        f"it, list them all. Respond with ONLY a compact JSON array of strings."
    )
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        OPENROUTER_URL, data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://localhost/poc",
            "X-Title": "underdecl-grounding-poc",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = json.loads(r.read().decode())["choices"][0]["message"]["content"]
    except Exception as e:  # noqa: BLE001
        return [], f"llm-error: {e}"
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return [], f"unparseable:{content[:60]!r}"
    try:
        arr = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return [], f"badjson:{content[:60]!r}"
    out, seen = [], set()
    for x in arr:
        d = _clean_dist(x)
        if d and canon(d) not in seen:
            seen.add(canon(d))
            out.append(d)
    return out, "ok"


@dataclass
class Judged:
    dist: str
    source: str
    record: str      # confirm | deny | blind
    import_ok: bool
    detail: str

    @property
    def role(self) -> str:
        if self.record == "confirm" and self.import_ok:
            return "agree+"
        if self.record == "confirm" and not self.import_ok:
            return "GROUNDING-FALSE-POS"
        if self.record == "deny" and self.import_ok:
            return "INSTALL-FALSE-POS"
        if self.record == "deny" and not self.import_ok:
            return "agree-"
        if self.record == "blind" and self.import_ok:
            return "SDIST"
        return "blind-reject"


def judge(dist: str, source: str, import_name: str) -> Judged:
    top = import_top(import_name)
    status, provides = wheel_provides(dist, top)
    if status == "no-dist":
        return Judged(dist, source, "deny", False, "not on PyPI")
    record = {"wheel": "confirm" if provides else "deny",
              "no-wheel": "blind", "too-big": "blind"}[status]
    inst = install_and_import(dist, top)  # backstop ground truth
    return Judged(dist, source, record, inst.imported, inst.detail)


def _verdict(dists: list[str]) -> tuple[str, str]:
    u = sorted(set(dists))
    if len(u) == 1:
        return ACCEPT, u[0]
    if len(u) > 1:
        return AMBIGUOUS, ", ".join(u)
    return UNRESOLVED, "-"


def all_candidates(name: str, table: dict, llm: list[str]) -> list[tuple[str, str]]:
    ordered = ([(d, "normalize") for d in normalize_candidates(name)]
               + [(d, "pipreqs") for d in table_candidates(name, table)]
               + [(d, "llm") for d in llm])
    seen, out = set(), []
    for d, src in ordered:
        if canon(d) not in seen:
            seen.add(canon(d))
            out.append((d, src))
    return out


def main() -> int:
    table, tsrc = load_pipreqs_table()
    print("# LLM + PyPI-RECORD grounding vs install-backstop")
    print(f"# model : {MODEL}")
    print(f"# table : {tsrc} ({len(table)} entries)\n")

    roles_total: dict[str, int] = {}
    llm_top_hits = 0
    grounding_sufficient = 0
    install_load_bearing = 0

    for name, gold, note in CORPUS:
        llm, status = llm_candidates(name)
        print(f"== import '{name}'   ({note})")
        print(f"   LLM[{status}] -> {llm if llm else '(none)'}   gold={gold}")

        judged = [judge(d, src, name) for d, src in all_candidates(name, table, llm)]
        for j in judged:
            print(f"     {j.dist:<20} [{j.source:<9}] record={j.record:<8} "
                  f"import={'OK' if j.import_ok else '--'}  {j.role:<20} {j.detail}")
            roles_total[j.role] = roles_total.get(j.role, 0) + 1

        grounding_ok = [j.dist for j in judged if j.record == "confirm"]
        install_ok = [j.dist for j in judged if j.import_ok]
        gv, gpick = _verdict(grounding_ok)
        iv, ipick = _verdict(install_ok)
        agree = {canon(x) for x in grounding_ok} == {canon(x) for x in install_ok}

        # did LLM's own first pick land a real, importable provider?
        if llm and canon(llm[0]) in {canon(x) for x in install_ok}:
            llm_top_hits += 1
        if agree and gv != UNRESOLVED:
            grounding_sufficient += 1
        if not agree:
            install_load_bearing += 1

        flag = "grounding==install" if agree else ">>> DIVERGENCE"
        print(f"   grounding-only : {gv}: {gpick}")
        print(f"   install-truth  : {iv}: {ipick}    [{flag}]\n")

    print("-" * 68)
    print(f"LLM top-pick was a real importable provider : {llm_top_hits}/{len(CORPUS)}")
    print(f"grounding-alone matched install (no install needed): {grounding_sufficient}/{len(CORPUS)}")
    print(f"install was load-bearing (grounding disagreed)     : {install_load_bearing}/{len(CORPUS)}")
    print("\nrole tally (per candidate):")
    for role, n in sorted(roles_total.items(), key=lambda kv: -kv[1]):
        print(f"   {role:<22} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
