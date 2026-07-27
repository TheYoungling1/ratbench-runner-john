# Error classification in `bench/` — SPEC

**Goal:** add a per-repo *error* layer to `bench/` so an A/B can answer
*does arm B eliminate a specific class of environment error that arm A hits?* — rather than
only reporting that an aggregate score moved.

**This is a port.** The layer was built and validated against a **stale fork** of `bench/` living
in `john-v3-multi-lang` (measure.py diverged 200 lines, metrics.py 99, schema.py 74). That fork
has no consumers. This repo's pipeline A (`unified_bench.py`) is the real home for this layer — NOT `runner/benchmark.py`, which is a different pipeline entirely (see section 0). The fork is deleted as
the last step (§10).

**Critically, this repo already has a failure taxonomy** and a token histogram. Most of what the
fork built duplicates them under different names. This spec keeps only what is genuinely new and
makes everything else speak the vocabulary already here.

**Branch:** all line citations are against **`node-producer`**, which is what runs on the VM at
`/opt/ratbench`. It is 18 commits ahead of `main`. `schema.py`, `metrics.py`, `unified_bench.py`
and `report/case_study.py` are byte-identical on both branches; only `measure.py` and
`languages/` diverged, so only `measure.py` citations differ. Do the work on `node-producer`.

---

## 0. `bench/` is TWO pipelines — read this before anything else

This is the single easiest thing to get wrong here, and an earlier draft of this spec got it
wrong: it built the taxonomy against pipeline A and specified the deliverable into pipeline B,
which share no rows.

```
A  unified_bench.py -> measure.py -> MeasureRow -> row.json / metrics.json
   the docker-rebuild remeasure harness.
   OWNS: status, contract.py, setup_compile_ok, collect_rc, collected_node_ids, collect_errors

B  runner/benchmark.py -> bench.rat_scorers -> _result_row.json
                       -> report/case_study.py -> case_study.json
   the live-agent production runner.
   OWNS: a DIFFERENT `status` (success | error | timeout, from RAT's predict())
```

`runner/` never imports `MeasureRow`, `bench.measure`, `bench.metrics`, or `bench.contract` —
it reaches pipeline A only by shelling out to `python -m bench.unified_bench` as a subprocess
(`runner/cli.py:228`). And `case_study.py:200` binds `row = rows_by_name.get(full, {})`: a plain
**dict** from `inline_score.score_agent()`, with none of the fields this layer reads.

Consequences, both binding:

- **This layer lives entirely in pipeline A.** Its inputs (`collect_errors`, `collect_rc`,
  `collected_node_ids`, `build_ok`, `env_status`) exist only there.
- **`case_study.json` is out of scope.** Putting an `errors` block there would require a join
  between the two pipelines that no code performs, and would only be defined for runs where a
  `unified_bench` measure pass also happened. The per-repo block goes in `row.json` (§6.5).
- The two `status` fields are **different vocabularies that share a name**. Never conflate them.

---

## 1. What already exists (do not rebuild)

| thing | pipeline | where | what it gives |
|---|---|---|---|
| `MeasureRow.status` | A | `schema.py:43`, assigned in `measure.py` | one verdict per measured env, set **at measure time** |
| `_DISQUALIFIED` | A | `metrics.py:12` | the denominator rule |
| `status_census` | A | `metrics.py:64` | per-status counts in the metrics output |
| `setup_compile_ok` | A | `schema.py:38`, `measure.py:267` | separates "setup.sh returned nonzero" from "build failed" |
| `contract.py` | A | `contract.py`, consumed at `measure.py:288-312` | in-container probe → `non_conforming` / `empty_testbed` / `py_test_files` |
| `error_breakdown` | **B** | `rat_scorers.py:54,113` | `{"ModuleNotFoundError": 12}` — a token histogram feeding `pass_rate_exclude_code_issues`. **Different pipeline**; it is prior art for the idea, not a field this layer can read. |

The status vocabulary (`schema.py:40-42`):

```
unmeasurable · error · missing · measure_error · build_fail · non_conforming
empty_testbed · no_tests_collected · collect_error · timed_out · executed · gate_fail
                                                    (+ legacy_ok / legacy_missing / ok)
```

