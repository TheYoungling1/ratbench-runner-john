# Cost & Token Metering: Settled Cost (Stage 1) + Loopback Ledger (Stage 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `economy` column comparable across arms. Today five arms report tokens and cost from five different sources with five different semantics, and one arm (`executionagent`) reports nothing at all. Stage 1 replaces guessed cost with the provider's settled cost wherever the provider offers one. Stage 2 moves metering off the agents entirely and onto a loopback proxy that sees every request whether or not the agent cooperates.

**Architecture:** Two stages, deliberately sequenced so Stage 1 is useful standing alone and Stage 2 subsumes it.

- **Stage 1** injects OpenRouter's `usage: {include: true}` accounting extension at each agent's existing request-construction site and records the returned `usage.cost` into the `cost_usd` field that already exists in `_meta.json`. No new processes, no new files, no topology change. Works only where the upstream is OpenRouter.
- **Stage 2** stands a stdlib HTTP proxy on loopback, points the host-process arms at it, and derives every economy number from one ledger. Provider-independent for tokens; cost is settled by OpenRouter where offered and *computed* for DeepSeek-direct from billing inputs the proxy captures (`ts` for the peak window, cache hit/miss split). Stage 1's per-agent injection then moves into the proxy and the agents stop doing it.

The framing that unifies both: **capture the billing inputs at the boundary, settle from the provider where it offers a number, compute where it does not.** A price table is only wrong when applied to tokens you never characterised.

**Tech Stack:** Python 3.10+ (runner venv) and 3.11+ (sweagent venv), stdlib only (`http.server`, `json`, `urllib.request`, `threading`), pytest. **No new third-party dependencies.**

---

## Global Constraints

- **Telemetry only. EBSR/ESSR must not move.** Every arm's measured build/test outcome must be byte-identical before and after. The only permitted diffs in `_meta.json` are economy fields and one new `usage_source` discriminator.
- **Anti-vanish invariant (design §1) is inviolable.** Metering failure must never turn a successful, already-paid-for produce into an error. Every ledger read, proxy call and cost computation is wrapped; failure yields `None`, never a raise. Precedent: `producers/executionagent.py::_harvest_economy`, which swallows every OSError and returns a partial dict rather than propagating.
- **Dependency direction holds.** `producers/` must not import `runner/`. `bench/` must never import `rat/`. The proxy is a standalone script invoked by path, not an import, so it belongs beside its consumer and is shared by path — see Phase 2.1 for placement and the open question on it.
- **Additive contract only.** New fields default to `None`. A row with no settled cost carries `cost_usd: null`, never `0` — a missing cost must never be readable as a free event.
- **Repo root:** `/Users/john/ratbench-runner-john`. **Test command:** `python3 -m pytest <path> -q` from the repo root.
- **`swe-maintain`'s bridge is a RECONSTRUCTION.** Its own header states it "is NOT the implementation that produced the authors' published numbers." Port the design; do not inherit the assumption that it has been proven at scale.
- **Existing price tables stay.** `_KNOWN_MODEL_COSTS` in `producers/sweagent_repo2run_runner.py` and `runner/live/sweagent_runner.py` exist to satisfy SWE-agent's `ModelConfigurationError` guard (`sweagent/agent/models.py:747-754` raises when `litellm.cost_calculator.completion_cost` fails while a cost limit is set). They must remain registered. After Stage 1 they no longer *report*; they only keep the guard quiet.

---

## Background: the five provenances

| arm | economy source today | shape |
|---|---|---|
| `dockeragent` (5 varieties) | `rat/libkit/llm.py` response usage | tokens |
| `repo2run` | `output/<repo>/track.json`, last numeric `cost_tokens` | one cumulative total, no in/out split |
| `sweagent` | litellm `model_stats` against a registered price table | self-described "approximate (~within 2x)" |
| `sweagent_repo2run` | same table (`_KNOWN_MODEL_COSTS`) | same |
| `executionagent` | **nothing** — EA persists no token or cost totals | cycle counts only |

`producers/executionagent.py::_harvest_economy` can only count `cycles_chats/cycle_*/prompt_*.json` files, because EA's `LitellmModel` holds `.cost`/`.n_calls` in memory and never writes them. That blank is the argument that settles Stage 2: it cannot be fixed without patching EA, and a proxy needs no cooperation from the agent.

### Why price tables cannot be made right

DeepSeek bills on two axes (verified against `api-docs.deepseek.com/quick_start/pricing`, 2026-09-03):

