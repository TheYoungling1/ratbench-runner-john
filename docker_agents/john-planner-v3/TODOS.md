# TODOS

Deferred items from the deterministic-envstate-snapshot design + eng review (2026-06-10).
See `docs/superpowers/specs/2026-06-10-deterministic-envstate-snapshot-design.md` §12.

## Deferred (post-snapshot)

- **Deep / transitive dependency resolution for `required`**
  - What: resolve the full pinned tree (lock files / resolver) behind the existing `parse_manifests` interface.
  - Why: exact, complete `required` set if we ever want it to drive a pinned/offline install rather than be an advisory hint.
  - Why deferred: shallow declared-names is enough for a Planner hint; pip resolves transitively at install time; collect-only gates termination.
  - Start at: `src/envstate/manifest.py` (add a deep mode, same `ManifestResult`).

- **Multi-language snapshot providers (JS / Go / etc.)**
  - What: per-language `probe_env` + `parse_manifests` implementations behind the Python-first seam.
  - Why: the harness is multi-language (`language_handlers.py`); non-Python repos currently fall back to reactive behavior.
  - Why deferred: Repo2Run (the active benchmark) is Python; YAGNI until non-Python repos are in scope.

- **Planner gap-view / `installed` count-cap**
  - What: render `required − installed` + a count instead of the full `installed` list in `render_planning_view`.
  - Why: keeps the Planner prompt compact if a fat base image (anaconda-style, 300+ pkgs) ever appears.
  - Why deferred: measured a non-issue — `pip freeze` = 0 on a fresh `python:3.x`; `installed` only grows to the project's own footprint. Revisit if a fat base image shows up.

- **`COLLECT_ONLY_CMD` dedup (pre-existing)**
  - What: `orchestrator.py` (full command) and `maintainer.py` (`"--collect-only"` substring) share a name with different values.
  - Why: same name, two meanings = refactor footgun.
  - Why deferred: pre-existing, outside this plan's scope. Separate cleanup.

- **System-package (apt/dpkg) install planning**
  - What: capture `dpkg` into `env` and let the planner reason about system packages deterministically.
  - Why: system libs (libpq-dev, build-essential) are a real failure layer.
  - Why deferred: stays reactive/LLM for now; the `resolved` list (Issue 3) clears system problems without a full dpkg snapshot.

## [ ] Tiered concurrency for benchmark runner (heavy solo / light high-K)
**What:** Scheduler runs heavy repos at low K (or a solo lane), light repos at high K.
**Why:** Heavy repos (darts, LibreTranslate, OpenManus, markitdown, memU-server, Scrapling) can exceed the 7200s per-repo budget under contention and flip to a false status:timeout. Tiering maximizes throughput while protecting fidelity.
**Pros:** Best wall-clock without contention-induced false timeouts.
**Cons:** More scheduler logic; needs a heavy/light tag per repo (the `size` field already exists in the subset).
**Context:** Eng review 2026-06-05 chose "start K=3, measure first." Once real per-repo timings at K=3 exist, tiering is the throughput-optimal end state. Lives in run_rat_benchmark.py scheduler.
**Depends on:** the K=3 measurement pass.
**Date:** 2026-06-05 | Source: /plan-eng-review

## [ ] Decide: pin repo SHAs for the RAT benchmark (fidelity vs RAT-parity)
**What:** Clone each benchmark repo at a pinned commit SHA instead of default-branch HEAD.
**Why:** Codex eng-review (2026-06-05) flagged unpinned HEAD as the single biggest source of invalid run-to-run comparison — bigger than any parallelization detail. A repo's code (and tests) can change between runs, so a pass-rate delta may reflect upstream drift, not the agent.
**Pros:** Reproducible, comparable runs; isolates agent quality from upstream churn.
**Cons:** Diverges from how RAT itself runs (its dataset carries no SHA), so head-to-head vs released RAT/Repo2Run baselines would no longer be apples-to-apples. Need to pick + store SHAs.
**Context:** Surfaced during /plan-eng-review outside voice on the parallel-runner spec. This is a benchmark-semantics decision, not a perf detail — keep it out of the parallel-runner scope. Record chosen SHAs in the subset JSON if adopted.
**Depends on:** none (orthogonal to the scheduler).
**Date:** 2026-06-05 | Source: /plan-eng-review (codex outside voice)
>>>>>>> rat-bench-integration