assigned at four separate points in `measure.py` (line numbers on `node-producer`):

```
:245  env.status not in _MEASURABLE or not env.dockerfile   -> missing
:273  build_rc != 0                                         -> build_fail
:312  contract probe not conforming                         -> non_conforming | empty_testbed
:323  compiled-language gate, collect not clean             -> gate_fail
:344-351
        if timed_out:            status = "timed_out"
        elif not collect_clean:  status = "collect_error"
        elif executed:           status = "executed"
        else:                    status = "no_tests_collected"
```

The four gates before `:344` are why a derived status cannot reproduce the full vocabulary
(§3.1).

---

## 2. What is genuinely new

Three things, none of which collide:

1. **Error events** — the *identity* of each error, not just its type.
   `error_breakdown` says `ModuleNotFoundError: 12`. It cannot say **which package**. The fix
   list is the captured group (`wrapt`, `libGL.so.1`, `pytest_check`), and that is the most
   actionable field in the whole layer.
2. **Error surface** — whether the repo's errors were *observable at all* (§4). Orthogonal to
   `status`, and materially finer.
3. **Arm-vs-arm interval delta** with partial identification, plus a masking 2×2 (§7).

Everything else the fork built — its `failure_mode` tree of `no_env` / `infra_error` /
`setup_failed` / `test_timeout` / `zero_pass` / `partial` / `success` — is a re-derivation of
`status` under different names, and is **deleted**, not ported.

---

## 3. One taxonomy: `status` is authoritative

A taxonomy measured **at source** beats one derived after the fact. `contract.py` knows
`non_conforming`; `setup_compile_ok` knows setup-vs-build. Neither is reconstructible from a
stored row — which is exactly why the fork's `setup_failed` bucket turned out to have **zero**
attested instances across 100 real rows.

And the denominator argument is decisive: `_DISQUALIFIED` already exists. Two taxonomies means
two denominators.

### 3.1 `legacy_status` — backfill, in this repo's vocabulary

The 100-row corpus at `/opt/ratbench/remeasure_50` was measured by the fork and carries **no
`status` field at all** (`{'<<ABSENT>>': 100}`). `unified_bench.py:51-52` already backfills
`legacy_ok` / `missing` on aggregate, which is too coarse to analyse.

So: when `status` is absent or `legacy_ok`/`ok`, derive it — **into the existing vocabulary,
never a new one**:

```
env_status != "ok"                            -> missing
meta.error ~ CalledProcessError(125|DockerException|No space left  -> measure_error
build_ok is False                             -> build_fail
timed_out is True                             -> timed_out
collect_clean is False                        -> unknown_conformance   (see below)
executed is True                              -> executed
otherwise                                     -> no_tests_collected
```

### 3.1.1 The backfill covers 7 of 12 statuses, and must say so

An earlier draft claimed the order "matches `measure.py:281-288` so a derived row and a measured
row agree wherever both are possible." **That is false.** `measure.py` assigns three statuses at
gates *before* the `:344` block, from inputs that are never persisted:

- **`non_conforming` / `empty_testbed`** (`:312`) come from `contract.py`'s in-container probe.
  The probe's discriminating field is `reason` (`no_testbed` / `not_git` / `empty`), and
  `measure.py` **drops it** — only `probe["status"]` and `py_test_files` survive, and
  `py_test_files` is `0` on every failing branch, so it discriminates nothing.
- **`gate_fail`** (`:323`) is a compiled-language short-circuit.

A non-conforming repo still runs the collect step, so its `collect_clean` is usually false — and
a naive derivation would confidently label it `collect_error`. That is a **silent
mislabelling**, not a gap.

So the `collect_clean is False` branch emits **`unknown_conformance`**, not `collect_error`. It
is the one new name this spec introduces, and it exists precisely because the honest answer is
"this row could be `collect_error`, `non_conforming`, `empty_testbed`, or `gate_fail`, and
nothing persisted can tell them apart." Naming that is better than picking one.

`unknown_conformance` must be added to `_DISQUALIFIED`'s neighbours in documentation but **not**
to `_DISQUALIFIED` itself — it changes no existing denominator.