- **Time of day.** Peak is 01:00–04:00 and 06:00–10:00 UTC, Mon–Fri. Off-peak is exactly half price.
- **Cache hit vs miss.** For `deepseek-v4-flash`: input `$0.007`/1M on a cache hit versus `$0.22`/1M on a miss — a **31× spread**.

OpenRouter's flat rate for the same model (`6.5e-8`/token input) matches neither. It is OpenRouter's own blended price, correct only if you route through OpenRouter.

---

## Verified facts (do not re-derive)

Established by reading the tree on 2026-09-03. File:line references are load-bearing.

> **Revision note (2026-09-03).** This plan was adversarially reviewed after its first draft. Fact 4 originally recommended **deleting** `rat/libkit/tools/llm.py`; that recommendation was wrong and would have broken `read-file`/`edit-file` across six varieties — it is corrected below. Fact 7 (the MiniMax sibling landmine) was found by the review and is new. Phase 2.3 was restructured after the review judged its original "move the pin into the proxy" approach to be a measurement change, not telemetry. Phase 2.2's success check and Phase 2.6's preflight were both corrected. The review confirmed facts 2, 3, 5 and 6 as originally written, and traced the anti-vanish invariant end-to-end with no path where telemetry failure loses a paid-for produce.

1. **`rat/libkit/llm.py:17-30` — `_openrouter_extra_body(existing)`** already builds an `extra_body` dict and merges a provider-pin block into it. This is the Stage 1 injection point for every `dockeragent` variety. The OpenAI SDK merges `extra_body` contents into the top-level JSON body, so `extra_body={"usage": {"include": True}}` produces a top-level `usage` field — the shape OpenRouter expects.

2. **`rat/libkit/llm.py:10-14` — `_is_openrouter(base_url, model)` keys on `"openrouter" in base_url`.** This is a **Stage 2 landmine**: pointing `OPENROUTER_API_BASE` at `http://127.0.0.1:PORT/v1` makes this return `False`, silently dropping the provider pin and `allow_fallbacks: False`. Redirection would therefore change which upstream serves the request — a real behaviour change smuggled in by a telemetry feature. Resolution in Phase 2.3.

3. **`rat/libkit/config.py:135-141` — the OpenRouter path** is selected when the model slug contains `/` **or** `LLM_API_PROVIDER == "openrouter"`, and reads `base_url` from **`OPENROUTER_API_BASE`** (default `https://openrouter.ai/api/v1`). That env var — *not* `OPENAI_BASE_URL` — is the redirection hook for the `dockeragent` arms. `OPENAI_BASE_URL` (`config.py:54`) only serves the `"gpt"` family, which no current variety uses.

4. **`rat/libkit/tools/llm.py` cannot bill, and MUST NOT be deleted.** It hardcodes `base_url: "https://api.deepseek.com"` *and* a placeholder key `"sk-<your_deepseek_api_key>"`, so it would 401 rather than produce billable traffic — it is a *latent* bypass, not an active one.

   **It is also load-bearing.** An earlier draft of this plan claimed two guarded importers and recommended deletion. That was wrong, and an adversarial review caught it. There are **eight** importers, and three are **unguarded** bare module-level imports:

   ```
   rat/libkit/tools/read_file.py:20        from llm import LLMChat   ← UNGUARDED
   rat/libkit/tools/edit_file.py:21        from llm import LLMChat   ← UNGUARDED
   rat/libkit/tools/edit_file_copy.py:20   from llm import LLMChat   ← UNGUARDED
   rat/libkit/tools/create_test.py:15      (guarded)
   rat/libkit/tools/cicd_config.py:43      (guarded)
   rat/libkit/tools/search_web.py:27       (guarded)
   rat/libkit/tools/retrieve_issue.py:23   (guarded)
   rat/libkit/tools/search_repo.py:25      (guarded)
   ```

   `read_file.py` and `edit_file.py` are dispatched **as scripts inside the container** — `tool_dispatcher.py:45` sets `tools_path = "/home/tools"`, and `:530`/`:687` run `python3 /home/tools/read_file.py` and `.../edit_file.py`, bound to the `read-file` / `edit-file` actions at `:60`/`:67`. Python sets `sys.path[0]` to the script's own directory, so `from llm import LLMChat` resolves to `/home/tools/llm.py` — this file. The `sys.path.append(parent_dir)` at `read_file.py:17-19` appends `/home` in-container, which holds no `libkit/`, and `rat/libkit/llm.py:7` needs `from libkit.config import config` which is unavailable there anyway.

   The `use_llm=False` gating claim was also false: `edit_file.py:287` declares `use_llm_fallback: bool = True`, `:805` passes `use_llm_fallback=not args.no_llm_fallback` (CLI default on), and `FileEditor.__init__` at `:39` constructs `LLMChat(llm_name)` **unconditionally**.

   **Deleting it raises `ImportError` at module import, before argparse — breaking `read-file` and `edit-file` on every invocation for `rat` and all five `dockeragent` varieties.** It would present as the agent being bad at file operations, not as a metering regression. See Phase 2.2 for the repair.

