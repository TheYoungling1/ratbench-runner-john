# Handoff — `claudecode-dockerfile` on rat_python50_new

Target: reproduce the `runs/claudecode-dockerfile/envbench100-merged-20260915` configuration on
`datasets/rat_python50_new.json` (50 repos), pinned to **`deepseek/deepseek-v4-flash-0731`** so the
arm runs the same weights as `sweagent_repo2run_modern` and `setupx`.

Written 2026-09-18. Every value below was read from the repo or the VM, not recalled; anything I
could **not** verify is marked UNVERIFIED.

---

## 1. The model question, and why the route is what it is

This is the only arm where pinning the model is awkward, so it is worth stating once.

The Claude Code CLI speaks **only the Anthropic wire format**. It cannot talk to
`/v1/chat/completions`. That constrains the options:

| route | model actually served | thinking off? | verdict |
|---|---|---|---|
| DeepSeek direct (`api.deepseek.com/anthropic`) | **V4.1-Flash** — the bare `deepseek-v4-flash` alias is retired | yes (`MAX_THINKING_TOKENS=0` honoured) | **wrong model** — silently differs from the other arms |
| OpenRouter Anthropic skin (`openrouter.ai/api`) | `deepseek/deepseek-v4-flash-0731` — the dated slug exists only here | **no** — the skin ignores `thinking: {type: disabled}` | **right model, thinking stays on** |

Both were measured, not assumed: the skin emitted 30 thinking blocks with `thinking:disabled` on
the wire, while DeepSeek's own bridge honoured it (237 → 164 output tokens on one probe).

**The chosen route is OpenRouter**, because a model mismatch is a worse confound than a reasoning
mismatch. `sweagent_repo2run` pins `thinking: {type: disabled}` on the grounds that the paper's
baselines ran on a non-reasoning model; ccdf **cannot** match that over the skin. Record it as a
known asymmetry rather than claiming parity.

`temperature` is a second, smaller asymmetry: sweagent pins 0.2, and Claude Code sends no
temperature field at all, so the endpoint default applies. Nothing short of rewriting the request
body changes that.

---

## 2. Settings

From `varieties.toml` `[variety.claudecode-dockerfile]` and the source run manifest
`runs/claudecode-dockerfile/envbench70-20260908-203504/_run_manifest.json`:

| setting | value | source |
|---|---|---|
| `model` (variety) | `claudecode-dockerfile` | varieties.toml |
| `llm` | `deepseek/deepseek-v4-flash-0731` | varieties.toml |
| `num_turn` | `100` | varieties.toml — per-instance LLM-call cap, matches sweagent |
| `measure` | `conforming` | varieties.toml — agent homes the repo at `/testbed` itself |
| concurrency | `4` | envbench70 run manifest |
| agent/harness commit | `d7d7fb3` | envbench70 run manifest |
| CLI version | `2.1.268 (Claude Code)` | installed on the VM |

The `llm` slug reaches the CLI through `_normalize_model()` in
`producers/_claudecode_helpers.py`: a provider-qualified slug passes through unchanged (so the skin
selects it by body slug), while a leading `openrouter/` prefix — litellm's routing prefix, which
the sweagent arms use — is stripped. **Do not add an `openrouter/` prefix here**; unlike the
sweagent arms, this lane is not going through litellm.

---

## 3. Environment

`agent_env()` forwards exactly these keys into the workbench container
(`AUTH_KEYS + ANTHROPIC_BASE_URL`):

```bash
ANTHROPIC_BASE_URL=https://openrouter.ai/api      # note: NO /v1 — this is the Anthropic skin
ANTHROPIC_AUTH_TOKEN=<openrouter key>             # Bearer; NOT ANTHROPIC_API_KEY
```

Three behaviours in `_claudecode_helpers.py` worth knowing before debugging anything:

- **`ANTHROPIC_API_KEY` is auto-migrated.** On an OpenRouter base URL, `agent_env()` moves it into
  `ANTHROPIC_AUTH_TOKEN`, because the CLI sends `ANTHROPIC_API_KEY` as `x-api-key`, which OpenRouter
  ignores — and which is an *Anthropic* credential shape, so leaving it set risks authenticating
  against Anthropic instead. Setting `ANTHROPIC_AUTH_TOKEN` directly is clearer.
- **`CLAUDE_CODE_OAUTH_TOKEN` is dropped** for any non-Anthropic base URL. A subscription bearer
  must never be forwarded to a third party, and if the CLI preferred it the run would silently
  stay on Anthropic. Dropping it makes `has_auth` fail loudly instead.
- **Thinking**: `inference_env()` emits `MAX_THINKING_TOKENS=0` unless `CLAUDE_THINKING=enabled`.
  Leave it unset. It is the only lever the CLI exposes (`--effort low|medium|high|max` all put the
  same `thinking: {"type": "adaptive"}` on the wire). Over the OpenRouter skin it will be ignored —
  send it anyway so the intent is on the wire and in the logs.

---

## 4. Provider pinning — the one genuinely unresolved item

The other arms pin DeepInfra with `provider: {order: ["DeepInfra"], allow_fallbacks: false}` in the
request body. **The Claude Code CLI cannot send that field.** So the model is pinned and the
provider is whoever OpenRouter routes to, unless it is carried server-side by one of:

1. an **OpenRouter preset** referenced from the slug —
   `deepseek/deepseek-v4-flash-0731@preset/<name>` with `order=["DeepInfra"]`,
   `allow_fallbacks=false`; or
2. the account-level **allowed-providers** setting.