**The backfill must be loud.** `ArmErrorReport.n_status_derived` counts rows whose status was
derived rather than measured. A silent backfill hides "this whole corpus predates the taxonomy,"
which is the class of invisible assumption that produces confidently wrong numbers.

### 3.2 `bucket` — keeping the pass-rate split without a second taxonomy

`status` lumps everything that ran into `executed`. That collapses real signal: in the corpus,
`module_not_found` appears in repos that ended at every pass-rate level, and a third of them
recovered anyway.

One key, not two vocabularies:

```
bucket = status                              when status != "executed"
       = zero_pass | partial | success       when status == "executed", split on pass_rate
                                             (threshold 0.8, matching metrics.py)
```

`bucket` is what the cross-tab is keyed on. It never introduces a name that competes with
`status` — it only sub-divides `executed`.

**Caveat that must travel with any cross-language reading:** `pass_rate` is not comparable
across ecosystems. `measure.py`'s own comment documents a repo (Archipelago) whose pass_rate
swings **0.017 ↔ 0.990** depending on whether JUnit `tests` attributes or `<testcase>` elements
are counted, and the four producers in `bench/languages/` (pytest, gotestsum, Surefire/Gradle,
jest-junit) disagree about what counts as a test. A single 0.8 split is therefore defensible
*within* one language and misleading *across* languages. Split `bucket` by language before
comparing, or restrict the comparison to one.

Note also that `bench/attribution.py:74-81` already returns a dict keyed `"bucket"` for an
unrelated A/B/C/D taxonomy. No JSON key collides, but the word is overloaded in this package.

---

## 4. Error surface — and why it is not `collect_error`

```
unobserved   env_status != "ok"  OR  build_ok is False        -> never reached collection
masked       collect_rc != 0 AND len(collected_node_ids) == 0
             reason = "startup_abort"    if collect_rc in {3, 4}
                      "nothing_collected" otherwise
full         otherwise (may legitimately hold zero events)
```

**Only the masked-vs-full split is orthogonal to `status`.** `measure.py:346` sets `collect_error`
whenever `collect_clean` is false — *regardless of how many tests were collected*. So these two
repos get the identical status:

```
rc=2, 500 tests collected, 3 modules broken   -> collect_error   (we saw the whole surface)
rc=2,   0 tests collected, startup aborted    -> collect_error   (we saw ONE error, and nothing else)
```

Those are opposite epistemic situations. Measured over the corpus, an rc-in-{3,4} test finds
**7** masked baseline repos; the `collected_node_ids` test finds **16** — nine repos (Qiskit,
baserow, promnesia, vizro, supabase-py, testcontainers-python, press, EvalAI, Spoolman) exit
`rc=2` having collected **zero** tests and look like ordinary per-module failures by return code.

Surface is an **admissibility flag**, not a verdict: it says whether the event counts for this
repo are comparable to another repo's. It is the difference between *"tested negative"* and
*"wasn't tested."*

**Only the `masked` vs `full` split is new.** The `unobserved` branch
(`env_status != "ok" OR build_ok is False`) is the same predicate `measure.py` uses at `:245`
and `:273` to assign `missing` and `build_fail` — it is redundant with `status` and is kept only
so the field is total. Do not present the three-way enum as wholly orthogonal; one of its three
values is a restatement.

Corpus measurement:

```
                full   masked   unobserved
baseline          32       16            2
repaired          41        3            6
2x2: neither 34 / baseline-only 13 / repaired-only 0 / both 3
paired full-in-both: 30/50
```

---

## 5. Error events

```
token        verbatim FQN exception label, e.g. "redis.exceptions.ConnectionError"
group        captured payload: "wrapt" | "libGL.so.1" | "_accelerate" | "/opt/x.cfg"
group_kind   module | soname | name | path | ""
category     DERIVED view over (token, group, group_kind, repo_toplevel, language)
source       collect | run | install     — NEVER pooled (§8)
occurrences  raw lines collapsed by dedup on (source, token, group)
raw          the first raw line, 200 chars, kept for audit
```

**Token is the substrate; category is a view over it.** The token is machine-emitted and exactly
reproducible; the category is a claim. Storing both means any grouping decision is auditable and
re-derivable offline without a re-measure.

Category table (first match wins; order is the contract):