5. **`producers/sweagent_repo2run_runner.py::_apply_overrides`** already sets `extra_body.provider` and gates on `str(a.llm).startswith("openrouter/")` — keyed on the **model slug**, not the base URL, so unlike fact 2 this one survives redirection.

6. **`producers/base.py::write_env_packet`** already writes `cost_usd` into `_meta.json`. **Stage 1 needs no schema change** — only a populated value. `bench/bench/measure.py` builds `base_row` from that meta.

7. **`rat/libkit/llm.py:60` — a SECOND redirection landmine of the same class as fact 2.** Ten lines below `_is_openrouter`, the MiniMax branch gates on `"minimaxi" in (self._base_url or "")` and sets `extra_body.thinking` from `MINIMAX_THINKING`. Redirecting to `127.0.0.1` makes this `False` too, silently dropping thinking-mode configuration. The path is live: `config.py:122-131` selects it on `LLM_API_PROVIDER=minimax` or a `minimax`/`abab` slug. **Any fix that patches only `_is_openrouter` leaves this one armed.** Resolution in Phase 2.3.

8. **`swe-maintain`'s `_usage_row` reads the OpenAI cache shape:** `usage["prompt_tokens_details"]["cached_tokens"]`. **DeepSeek returns top-level `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`** (verified at `api-docs.deepseek.com/guides/kv_cache`). A straight port records `0` cached tokens on DeepSeek-direct — silently, looking healthy, against a 31× price spread. This is the single highest-value bug to not ship.

---

## Stage 1 — settled cost from OpenRouter

Small, self-contained, no topology change. Deliverable: `cost_usd` in `_meta.json` is the provider's settled figure on every OpenRouter-routed arm, and `usage_source` says where it came from.

### Phase 1.1 — the shared guard and extractor

- [ ] Add a pure module `producers/usage_accounting.py` with two functions and no I/O:
  - `wants_accounting(base_url: str) -> bool` — `"openrouter." in (base_url or "")`. **Never send unconditionally.** A strict OpenAI-compatible endpoint rejects unknown top-level body fields with a 400; `swe-maintain`'s bridge carries the same warning in-comment. Honour an explicit override env (`RATBENCH_USAGE_ACCOUNTING` in `{"0","1"}`) that beats the sniff, mirroring `SEWORLD_USAGE_ACCOUNTING`.
  - `settled_cost(usage: dict) -> float | None` — return `float(usage["cost"])` when present and numeric, else `None`. Never `0.0` as a fallback.
- [ ] **Tests** (`producers/tests/test_usage_accounting.py`): sniff true for an OpenRouter base, false for `api.deepseek.com`, false for empty/None; env override wins both directions; `settled_cost` returns `None` for missing/`null`/non-numeric/bool, and the float otherwise.
- [ ] **How we know this worked:** unit tests only; no live traffic in this phase.
- [ ] **Rollback:** delete the file. Nothing imports it yet.

### Phase 1.2 — inject at the three reachable call sites

Each site already has an `extra_body` (or equivalent) construction point. Add the `usage` block behind `wants_accounting`, and harvest `usage.cost` off the response.

> **Decided (Q7): Stage 2 subsumes this.** When the proxy lands, injection moves there and these three call sites are **removed**, so one field is never written by two code paths. That means this phase's wiring is temporary by design — keep it small and resist elaborating it. `producers/usage_accounting.py` (Phase 1.1) survives the transition; the per-site wiring does not. If Stage 1 and Stage 2 are going to land in the same push, skip straight to Stage 2 and do the injection once, at the proxy.