**UNVERIFIED: I did not confirm that such a preset exists on the account.** If it does not, this
arm's provider differs from the sweagent/setupx arms even though the model matches. Decide
deliberately and record which option was used — different providers can serve the same weights at
different quantisations.

---

## 5. Prerequisites — current gaps on `167.233.64.96:/opt/ratbench-cc`

Verified 2026-09-18. The checkout is at `d7d7fb3` with `producers/_claudecode_helpers.py` and
`varieties.toml` modified (the smoke-test patches, still in place).

| item | state | action |
|---|---|---|
| `_claudecode_helpers.py` | sha256 `ae854c27…` — **byte-identical to local** | none; OpenRouter support is present |
| `varieties.toml` ccdf `llm` | already `deepseek/deepseek-v4-flash-0731` | none |
| `datasets/rat_python50_new.json` | **ABSENT** | copy it over |
| `.env` | has `DEEPSEEK_API_KEY`, `OPENROUTER_API_BASE`, `OPENROUTER_API_KEY`, `SETUPX_ROOT` | **add `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`** |
| `bench/bench/measure.py:165` | still `bash -lc` | apply the `bash -c` fix before measuring — see §7 |
| `bench/bench/contract.py:61` | still `bash -lc` | same |
| `bench/bench/docker_client.py` | no `errors="replace"` | apply the decode fix — see §7 |
| disk | 76 GB free (74% used), 14 containers up, 50 GB images | reclaim before a 50-repo run |

> **Rotate the OpenRouter key.** It was exposed in an editor selection during the 2026-09-17
> session and is also used by SetupX embeddings.

---

## 6. Running it

```bash
cd /opt/ratbench-cc
# .env must contain ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (see §3)
./run_bench.sh claudecode-dockerfile \
  --repos-json datasets/rat_python50_new.json \
  --tier all \
  --concurrency 4
```

`run_bench.sh` sets the `*_ROOT` vars to the repo, sources `.env`, and execs
`python -m runner.cli` through the repo venv. Do not invoke `runner.cli` directly — the "system
python has no dotenv" gotcha is exactly what the wrapper exists to prevent.

**Smoke first.** One small repo before committing 50:

```bash
./run_bench.sh claudecode-dockerfile --only <owner/repo> --limit 1
```

Then confirm on the wire, before trusting a full run:

- the response `model` field says `deepseek/deepseek-v4-flash-0731` (not `V4.1-Flash`, not a
  Claude id) — this is the check that catches a silent fallback to the retired alias;
- `_meta.json` records non-null `tokens_in`/`tokens_out`;
- cost is tagged `provider` (from OpenRouter's `usage.cost`) or `computed` (DeepInfra card), never
  `cli`. `settled_cost()` prefers `info["provider_cost"]`; a `cli` tag means Claude Code's own
  Anthropic-priced estimate leaked in and the number is wrong.

---

## 7. Scoring

The measure stage must run on a harness with **both** fixes, or the numbers are not comparable with
the other arms:

- **`bash -lc` → `bash -c`** (`measure.py` `_sh`, `contract.py` `probe_testbed`). A login shell
  sources Debian's `/etc/profile`, which overwrites `PATH` and discards the image's `ENV PATH`.
  ccdf's own exposure is low — **1 of 100** Dockerfiles in the envbench run use `ENV PATH`, versus
  100% for setupx and Graph2Env — so this barely moves ccdf's score. It is still mandatory: the
  table must be measured under one harness, and mixing `-lc` and `-c` numbers makes every cross-arm
  delta a harness artifact.
- **`docker_client.py` strict UTF-8 decode.** `text=True` without `errors=` raises
  `UnicodeDecodeError` on any invalid byte in docker output (BuildKit truncating a multi-byte
  character), which surfaces as a fabricated `measure_error`. **This one matters here**: rat50new
  contains several repos with non-ASCII build output, and it took out
  `insistence/RuoYi-Vue3-FastAPI` on 2026-09-17.

```bash
cd bench
python -m bench.unified_bench \
  --harvest claudecode-dockerfile=../runs/claudecode-dockerfile/<run>/output \
  --out ../runs/claudecode-dockerfile/<run>/measure \
  --gold ../datasets/rat50new_gold.json \
  --turn-cap 100 --concurrency 2
```

Gold is `rat50new_gold.json` — 44 CERTIFIED of 50, zero SHA mismatches against the dataset pins.
The 6 without a denominator (`NVIDIA-NeMo/RL`, `sgl-project/sglang-jax`, `modelscope/AgentJet`,
`BerriAI/litellm`, `meta-pytorch/torchrec`, `TencentBlueKing/bk-lite`) leave the ESSR mean; they are
still scored for EBSR.

`campaign.py` does **not** know `rat_python50_new.json`, so `--agent-dir` will silently ignore this
run. Use `--harvest`, or add the dataset to `DATASET_GROUPS`, `GROUP_GOLD_CORPUS` and
`GROUP_DATASET`.

---

## 8. Open items

1. **Provider pin unconfirmed** (§4) — the one thing that could still make this arm differ from
   sweagent/setupx despite the matching model slug.
2. **Thinking stays on** over the OpenRouter skin, unlike every other arm. Disclose it.
3. **`_DEEPSEEK_RATES` in `_claudecode_helpers.py` is stale** (~1.4× overstated vs DeepSeek's
   current card). Only affects the DeepSeek-direct path, but it will misprice anything that falls
   back to it.
4. **The VM checkout carries uncommitted smoke-test patches.** `producers/_claudecode_helpers.py`
   matches local exactly, so this is safe today, but it is not reproducible from git — commit the
   helpers change before publishing numbers from it.