```
token startswith "_pytest." | "Pytest" | == "UsageError"        -> harness_error
token endswith "ModuleNotFoundError"
        group's first segment ∈ repo_toplevel                   -> internal_import_failure
        else                                                    -> module_not_found
token endswith "ImportError"
        group_kind == "soname"                                  -> syslib_missing
        group_kind == "name"                                    -> partial_import
        group_kind == "module"  -> internal_import_failure | module_not_found (as above)
        else                                                    -> import_failed
token endswith ConnectionError|ConnectionRefusedError|ConnectionResetError|DockerException
                                                                -> service_unavailable
token ∈ SyntaxError|IndentationError|TabError                    -> syntax_error
token == "FileNotFoundError"                                     -> file_missing
anything else                                                    -> uncategorized
```

Deliberately absent: `compile_error`, `toolchain_missing`, `pip_resolution_conflict`,
`version_incompatible` — measured zero times at collection across 100 rows, because those
failures kill the *install*. They belong to a separate install-log classifier.
`OperationalError` is deliberately **not** a service token — sqlite/sqlalchemy raise it for
"no such table" far more often than for a refused connection.

Extraction rules that are load-bearing (all three were review findings against working code):

- The token must be **anchored on its trailing colon**, `(?=:(?!:))`. A bare
  `\b[\w.]*(?:Error|Exception)\b` scan picks the first `*Error` on the line, which on a
  `FAILED tests/x.py::test_raises_ValueError - AssertionError: …` line is the **test name**.
- The soname pattern must be **bounded on both sides**. Unbounded, `matplotlib.something`
  yields a fabricated soname `lib.so` written into `group` — the reproducible primary key.
- Anchored `module`/`name` patterns are checked **before** `soname`.
- Warning tokens never become events.

---

## 6. Storage design

### 6.1 Cardinality is the schema

```
status / bucket   exactly 1 per repo   scalar   mutually exclusive, feeds denominators
error_surface     exactly 1 per repo   scalar   admissibility, not a verdict
events            0..N per repo        list     non-exclusive, carries counts
```

They cannot be collapsed. A `success` repo can carry twelve `module_not_found` events —
surfaced and repaired, i.e. the system working. A `collect_error` repo can carry exactly one.
Same error type, opposite verdicts. **Error type is what you observe; status is what you
conclude.**

### 6.2 Scalar verdicts must ship their evidence

A single-valued verdict is legitimate only if every input that produced it is also persisted;
otherwise it is unfalsifiable and can never be re-derived under a changed rule. `status` mostly
satisfies this (`build_ok`, `timed_out`, `collect_rc`, `pass_rate`, `env_status`,
`setup_compile_ok` are all on the row).

**Three statuses fail this rule today**, which is why §3.1.1 exists rather than pretending they
can be derived:

- `non_conforming` / `empty_testbed` — `contract.py`'s probe returns the discriminating field
  `reason` (`no_testbed` / `not_git` / `empty`), and `measure.py:288-312` **drops it**. The
  surviving `py_test_files` is `0` on every failing branch, so it discriminates nothing. An
  earlier draft of this section claimed `py_test_files` was that evidence; it isn't.
- `gate_fail` — a compiled-language short-circuit with no persisted marker.
- Also unpersisted: `HarvestedEnv.status == "ok"` with `dockerfile is None` yields `missing`
  (`measure.py:245`), and nothing records "had no dockerfile."

Persisting `contract_reason` on `MeasureRow` would retire the first of these. Out of scope, but
it is the single cheapest thing that would make the taxonomy fully falsifiable.

### 6.3 `status_flags` — for what the ordering hides

First-match-wins is what makes buckets sum to *n*, and that property is what makes arm-vs-arm
comparison work. But it is lossy: a repo that both timed out and failed to build is labelled
`timed_out`, permanently.

The fix is **not** a set — that breaks the denominator. It is a scalar plus flags:

```json
"status": "timed_out",
"status_flags": ["build_fail", "unconverged"]
```

One value for counting, the rest for reading. `unconverged` is a flag for the same reason: as a
*mode* it would be unreachable for an arm with no repair loop and near-universal for one with,
making the delta a labelling artifact rather than a measurement.

### 6.4 Event shape differs by artifact, on purpose