- [ ] **`rat/libkit/llm.py`** (all 5 `dockeragent` varieties, plus `rat` and `repo2run`'s RAT-side calls). Extend `_openrouter_extra_body` to also `setdefault("usage", {"include": True})`. Harvest `response.usage.cost` where the response carries it and thread it into whatever the caller already records. *Note the dependency direction:* `rat/` cannot import `producers/`, so the 8-line guard is duplicated here rather than imported. Duplicating a two-line predicate across a boundary the repo deliberately maintains is cheaper than a shared package; keep the two copies textually identical and cross-reference them in comments.
- [ ] **`producers/sweagent_repo2run_runner.py::_apply_overrides`** — add `request-level` usage accounting to `completion_kwargs`. Verify it reaches the wire: SWE-agent passes `completion_kwargs` through litellm, which forwards unknown keys to the provider body for OpenRouter.
- [ ] **`runner/live/sweagent_runner.py::_ensure_sweagent_config`** — same, in the `completion_kwargs` block it already patches.
- [ ] **ExecutionAgent** — EA constructs `LitellmModel(model_name=..., model_kwargs={})` at `main.py:855`. Its `model_kwargs` are forwarded to `litellm.completion`. **This requires patching EA, which we do not vendor.** Do not attempt it in Stage 1. EA's cost stays `None` until Stage 2. Record this explicitly in the phase notes so it is not mistaken for an oversight.
- [ ] **Tests:** for each site, a unit test on the *body-construction* function asserting `usage.include` present for an OpenRouter base and absent for a DeepSeek base. No live calls.
- [ ] **How we know this worked:** one smoke repo per touched arm (`./run_bench.sh <variety> --only pallets/itsdangerous`), then `jq '.cost_usd, .usage_source' runs/<variety>/*/output/*/*/_meta.json` shows a non-null float and `"settled"`. Cross-check the figure against the OpenRouter dashboard for the same window — they should agree to the cent, which is the whole point.
- [ ] **Rollback:** revert the guard call at each site; the tables are untouched so the guard keeps working.

### Phase 1.3 — record provenance

- [ ] Add `usage_source` to the `_meta.json` payload in `producers/base.py::write_env_packet`, defaulting to `None`. Values: `"settled"` (provider returned cost), `"table"` (litellm price map), `"computed"` (Stage 2, priced from captured billing inputs), `None` (nothing known).
- [ ] **Decided (Q5): `_meta.json` only.** Do not thread `usage_source` into `MeasureRow` or `metrics.json`. Nothing reports on it yet, and the field is one `jq` away in the packet. Add it to the measure pipeline when a report actually groups by it, not before.
- [ ] Populate it wherever `cost_usd` is populated.
- [ ] **Tests:** extend `producers/tests/test_base.py` — a packet with no economy still writes `usage_source: null`; a settled economy writes `"settled"`. Assert no other `_meta.json` key changed (guards the additive-only constraint).
- [ ] **How we know this worked:** `git diff` on a re-produced `_meta.json` from a prior run shows exactly one added key.
- [ ] **Rollback:** remove the key; consumers read it with `.get()`.

### Phase 1.4 — demote the price tables to a labelled fallback

**Decided (Q1):** keep the table-derived cost, clearly labelled. A 2×-wrong number you can filter on beats a hole, and `usage_source` makes it filterable.

- [ ] Precedence for `cost_usd`, highest first: **settled** (provider returned it) → **table** (litellm price map) → **null**. `usage_source` records which one produced the value: `"settled"`, `"table"`, or `None`.
- [ ] A `"table"` cost is never silently upgraded and never presented as authoritative. Any report that sums cost across arms must either filter on `usage_source == "settled"` or state that it is mixing settled and estimated figures.
- [ ] The tables also stay registered for SWE-agent's `ModelConfigurationError` guard, which is a separate job from reporting. Add a comment at both `_KNOWN_MODEL_COSTS` definitions saying so, so a future reader does not "fix" them back into the primary reporting path.
- [ ] **Tests:** a settled cost wins over an available table cost; a missing settled cost falls through to `"table"`; neither available yields `cost_usd: None` and `usage_source: None` — never `0.0`.

---

## Stage 2 — the loopback metering ledger

### Scope

**In:** `sweagent`, `sweagent_repo2run`, `executionagent`, `dockeragent`. All four are host processes reachable on `127.0.0.1`.

**Out:** the `claudecode` lanes, at both stages — Claude Code is not a baseline for this benchmark.

**Out (decided, Q6):** `repo2run`'s own subprocess. Repo2Run is not vendored here, so whether its entrypoint honours a base-URL env var is unread, and John has ruled it not worth the verification right now. **This exclusion must be recorded in the run manifest, not just here** — `repo2run`'s economy keeps coming from its own `track.json` (one cumulative total, no in/out split, `usage_source: null`). A partial ledger that is documented is fine; one that is silently partial is the exact failure this plan exists to prevent. Revisit if `repo2run` ever needs to be cost-compared against another arm.

Note that `repo2run`'s **RAT-side** calls still route through `rat/libkit/llm.py` and therefore *are* metered — it is only Repo2Run's own subprocess that escapes. That makes `repo2run` the one arm with a knowingly mixed ledger, which is the strongest reason to record the exclusion where a reader of the numbers will see it.

### Phase 2.1 — the proxy

- [ ] Write `tools/metering_proxy.py`: stdlib `ThreadingHTTPServer` bound to `127.0.0.1:0`, writing the chosen port to a `--ready-file` as JSON. Forwards `/v1/chat/completions` to the real upstream held in its own env (`RATBENCH_UPSTREAM_BASE`/`RATBENCH_UPSTREAM_KEY`), which the agent process never sees.
- [ ] Two JSONL ledgers, per `swe-maintain`'s split:
  - **audit** — one row per logical request, appended **before transport**, so a request that fails upstream still consumes its budget slot.
  - **usage** — one row per successful response.
- [ ] Usage row schema:
  ```
  ts, request_id, model, input_tokens, output_tokens,
  cache_read_input_tokens, cache_write_input_tokens,
  cost_usd | null, usage_source, provider | null, arm, full_name
  ```
- [ ] **Placement (decided, Q2): `tools/metering_proxy.py`.** `tools/` is imported by neither `producers/` nor `bench/`, and the proxy is launched by path rather than imported, so no dependency direction is violated. The README calls `tools/` "operator scripts (optional)"; update that line to note the proxy is load-bearing when metering is enabled, so nobody prunes it as optional.
- [ ] **But the ledger READER is not the proxy.** `ledger_economy` (Phase 2.6) is imported by `producers/`, and `producers/` must not depend on an optional operator directory. Put the pure reader in `producers/ledger_economy.py` and keep only the *server* in `tools/`. Server invoked by path, reader imported — the split satisfies both constraints.
- [ ] **Tests** (`tools/tests/test_metering_proxy.py`): drive it against a stub upstream `HTTPServer` on another loopback port, exactly as `swe-maintain/tests/test_bridge_metering.py` does. Assert: ready-file carries a live port; a 200 writes one audit and one usage row; a 500 writes an audit row and **no** usage row; the agent never receives the upstream key.
- [ ] **How we know this worked:** the stub-upstream test suite passes without Docker or any API key.
- [ ] **Rollback:** the file is inert until something points at it.

### Phase 2.2 — redirection, per arm

Each arm needs a verified hook. **Verify, do not assume.**

- [ ] **`dockeragent` (+ `rat`):** set `OPENROUTER_API_BASE` to the proxy (verified fact 3). Confirm with `LLM_DEBUG=1`, which makes `rat/libkit/llm.py:44` print the effective `base_url`.
- [ ] **`sweagent_repo2run`:** litellm honours `OPENAI_BASE_URL`/`api_base`. Set it in the subprocess env in `producers/sweagent_repo2run.py::run_sweagent_repo2run`.
- [ ] **`sweagent`:** same, in `runner/live/sweagent.py`'s subprocess env.
- [ ] **`executionagent`:** EA's `LitellmModel` reads litellm's standard env conventions. Set the base URL in the subprocess env built in `producers/executionagent.py::run_executionagent`. **This is the phase that fills EA's blank, and it requires no EA patch — the whole point of the proxy.** Verify by asserting a non-empty usage ledger after one EA smoke repo.
- [ ] **`repo2run` — OUT of scope (Q6), no verification needed.** `producers/repo2run.py` shells out to `{root_path}/Repo2Run/build_agent/main.py`, which is not vendored here. Rather than read it, we exclude the arm. **Do the exclusion actively, not by omission:** set `usage_source: null` on `repo2run` packets and record the exclusion in the run manifest, so a reader of the numbers sees that this arm's cost is not comparable rather than inferring it from an empty ledger. Its RAT-side calls remain metered; only the Repo2Run subprocess escapes.
- [ ] **`rat/libkit/tools/llm.py` — REPAIR, DO NOT DELETE.** Per verified fact 4, deleting it breaks `read-file` and `edit-file` in-container across `rat` and all five `dockeragent` varieties, via three unguarded module-level imports. The fix is to replace the hardcoded config dict with an env read (`OPENROUTER_API_BASE` / `OPENAI_BASE_URL` + key), keeping the current values as fallback so nothing that works today stops working. One function body; every import keeps resolving. This also closes the latent bypass, because the tool then follows the proxy like everything else.
- [ ] **Test for the repair:** import each of `read_file.py`, `edit_file.py`, `edit_file_copy.py` as modules and assert the import succeeds with no network and no key set — a direct regression test against the deletion that was nearly shipped.
- [ ] **Tests:** for each arm, assert the subprocess env dict contains the proxy URL — a pure test on the env-construction, no live run.
- [ ] **How we know this worked — TOTAL escape and PARTIAL escape are different problems.** Per arm, one smoke repo, then:
  - **Total escape:** the usage ledger is non-empty. Covered by Phase 2.6's `{}`-not-zeros rule.
  - **Partial escape:** the ledger is non-empty and plausible but missing a subset of traffic. The self-report cross-check (`input_tokens` within a few percent of the arm's own figure) only works for arms that *have* a self-report — and **`executionagent` has none, which is the entire reason it is in scope.** Worse, EA constructs **two** litellm clients, `model` and `knowledge_model` (`main.py:855`/`:859`). Redirect one and not the other and you get a non-empty, entirely believable ledger missing every knowledge-model call — including the forced-exit-cycle Dockerfile generation.
  - **Therefore add a self-report-independent cross-check:** compare the proxy's *request count* against the arm's own turn/cycle count, which every arm exposes without cost telemetry (EA via `producers/executionagent.py::_harvest_economy`'s cycle count; SWE-agent via trajectory length; `dockeragent` via its turn count). Warn when they diverge beyond a threshold. A ledger with 40 requests against 100 recorded cycles is escaping, and nothing else in this plan would catch it.
- [ ] **Rollback:** unset the env var per arm. Each arm is independently revertible; land them one at a time.

### Phase 2.3 — preserve the provider pin across redirection

Facts 2 and 7 are the same bug twice: two branches in `rat/libkit/llm.py` decide provider-specific behaviour by sniffing the **base URL**, which redirection changes by definition. An earlier draft proposed moving the pin into the proxy. **That fix was rejected by adversarial review as a measurement change wearing a telemetry hat** — the pin is built in the *agent* process from `os.getenv("OPENROUTER_PROVIDER", "Alibaba")` (`rat/libkit/llm.py:19-23`), so relocating it makes the served upstream depend on the *proxy's* environment. An operator who sets `OPENROUTER_PROVIDER` for the run but not for the proxy launch would silently get `Alibaba`, a different upstream at possibly different quantization, and moved EBSR/ESSR — violating this plan's first global constraint invisibly.

- [ ] **Fix the sniff, keep the pin agent-side.** Resolve the provider **once** in `rat/libkit/config.py::get_llm_config` and return it alongside `base_url`/`key`. Gate **both** `_is_openrouter` (fact 2) and the `minimaxi` thinking-mode branch (fact 7) on that resolved value instead of on `base_url`. The pin then keeps reading the agent's own env and keeps travelling in the request body, which the proxy forwards unmodified. This is genuinely telemetry-only: no behaviour depends on where the request is *sent*, only on what was *configured*.
- [ ] **Immunity, not a patch.** Keying on a resolved provider rather than a URL substring means any future redirection — a second proxy, a regional endpoint, a local mock — cannot silently disarm provider-specific behaviour. Patching only `_is_openrouter` would leave the MiniMax branch armed.
- [ ] **If the proxy is ever given the pin anyway** (do not do this without a reason): it must require `OPENROUTER_PROVIDER` explicitly and **fail loudly when unset — no `Alibaba` default**, and the phase test must assert the forwarded pin equals the value the *runner* resolved, not the proxy's fallback.
- [ ] Model pinning is a separate question from provider pinning. `rat/eval/sweagent/python-config.yaml` hardcodes `name: deepseek/deepseek-chat` while `varieties.toml` pins `openrouter/deepseek/deepseek-v4-flash`, and which wins has never been established. The proxy sees the model on every request and can record it regardless of who pins it.
- [ ] **Decided (Q4): record and warn, do not enforce.** The proxy writes the inbound `model` on every usage row and prints a warning when it differs from the model the runner resolved for that arm. It does **not** rewrite `request["model"]`. Rationale: enforcement would make the proxy silently override an operator's explicit `--llm`, which is surprising precisely when you are debugging one arm — and enforcement is a behaviour change, which this plan is not allowed to make. Revisit once a full run's ledger shows whether mismatches actually occur; the recorded field is what makes that decision possible.
- [ ] **Tests:** with `base_url` pointed at `127.0.0.1`, assert the OpenRouter provider pin is still built **and** the MiniMax thinking block is still built for their respective resolved providers — both must fail against the current `base_url`-sniffing code. Write them first and watch them fail.
- [ ] **How we know this worked:** ledger `provider` is constant across a smoke run and equals `$OPENROUTER_PROVIDER` as resolved by the runner — compare against a pre-redirection run, which must agree.
- [ ] **Rollback:** revert the `config.py` resolution; the two branches fall back to URL sniffing and the pin is dropped again under redirection (so roll back Phase 2.2 with it).

### Phase 2.4 — the two cost adapters

Pure functions over a usage row. No I/O, fully unit-testable.

- [ ] **`settled` (OpenRouter):** proxy injects `usage: {include: true}` (Stage 1's logic, now centralised); read `usage.cost`; `usage_source: "settled"`.
- [ ] **`computed` (DeepSeek-direct):** price from the captured inputs —
  - peak window from the row's `ts`: 01:00–04:00 and 06:00–10:00 **UTC, Mon–Fri** = peak; everything else off-peak at half rate;
  - cache split from **`prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`** (verified fact 8 — **not** `prompt_tokens_details.cached_tokens`);
  - `usage_source: "computed"`.
- [ ] Normalise both provider shapes into the one usage-row schema at ingest, so the cost adapters never branch on provider again.
- [ ] **Tests** (`tools/tests/test_metering_cost.py`), the highest-value tests in the plan:
  - a DeepSeek-shaped usage block yields non-zero `cache_read_input_tokens` — **this is the regression test for the porting bug**; write it first and watch it fail against a naive OpenAI-shaped reader;
  - an OpenAI-shaped block still reads `prompt_tokens_details.cached_tokens`;
  - peak/off-peak boundary cases: 00:59, 01:00, 04:00, 05:59, 06:00, 10:00 UTC, and a Saturday inside the peak window (must price off-peak);
  - a cache-hit-heavy row prices ~31× cheaper on input than an all-miss row of the same token count;
  - a missing `usage.cost` yields `None`, never `0.0`.
- [ ] **How we know this worked:** for one DeepSeek-direct smoke repo, hand-compute the cost from the ledger rows and match the DeepSeek console to the cent.
- [ ] **Rollback:** adapters are pure and only feed the reporting path; drop back to `usage_source: null`.

### Phase 2.5 — concurrency

- [ ] **Decision: one proxy per repo**, launched by the per-repo produce path and torn down with it, ledgers written into the repo's existing output dir.
- [ ] **Justification:** runs use `--concurrency 4`. A shared proxy needs `swe-maintain`'s `identity` plumbing (`task_id`/`arm` on every row) *plus* a way for concurrent agents to tag their own requests — and agents do not send a task id, so attribution would have to be inferred from connection or timing. That is a correctness risk for a telemetry feature. One proxy per repo makes attribution structural: the ledger's location *is* the attribution. Cost is one extra short-lived process per repo, which is noise next to a Docker build.
- [ ] **Tests:** two proxies on ephemeral ports simultaneously, each writing to its own ledger, with no cross-contamination.
- [ ] **How we know this worked:** a `--concurrency 4` smoke over 4 repos yields 4 ledgers, each non-empty, with no `full_name` appearing in the wrong one.
- [ ] **Rollback:** N/A — this is the initial design, not a migration.

### Phase 2.6 — ledger → economy, and failure modes

- [ ] Add a pure `ledger_economy(usage_path, audit_path) -> dict` returning the existing `economy` shape (`tokens_in`, `tokens_out`, `llm_calls`, `cost_usd`, `total_tokens`) plus `usage_source`. Port `swe-maintain`'s `model_proxy.ledger_usage` reconciliation: only `phase == "claimed"` audit rows are logical requests; subtract `phase == "failed"` rows, because an attempt the proxy answered with an upstream error is a client-side retry, not an agent step. This matters concretely — `runner/live/sweagent_runner.py` sets `max_requeries: 8` and those requeries are invisible today.
- [ ] Producers prefer ledger economy over their own harvested economy when a non-empty ledger exists; otherwise they keep today's behaviour untouched.
- [ ] **Failure modes — the ledger must never lie:**
  - **Proxy fails to start:** produce must fail loudly *before* the agent runs, not silently produce an unmetered run. **There is no general preflight hook to reuse** — `runner/cli.py`'s only one is `_ensure_claude_runner` (`cli.py:89`), gated on `_CLAUDE_LANES`, and Claude Code is out of scope. Add a general run-start preflight modelled on that function's shape (`cli.py:89-140`: check the prerequisite, print a `[bench] FATAL:` line naming the lane, exit non-zero). This check is the sole guard against a silently unmetered run, so it must not be skipped for want of something to reuse.
  - **Proxy dies mid-run:** the agent's requests start failing, which surfaces as an agent error — visible, not silent. `ledger_economy` on a truncated ledger returns what it has; a partial ledger is honest.
  - **Empty ledger with a successful produce:** this is the dangerous case — it means requests escaped the proxy. `ledger_economy` must return `{}` (falling back to the arm's own numbers with its own `usage_source`), and **never** an all-zero economy that reads as authoritative. `swe-maintain` hit exactly this: "on a path that produced no usage rows at all it published an all-zero token ledger as authoritative."
  - **Malformed JSONL tail** (proxy killed mid-write): skip unparseable lines, never raise. Precedent: `producers/executionagent.py::parse_trace` already tolerates a truncated tail line and is unit-tested for it.
- [ ] **Tests:** `ledger_economy` over — an empty ledger (`{}` not zeros); claimed-minus-failed request counting; a truncated final line; a ledger whose rows carry no `cost_usd` (yields `None`).
- [ ] **How we know this worked:** re-run one repo per arm and diff `_meta.json` against a pre-Stage-2 run. `build_ok`, `ebsr`, `essr` and every measured field must be identical; only economy fields and `usage_source` change.
- [ ] **Rollback:** stop passing the proxy env; producers fall back to their existing harvesters, which are never removed.

---

## Decisions

All resolved 2026-09-03. Recorded so the reasoning survives the decision.

| # | Question | Decision |
|---|---|---|
| **Q1** | Keep a table-derived cost as a labelled fallback, or emit `null`? | **Keep it, labelled `usage_source: "table"`.** A filterable 2×-wrong number beats a hole. Precedence: settled → table → null. |
| **Q2** | Proxy in `tools/` or a new top-level `metering/`? | **`tools/`** for the server. The pure ledger *reader* goes in `producers/` so nothing load-bearing imports an "optional" directory. |
| **Q3** | `rat/libkit/tools/llm.py` — delete or repair? | **Repair** — env read replacing the hardcoded dict. Closed by adversarial review: deletion breaks `read-file`/`edit-file` across six varieties (verified fact 4). |
| **Q4** | Should the proxy *enforce* the model or *record* mismatches? | **Record and warn.** Enforcement would override an operator's explicit `--llm` exactly when they are debugging, and is a behaviour change this plan may not make. |
| **Q5** | Does `usage_source` need to reach `metrics.json`? | **No — `_meta.json` only.** Add it downstream when a report groups by it. |
| **Q6** | Is `repo2run` in Stage 2 scope? | **No.** Excluded without verifying its entrypoint. Exclusion must be recorded actively in the run manifest, not left to be inferred from an empty ledger. |
| **Q7** | Does Stage 2 subsume Stage 1's per-agent injection? | **Yes.** The three call sites are removed when the proxy lands. Two paths writing one field is how they drift. |

---

## Before writing any Stage 1 code

- [ ] **Verify `usage.cost` survives the OpenAI SDK.** `rat/libkit/llm.py:71` returns `response.usage` straight to callers, and `cost` is an OpenRouter extra field on a typed `CompletionUsage`. If the SDK strips it, the entire `dockeragent` branch of Stage 1 silently yields nulls and the phase is wasted. **One live OpenRouter call with `extra_body={"usage": {"include": True}}`, then inspect `response.usage`.** Do this first; everything in Phase 1.2 depends on the answer.
- [ ] **Note the silent-degradation risk it sits on:** `rat/libkit/llm.py:72-75` catches every exception, retries 5×, then returns `(None, None)`. If usage accounting ever reaches a strict endpoint, the resulting 400 is invisible — the agent simply gets no completion. That makes `wants_accounting`'s sniff more safety-critical than a normal feature flag, and is why it must never send unconditionally.

---

## Sequencing

Verify the SDK question above **before** any Stage 1 code.

Stage 1 ships value standing alone, but per Q7 its per-site injection is temporary — if both stages are going out together, skip Stage 1's Phase 1.2 and inject once at the proxy.

Stage 2 is independently revertible per arm, so land the arms one at a time, `executionagent` **first**: it is the arm with nothing to lose and the clearest proof the proxy works, since any non-empty ledger is strictly better than today's blank. Then `dockeragent` (the largest arm, and the one whose provider-pin fix in Phase 2.3 is the riskiest change), then the two sweagent arms. `repo2run` is excluded (Q6) and `claudecode` is out of scope entirely.