```json
(a) {"ModuleNotFoundError": 12}                                   error_breakdown today
(b) [{"token":"ModuleNotFoundError","group":"wrapt","category":"module_not_found",
      "occurrences":12,"raw":"E   ModuleNotFoundError: No module named 'wrapt'"}]
(c) {"ModuleNotFoundError": {"wrapt": 12, "httpx": 3}}
```

- **`case_study.json` uses (b).** It is the audit artifact; `raw` belongs there, and offline
  re-derivation of categories reads from it.
- **Aggregates use (c).** Compact, queryable, and `raw` would be noise at scale.
- **(a) alone is insufficient** and stays only for RAT parity: a count without the group says
  23 repos have a problem and nothing about what to fix.
- **`row.json` stores neither list** — only the two scalars (§6.5). No reason to duplicate a
  list into every row.

### 6.5 Where each field lands

```
row.json          status, status_flags, error_surface, error_surface_reason  (scalars)
   "errors": {    surface, surface_reason, status_derived,
                  events: [ …(b)… ], by_category: {…}     }

errors.json       per arm, per source: bucket census, token/category counts in form (c),
                  crosstab, top_groups, plus _delta

react_trace.jsonl per-turn events on `run`/`test` phases (§8) — deferred, and never summed
                  into errors.json
```

**`case_study.json` is deliberately NOT in that list.** An earlier draft put the `errors` block
there, which is unimplementable: `case_study.py:200` holds a plain dict from
`inline_score.score_agent()`, not a `MeasureRow`, and pipeline B has none of the fields this
layer reads (§0). Wiring it as described would raise `AttributeError` on the first task and —
because `runner/benchmark.py:863-876` wraps the consolidation in a best-effort `try/except` —
would silently produce **zero** `case_study.json` files for the whole run rather than failing
loudly.

If a per-repo block in `case_study.json` is wanted later, it needs an explicit join step (load
the `row.json` for that agent/repo and reconstruct a `MeasureRow`), and it is only defined for
runs where a `unified_bench` measure pass also happened. That is new plumbing, out of scope here.

Putting the block in `row.json` costs one extra field and keeps producer and consumer in the
same pipeline.

### 6.6 Two count units, both stored

```
occurrences  135   raw lines, per repo, deduped by (token, group)
repos         12   how many repos hit it at all
```

`ModuleNotFoundError` is 135 events across 12 admissible repos — one repo with a large suite
dominates the event count. **Repo-presence is the unit for comparison; occurrences is a
diagnostic.** Storing only one guarantees someone reads the wrong one.

---

## 7. The arm-vs-arm delta is an interval

Excluding masked repos from the comparison is **post-treatment conditioning**: `error_surface`
is an *outcome* of the arm — a repo becomes unmasked *because* the arm improved. The sign of the
bias is undetermined, and it discards real evidence, since a masked repo usually still carries a
named startup cause.

So each repo contributes per category:

```
present   repo has ≥1 event of this category on an observed part of its surface
absent    repo's surface is FULL and has no event of this category
unknown   repo is masked or unobserved, and this category is not among its known events
```

A masked repo contributes its startup cause to `present` and everything else to `unknown`. The
delta is a bound:

```
lower = b.present - (a.present + a.unknown)
upper = (b.present + b.unknown) - a.present
identified = lower > 0 or upper < 0
```

If the interval excludes zero the direction holds regardless of what masking hid. The paired
intersection survives only as a **sensitivity row**, never the headline.

**Known gap: repos present in only one arm.** `category_status` iterates each arm's own row list,
so a repo in A's corpus but not B's contributes nothing to B's `unknown` bucket — it is simply
absent on that side rather than folded in as maximal uncertainty, which is what `masking_2x2`
correctly does via `only_in_a` / `only_in_b`. That **understates** the interval width and can let
`identified` fire on a bound a full accounting would have widened past zero. On the acceptance
corpus `only_in_a == only_in_b == 0`, so it does not bite there — but the code path is unguarded,
and any run with asymmetric coverage must not be read through this without fixing it first.

`masking_2x2` reports cells, not marginals — `16 → 3` is arithmetically compatible with repos
regressing; the measured cells (`neither 34 / a-only 13 / b-only 0 / both 3`) rule that out.
Repos present in only one arm are reported as `only_in_a`/`only_in_b`, never silently counted as
"not masked".

**Power ceiling, to be stated before any number is read:** with four categories that never fire,
one token dominating, and intervals widened by masking, this is effectively a two-column test at
n≈40. On the corpus exactly one category is identified (`module_not_found`, `[-20, -5]`); the
other nine span zero. That is the honest resolution of this design at n=50.

---

## 8. Construction-time and final-state errors are never pooled

An error surfaced and repaired mid-trajectory is the system **working**. One surviving to the
final run is a **failure**. Pooling makes "fewer errors" ambiguous between "better repair" and
"gave up earlier."

Therefore:

- `errors.json` reads **exclusively** from the final measurement.
- Per-turn events live **only** in `react_trace.jsonl`, attached to `run`/`test` phases, and are
  never summed into the aggregate.
- `source` (`collect` / `run` / `install`) is likewise never pooled: the run pass mixes *code*
  failures into an *environment* metric — an ordinary test `AssertionError` would otherwise land
  in `uncategorized` and drive the completeness number with how many tests failed.

---

## 9. Non-goals

- No LLM in the classification path. An LLM may mine `uncategorized` offline to propose regexes
  for review; it never labels a row. Counts stay exactly reproducible.
- No change to `compute_metrics`, `metrics.json`, `status`, `_DISQUALIFIED`, or any existing
  scorer. This layer is additive.
- No re-measure. Every new capture field defaults empty and nothing in this spec reads them; the
  next re-measure retires the `legacy_status` shim for free.
- No install-log classifier (`syslib_missing`, `compile_error`, …). Deferred.
- Non-Python signature tables. The table is keyed by language from day one and this repo already
  has `bench/languages/`, with only `python` populated. Note `language` exists on `RepoSpec`
  (`schema.py:12`) but **not** on `MeasureRow` — the port adds it there, defaulted, so the seam
  terminates at a real field rather than at a grep that looked like a hit.

---

## 10. Deleting the fork

Last step, after the acceptance run passes: delete `bench/` and `tests/bench/` from
`john-v3-multi-lang`, plus `tests/test_metrics.py`, which is its only other importer. Nothing in
that repo imports `bench.*` — `run_rat_benchmark.py` and `multi_docker_eval_adapter.py` never
did.

Do not delete before the acceptance run: the 100-row corpus was produced by that fork's
`measure.py`, and §11 is the check that the two agree about the stored rows.

---

## 11. Acceptance

Run the new aggregation over `/opt/ratbench/remeasure_50` (copied, not written into) and require:

```
baseline.collect   n_admissible=32  n_masked=16  n_unobserved=2
repaired.collect   n_admissible=41  n_masked=3   n_unobserved=6
_delta.collect.masking_2x2 == {neither 34, a_only 13, b_only 0, both 3, only_in_a 0, only_in_b 0}
_delta.collect.sensitivity_paired.paired_repos == 30
module_not_found interval == [-20, -5], identified true
n_status_derived == 50 per arm   (the whole corpus predates `status`)
syslib_missing absent from category_repos in both arms
*.run.token_repos == {}          (run_failed_lines unpopulated; a non-empty run report means
                                  sources got pooled)
```

These are **measured**, not estimated — but they are a **regression gate, not a validation
gate**, and the difference matters:

The corpus was produced by the fork's `measure.py`, which has **no contract probe, no
per-language dispatch, and no `status` field at all**. So by construction it never exercised
`non_conforming`, `empty_testbed`, `gate_fail`, or `setup_compile_ok`. Reproducing these numbers
proves the *event and surface* layers survived the port unchanged. It proves **nothing** about
`legacy_status`'s handling of the three unreachable statuses (§3.1.1) or about any branch this
repo's `measure.py` has and the fork's did not.

What would legitimately differ without indicating a bug: nothing, for a pure re-aggregation of
frozen rows. The moment these run against a *live* re-measurement, the fixed numbers stop being
meaningful and the gate must be re-derived.

Two gate criteria are also weaker than they look: `n_status_derived == 50` proves the counter
increments, not that the *right* status was derived; and `syslib_missing absent` is satisfied
identically whether the categoriser correctly found zero or its soname regex never fires.
