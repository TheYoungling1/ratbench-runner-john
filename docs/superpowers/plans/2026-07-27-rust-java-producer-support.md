# Rust + Java Producer Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Teach the `claudecode-dockerfile` producer to set up Rust and Java repositories, so `datasets/rat_rust50.json` and `datasets/rat_java50.json` can be run end-to-end instead of silently receiving the Python prompt.

**Architecture:** The producer already selects a per-language `LangProfile` (prompt + emitted base image). Two things are missing. First, there are no Rust/Java profiles, so `get_profile()` falls back to Python. Second, the *workbench* container the agent works inside (`claude-runner:latest` = `python:3.11-slim` + Node) has no cargo and no JDK, so even a correct prompt would leave the agent unable to verify anything. We add a `workbench` field to `LangProfile`, build one workbench image per language, and make the run-start preflight build the workbenches the dataset actually needs.

**Tech Stack:** Python 3.11 (stdlib only in `producers/`), Docker, pytest.

## Global Constraints

- **`producers/_claudecode_helpers.py` must stay stdlib-only.** It is imported on a path that must not touch the vendored RAT tree.
- **The Python prompt is frozen.** A golden-string test pins it to sha256 `4edebdd3…5b1ade`, 1304 chars, so the completed python50 run stays comparable. Do not edit `_PYTHON_PROMPT`, `PYTHON_PROFILE.default_base`, or `PYTHON_PROFILE.test_runner`.
- **Prompt templates are `str.format()` templates.** Format keys are exactly `{gen_path}`, `{base}`, `{full_name}`. Any *literal* brace in prompt text must be doubled (`{{`/`}}`) or `build_prompt` raises `KeyError`. `_NODE_PROMPT` does this for `{{}}` in its JavaScript snippet.
- **Both new languages set `test_runner=None`** — the measure side installs its own runner (`rust.py ensure_cmd` curls a prebuilt cargo-nextest; Surefire/Gradle emit JUnit natively).
- **Run the three test dirs together:** `python3 -m pytest bench/tests/bench producers/tests runner/tests -q`. Only `producers/tests/conftest.py` puts `<repo>/bench` on `sys.path`; `pytest bench/tests/bench` alone fails collection on every module.
- **Baseline suite state:** 355 passed, 6 skipped, plus 1 pre-existing failure `bench/tests/bench/test_e2e_smoke.py::test_itsdangerous_measures_green` (a live Docker build broken on this Mac, failing identically on the base commit). Deselect it: `--deselect bench/tests/bench/test_e2e_smoke.py::test_itsdangerous_measures_green`.

### Verified environment facts (do not re-derive; these were measured, not assumed)

| Fact | Value | Why it matters |
|---|---|---|
| `rust:1` login-shell `PATH` | `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` | cargo is **not** on it — `bash -l` resets `PATH` via `/etc/profile` |
| `rust:1` cargo location | `/usr/local/cargo/bin/cargo`, a **symlink to `rustup`** | matches `rust.py:7`'s hardcoded prepend; the shim resolves `rust-toolchain.toml` |
| `rust:1` env | `CARGO_HOME=/usr/local/cargo`, `RUSTUP_HOME=/usr/local/rustup`, both world-writable (`drwxrwxrwx`) | non-root `agent` can fetch crates and install a pinned toolchain with no chmod needed |
| `rust:1` has `curl` | `/usr/bin/curl` | `rust.py ensure_cmd` curls cargo-nextest at measure time |
| `maven:3-eclipse-temurin-17` | `JAVA_HOME=/opt/java/openjdk` | **exactly** `java.py:11`'s hardcoded default |
| same image | `mvn` at `/usr/bin/mvn`, on the login `PATH`; JDK 17.0.19; Maven 3.9.16 | the gate's Maven branch works with no `PATH` fixing |
| same image | **no `gradle` binary** | at-risk: `google/tsunami-security-scanner`, the only 1 of 13 Gradle repos in `rat_java50.json` without a `./gradlew` wrapper. See the RAT-parity section — apt's gradle is 4.4.1 (2017) and would not help |
| `eclipse-temurin:17` + `apt-get install maven gradle` (RAT's exact recipe) | Maven 3.9.12, **Gradle 4.4.1** | reproducing RAT's image gives a Gradle from 2017 — nominal parity, no real capability |
| `docker exec` shape | `docker exec <name> bash -lc "<cmd>"` (`measure.py:141`, `docker_client.py:33`) | image `ENV` is inherited, but `bash -l` **overwrites `PATH`** and leaves other vars alone |
| gate is time-bounded | `docker_client.exec` takes a `timeout` and returns **rc 124** on expiry (`docker_client.py:36`); the scheduler adds a per-child `hard_wall` (`benchmark.py:726`) | this — NOT any Dockerfile inspection — is the real reason to bake the compile. Nothing in the harness reads the emitted Dockerfile for a compile step, so a prompt claiming the grader *requires* one is lying to the agent |

### Parity with the RunAnyThing (RAT) reference environment

The base images were cross-checked against RAT's own, read from
`libkit/dockerfile_templates/ALLOWED_VERSIONS.json` and the `rust.template` / `java.template` files
in the RunAnyThing repo. RAT selects a version **per repo** from an allowlist
(`version_selector.py`, from project analysis or an LLM answer), falling back to a default.

| | RAT | This plan | Verdict |
|---|---|---|---|
| Rust image | `rust`, variant `""` (full, not slim) | `rust:1` (full) | **same image + variant** |
| Rust version | default `1.75`, allowlist `1.70`–`1.85` | `1` → currently `1.97.1` | **diverges, deliberately** |
| Java image | `eclipse-temurin`, variant `""` | `maven:3-eclipse-temurin-17` | same JDK vendor, `JAVA_HOME=/opt/java/openjdk` on both |
| Java version | default `17`, allowlist `8`–`25` | `17` | **same default** |
| Java build tools | `apt-get install -y maven gradle` | Maven 3.9.16 only | **gap — closed by the prompt clause in Task 3** |

Deliberate divergences, with reasons:

- **Rust version.** RAT's allowlist stops at `1.85` and defaults to `1.75` (a 2023 toolchain). Our
  datasets pin repos to their 2026 HEAD, and edition-2024 crates need ≥1.85, so RAT's default
  cannot compile a meaningful slice of this corpus. Keep the newer base. Note that 26/50 Rust
  repos pin their own toolchain anyway, so the base version only decides the other 24.
- **Java build tools.** RAT's image always has *a* gradle. Reproducing its exact recipe
  (`eclipse-temurin:17` + `apt-get install -y maven gradle`) was measured and yields Maven 3.9.12
  and **Gradle 4.4.1** — a 2017 release that cannot build any modern Gradle project. So apt-gradle
  buys nominal parity and almost no capability: it would still not rescue
  `google/tsunami-security-scanner`. Task 3 therefore keeps the Maven base (37 of 50 repos are
  Maven) and adds a prompt clause telling the agent to install gradle itself when a Gradle repo
  ships no wrapper, which is strictly more capable than RAT's stale system gradle.
- **Not copied from RAT's templates:** the aliyun pip mirror and the `rsproxy.cn` cargo mirror
  (China-region mirrors; this VM is in Europe, so they would be slower and an availability risk),
  and `python3`/`pip`/`openai` (RAT's agent runs *inside* the container and calls the LLM from
  there; ours runs the Claude Code CLI, which needs Node instead).

**The one architectural difference worth revisiting later:** RAT picks the language version
per repo; this plan pins one base per language. For Java that matters most — an allowlist of 8–25
lets RAT put an old Spring project on JDK 8 and Quarkus on 21, whereas here the only escape is the
`ENV JAVA_HOME` clause, which makes the agent install a JDK itself. If a full Java run shows
systematic version failures, a `version_selector` equivalent is the principled fix, not a
different single pin.

**Reproducibility note:** `rust:1` floats — a rebuild months apart silently changes the compiler.
`rust:1.97` exists and pins it (as does RAT's practice of pinning a minor version). This is the
same class of hole as the unpinned `@anthropic-ai/claude-code` install in the workbench images.
Pin both deliberately before any scored run whose value is cross-run comparability.

### The PATH/ENV asymmetry

The `docker exec` row above produces the single most important asymmetry in this plan:

- **Java is flexible.** `java.py:11` is `export PATH="${JAVA_HOME:-/opt/java/openjdk}/bin:$PATH"`. `/etc/profile` resets `PATH` but not `JAVA_HOME`, so a Dockerfile `ENV JAVA_HOME=…` **is honored by the grader**. This is the escape hatch for repos needing a JDK other than 17.
- **Rust is rigid.** `rust.py:7` is `export PATH=/usr/local/cargo/bin:$PATH` with no env indirection. An agent that installs rustup elsewhere and sets `ENV PATH` will have that `PATH` **discarded**, and the gate will not find cargo.

Both prompts must state their own rule. Getting either backwards yields a gate failure that reads as agent error.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `producers/_claudecode_helpers.py` | `LangProfile`, prompts, `_PROFILES`, `get_profile`, `resolve_base` | add `workbench` field; add `_RUST_PROMPT`, `_JAVA_PROMPT`, `RUST_PROFILE`, `JAVA_PROFILE`; register both |
| `producers/claudecode_dockerfile.py` | live produce path | read the workbench from the profile instead of a hardcoded tag (line 131) |
| `runner/cli.py` | run-start preflight | build the workbench(es) the dataset needs, not just `claude-runner:latest` |
| `docker/claude-runner-rust.Dockerfile` | Rust workbench image | **new** |
| `docker/claude-runner-java.Dockerfile` | Java workbench image | **new** |
| `producers/tests/test_language_profiles.py` | profile/prompt/parity tests | shrink `_NO_PROFILE_YET`; add Rust/Java prompt tests |
| `runner/tests/test_claude_runner_image.py` | preflight tests | add language-aware preflight tests |

**Workbench tag → Dockerfile convention:** the preflight derives the recipe path from the tag by stripping the `:tag` suffix and appending `.Dockerfile` under `docker/`. `claude-runner:latest` → `docker/claude-runner.Dockerfile`; `claude-runner-rust:latest` → `docker/claude-runner-rust.Dockerfile`. A missing file makes `docker build` fail, which the preflight already converts into a loud `SystemExit`.

---

### Task 1: Make the workbench image profile-driven (no behaviour change)

Today `producers/claudecode_dockerfile.py:131` hardcodes `claude-runner:latest` for every language, with a comment claiming "The workbench ships python3 AND node 20, so it hosts either language's setup." That claim is true for Python and Node and false for Rust and Java. This task moves the choice onto the profile while keeping Python and Node on exactly the same image, so it is provably inert.

**Files:**
- Modify: `producers/_claudecode_helpers.py:57-66` (add field), `:150-172` (both profiles)
- Modify: `producers/claudecode_dockerfile.py:127-131`
- Test: `producers/tests/test_language_profiles.py`

**Interfaces:**
- Produces: `LangProfile.workbench: str` — the Docker tag of the container the agent works inside. Consumed by Task 4's preflight and by the producer.

- [ ] **Step 1: Write the failing tests**

Append to `producers/tests/test_language_profiles.py`:

```python
# ── workbench image ─────────────────────────────────────────────────────────────────────────

def test_python_and_node_share_the_original_workbench():
    # Task 1 is a pure refactor: whatever these two resolved to before must be byte-identical
    # after, or the completed python50 / node-full50 runs stop being comparable.
    assert PYTHON_PROFILE.workbench == "claude-runner:latest"
    assert NODE_PROFILE.workbench == "claude-runner:latest"


def test_workbench_env_override_wins_for_every_profile():
    # CLAUDE_RUNNER_IMAGE is a GLOBAL pin, mirroring CLAUDE_DOCKERFILE_BASE.
    from producers._claudecode_helpers import resolve_workbench
    for profile in (PYTHON_PROFILE, NODE_PROFILE):
        assert resolve_workbench(profile, "custom:tag") == "custom:tag"


def test_unset_or_empty_workbench_env_falls_to_the_profile():
    from producers._claudecode_helpers import resolve_workbench
    for env in (None, ""):
        assert resolve_workbench(PYTHON_PROFILE, env) == "claude-runner:latest"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_language_profiles.py -q -k workbench`
Expected: FAIL — `AttributeError: 'LangProfile' object has no attribute 'workbench'` and `ImportError: cannot import name 'resolve_workbench'`.

- [ ] **Step 3: Add the field and resolver**

In `producers/_claudecode_helpers.py`, add to the `LangProfile` dataclass after `prompt_template` (keep `test_runner` last, since it has a default):

```python
    # The Docker image the AGENT works inside. Distinct from default_base, which is the FROM the
    # agent writes into the emitted Dockerfile and is the only one ever measured. Python and Node
    # share one workbench because it ships both toolchains; Rust and Java each need their own,
    # since an agent with no cargo/JDK cannot verify its own setup.
    workbench: str = "claude-runner:latest"
```

Add next to `resolve_base`:

```python
def resolve_workbench(profile: LangProfile, env_image) -> str:
    """The image the agent works inside: CLAUDE_RUNNER_IMAGE if set, else the language's own.

    `or`, not a dict default: an exported-but-empty var must fall through to the profile rather
    than asking Docker to run the image named "" (same rule as resolve_base)."""
    return env_image or profile.workbench
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest producers/tests/test_language_profiles.py -q -k workbench`
Expected: PASS (3 passed).

- [ ] **Step 5: Point the producer at the profile**

In `producers/claudecode_dockerfile.py`, replace lines 127-131 (the `# FIX 3:` comment block and the `base_image = …` line) with:

```python
    # FIX 3: the CONTAINER image must be a claude-runner workbench (has the `agent` user + claude
    # CLI), mirroring the deleted wrapper. The generated Dockerfile's FROM is a SEPARATE thing
    # (dockerfile_base, above) and stays a vanilla language base — only the container image was
    # wrong. The workbench is per-language: the original ships python3 AND node 20, but an agent
    # asked to set up a Rust or Java repo inside it has no cargo and no JDK, so it cannot run the
    # gate it is being scored on. CLAUDE_RUNNER_IMAGE remains a global override.
    base_image = resolve_workbench(profile, os.environ.get("CLAUDE_RUNNER_IMAGE"))
```

Add `resolve_workbench` to the import list at line 113-116:

```python
    from producers._claudecode_helpers import (
        W, AUTH_KEYS, _normalize_model, build_prompt, get_profile, resolve_base,
        resolve_workbench, DOCKERFILE_GEN_PATH,
    )
```

- [ ] **Step 6: Run the full suite**

Run: `python3 -m pytest bench/tests/bench producers/tests runner/tests -q --deselect bench/tests/bench/test_e2e_smoke.py::test_itsdangerous_measures_green`
Expected: 358 passed, 6 skipped (355 baseline + 3 new).

- [ ] **Step 7: Commit**

```bash
git add producers/_claudecode_helpers.py producers/claudecode_dockerfile.py producers/tests/test_language_profiles.py
git commit -m "refactor(producers): move the workbench image onto LangProfile

No behaviour change: python and nodejs both keep claude-runner:latest.
This is the seam Rust and Java need, since the shared workbench is
python:3.11-slim + Node and has no cargo or JDK."
```

---

### Task 2: Rust workbench image + RUST_PROFILE

**Files:**
- Create: `docker/claude-runner-rust.Dockerfile`
- Modify: `producers/_claudecode_helpers.py` (add `_RUST_PROMPT`, `RUST_PROFILE`, register in `_PROFILES`)
- Test: `producers/tests/test_language_profiles.py`

**Interfaces:**
- Consumes: `LangProfile.workbench` (Task 1), `resolve_base`, `build_prompt`.
- Produces: `RUST_PROFILE` with `key="rust"`, `default_base="rust:1"`, `workbench="claude-runner-rust:latest"`, `test_runner=None`.

- [ ] **Step 1: Write the failing tests**

Append to `producers/tests/test_language_profiles.py`:

```python
# ── rust ────────────────────────────────────────────────────────────────────────────────────

def _rust_prompt(full_name="o/r"):
    from producers._claudecode_helpers import RUST_PROFILE
    return build_prompt(full_name, resolve_base(RUST_PROFILE, None), RUST_PROFILE)


def test_rust_profile_shape():
    from producers._claudecode_helpers import RUST_PROFILE
    assert RUST_PROFILE.key == "rust"
    assert RUST_PROFILE.default_base == "rust:1"
    assert RUST_PROFILE.workbench == "claude-runner-rust:latest"
    # None, NOT a cargo-nextest install: RustLanguage.ensure_cmd curls a prebuilt nextest at
    # measure time, so an install line in the emitted Dockerfile would be dead weight.
    assert RUST_PROFILE.test_runner is None


def test_rust_prompt_names_the_gate_and_its_consequence():
    # RustLanguage.short_circuit_gate is True: if the test target does not compile, nextest never
    # runs and the repo scores zero. The agent must know which command it is being scored on.
    prompt = _rust_prompt()
    assert "cargo test --no-run" in prompt
    assert "ZERO" in prompt
    assert "cargo nextest run" in prompt          # what runs after the gate passes


def test_rust_prompt_pins_the_cargo_location():
    # rust.py hardcodes `export PATH=/usr/local/cargo/bin:$PATH` with no env indirection, and the
    # grader's `bash -l` resets PATH from /etc/profile — so an agent that relocates CARGO_HOME and
    # sets ENV PATH gets that PATH silently discarded and the gate cannot find cargo.
    prompt = _rust_prompt()
    assert "/usr/local/cargo/bin" in prompt
    assert "ENV PATH" in prompt                   # names the trap explicitly


def test_rust_prompt_asks_for_the_compile_and_gives_the_real_reason():
    # The gate re-compiles from scratch in a fresh container. Nothing in the harness INSPECTS the
    # Dockerfile for a compile step, so the prompt must not claim the grader requires one — the
    # actual reason is the exec timeout (docker_client.exec returns rc 124 on expiry, and the
    # scheduler applies a per-child hard wall). Overstating it as a grader rule teaches the agent
    # a false model of the contract, which is how prompts start lying about everything else.
    prompt = _rust_prompt()
    assert "RUN cargo test --no-run" in prompt
    assert "time limit" in prompt
    assert "COMPILES the tests without executing" in prompt
    assert "REQUIRED work your Dockerfile must do" not in prompt


def test_rust_prompt_warns_about_a_missing_root_manifest():
    # `cargo test --no-run` runs at the ROOT. A repo whose crates all live in subdirectories with
    # no root Cargo.toml cannot be gated at all — and that is a property of the repo, not something
    # the agent caused by moving files.
    prompt = _rust_prompt()
    assert "no Cargo.toml at the" in prompt


def test_rust_prompt_states_the_one_turn_constraint():
    prompt = _rust_prompt()
    assert "EXACTLY ONE TURN" in prompt
    assert "FOREGROUND" in prompt


def test_rust_prompt_never_mentions_python_or_node_tooling():
    prompt = _rust_prompt()
    for token in ("pip", "pytest", "npm ci", "package.json"):
        assert token not in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_language_profiles.py -q -k rust`
Expected: FAIL — `ImportError: cannot import name 'RUST_PROFILE'`.

- [ ] **Step 3: Write the Rust workbench Dockerfile**

Create `docker/claude-runner-rust.Dockerfile`:

```dockerfile
# docker/claude-runner-rust.Dockerfile
# The WORKBENCH image for Rust repos in the `claudecode-dockerfile` lane. This is where the agent
# explores and compiles live; it is NOT the base of the Dockerfile the agent emits (that is
# RUST_PROFILE.default_base, also rust:1) and it is never measured.
#
# rust:1 (bookworm), not -slim or alpine: RustLanguage.ensure_cmd curls a prebuilt cargo-nextest,
# and rust.py hardcodes PATH=/usr/local/cargo/bin, which is the full image's layout.
FROM rust:1

# System basics the agent commonly needs, plus Node for the CLI. pkg-config/libssl-dev/cmake are
# the three that unblock the majority of -sys crates (openssl-sys, ring, prost/protobuf builds);
# without them the agent burns turns rediscovering them on almost every non-trivial crate.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git build-essential ca-certificates sudo pkg-config libssl-dev cmake \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# Non-root user: Claude Code refuses --permission-mode bypassPermissions as root.
RUN useradd -m -u 1000 -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# NOTE: no chmod on CARGO_HOME/RUSTUP_HOME is needed — the official rust image already makes
# /usr/local/cargo and /usr/local/rustup world-writable (verified: drwxrwxrwx), so the
# unprivileged `agent` user can fetch crates and let rustup install a pinned toolchain.
# The image default user intentionally stays root; the producer selects `agent` per-exec.
WORKDIR /testbed
```

- [ ] **Step 4: Write the Rust prompt and profile**

In `producers/_claudecode_helpers.py`, add after `_NODE_PROMPT`:

```python
_RUST_PROMPT = (
    "You are configuring a Rust repository at /testbed so its EXISTING test suite can run, and "
    "then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "YOU GET EXACTLY ONE TURN. Nothing will re-invoke you, no scheduled wakeup will ever fire, "
    "and any backgrounded or detached process is killed the moment you stop. Run every command in "
    "the FOREGROUND and wait for it to finish. Never end your turn intending to resume later — if "
    "you are running short on budget, write the best Dockerfile you can to {gen_path} NOW instead "
    "of deferring.\n\n"
    "The grader rebuilds your Dockerfile from a clean base and then, inside the fresh image, runs "
    "EXACTLY this gate at /testbed:\n"
    "    export PATH=/usr/local/cargo/bin:$PATH && cargo test --no-run\n"
    "If that gate fails, NO tests are run at all and the repo scores ZERO. If it passes, the "
    "grader runs the suite with `cargo nextest run --profile ci`.\n\n"
    "Two things about that gate decide whether your Dockerfile works:\n"
    "  - It runs under a LOGIN shell, which resets PATH. The grader prepends exactly one directory, "
    "/usr/local/cargo/bin, and relies on the rest of the default profile PATH for everything else. "
    "So `cargo` must be reachable at /usr/local/cargo/bin or on that default PATH — an `ENV PATH` "
    "in your Dockerfile does NOT survive, because the login shell overwrites it. The base image "
    "already satisfies this; if you install another toolchain, symlink its `cargo` into "
    "/usr/local/bin rather than editing PATH.\n"
    "  - It compiles from scratch in a container that has never built this crate, and it runs under "
    "a time limit. Nothing carries over from your container, so a crate that compiles only at gate "
    "time can exhaust that limit and score ZERO on a repo that was actually fine. Compiling in the "
    "Dockerfile is how you avoid that: end it with `RUN cargo test --no-run` so the crate registry, "
    "the pinned toolchain and ./target are baked into the image. That command COMPILES the tests "
    "without executing any, which is exactly what is wanted — it is not the forbidden step.\n\n"
    "So, first, in THIS container: install any system packages the crate needs to build "
    "(`sudo apt-get install -y ...` — pkg-config, libssl-dev and cmake are already present), then "
    "run `cargo test --no-run` at /testbed until it exits 0. If the repo has a rust-toolchain.toml "
    "or rust-toolchain file, LEAVE IT ALONE: rustup reads it and installs the pinned toolchain "
    "automatically. The gate runs at the repo ROOT. If this repository has no Cargo.toml at the "
    "root, the gate cannot run at all — add a root workspace manifest covering the real crates as "
    "part of your setup, and re-encode it in the Dockerfile. Do NOT create or edit "
    ".config/nextest.toml — the grader writes its own. You may edit configuration files. DO NOT "
    "modify, add, or delete any test files. DO NOT execute the test suite (running the gate command "
    "above to check your work is fine).\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment FROM A "
    "CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` (do NOT "
    "rely on any files from this container — the build starts empty, and the grader requires "
    "/testbed to still be a git worktree);\n"
    "  - install the SAME system packages you installed, as RUN steps. The build runs as ROOT, so "
    "DROP every `sudo` prefix (use `apt-get install -y ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. `RUN sed -i ...` "
    "or a heredoc), since the clone is pristine;\n"
    "  - end with `RUN cargo test --no-run` to bake the build;\n"
    "  - NOT contain `cargo nextest run`, `cargo test` WITHOUT `--no-run`, or any other command "
    "that EXECUTES tests.\n"
    "When the Dockerfile is written, stop.\n"
)
```

Add the profile after `NODE_PROFILE`:

```python
RUST_PROFILE = LangProfile(
    key="rust",
    default_base="rust:1",
    prompt_template=_RUST_PROMPT,
    # rust:1, not -slim/alpine: rust.py hardcodes PATH=/usr/local/cargo/bin (the full image's
    # layout) and its ensure_cmd curls a prebuilt cargo-nextest, so curl must ship in the base.
    workbench="claude-runner-rust:latest",
    # None: RustLanguage.ensure_cmd installs cargo-nextest at measure time.
    test_runner=None,
)
```

- [ ] **Step 5: Register it and shrink the parity set**

In `_PROFILES` add `"rust": RUST_PROFILE,`. In `producers/tests/test_language_profiles.py` change:

```python
_NO_PROFILE_YET = {"golang", "go", "java"}
```

(`rust` is removed because it now has a profile; the two-way parity assertions then force the seam to stay consistent.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest producers/tests -q -k "rust or parity or alias"`
Expected: PASS.

- [ ] **Step 7: Build the workbench image for real**

Run: `docker build -t claude-runner-rust:latest -f docker/claude-runner-rust.Dockerfile docker/`
Then verify the toolchain is reachable exactly the way the grader reaches it:
```bash
docker run --rm claude-runner-rust:latest bash -lc \
  'export PATH=/usr/local/cargo/bin:$PATH && cargo --version && node --version && id agent'
```
Expected: a cargo version, a node version, and an `id` line for uid 1000. If cargo is not found, the image layout diverged from `rust.py:7` and Task 2 is not done.

- [ ] **Step 8: Run the full suite and commit**

Run: `python3 -m pytest bench/tests/bench producers/tests runner/tests -q --deselect bench/tests/bench/test_e2e_smoke.py::test_itsdangerous_measures_green`
Expected: 365 passed, 6 skipped.

```bash
git add docker/claude-runner-rust.Dockerfile producers/_claudecode_helpers.py producers/tests/test_language_profiles.py
git commit -m "feat(producers): Rust profile + Rust workbench image

The prompt pins cargo to /usr/local/cargo/bin because rust.py has no env
indirection and the grader's login shell discards ENV PATH, and it requires
RUN cargo test --no-run so the gate does not compile cold."
```

---

### Task 3: Java workbench image + JAVA_PROFILE

**Files:**
- Create: `docker/claude-runner-java.Dockerfile`
- Modify: `producers/_claudecode_helpers.py` (add `_JAVA_PROMPT`, `JAVA_PROFILE`, register)
- Test: `producers/tests/test_language_profiles.py`

**Interfaces:**
- Consumes: `LangProfile.workbench` (Task 1).
- Produces: `JAVA_PROFILE` with `key="java"`, `default_base="maven:3-eclipse-temurin-17"`, `workbench="claude-runner-java:latest"`, `test_runner=None`.

- [ ] **Step 1: Write the failing tests**

Append to `producers/tests/test_language_profiles.py`:

```python
# ── java ────────────────────────────────────────────────────────────────────────────────────

def _java_prompt(full_name="o/r"):
    from producers._claudecode_helpers import JAVA_PROFILE
    return build_prompt(full_name, resolve_base(JAVA_PROFILE, None), JAVA_PROFILE)


def test_java_profile_shape():
    from producers._claudecode_helpers import JAVA_PROFILE
    assert JAVA_PROFILE.key == "java"
    # JAVA_HOME in this image is /opt/java/openjdk, which is exactly java.py's hardcoded default.
    assert JAVA_PROFILE.default_base == "maven:3-eclipse-temurin-17"
    assert JAVA_PROFILE.workbench == "claude-runner-java:latest"
    assert JAVA_PROFILE.test_runner is None      # Surefire/Gradle emit JUnit natively


def test_java_prompt_names_both_gate_branches_and_the_consequence():
    prompt = _java_prompt()
    assert "test-compile" in prompt               # Maven branch
    assert "testClasses" in prompt                # Gradle branch
    assert "ZERO" in prompt


def test_java_prompt_reproduces_the_gate_verbatim_including_the_x_tests():
    # The prompt says "EXACTLY this", so it must actually match java.py: the branch is `[ -f
    # pom.xml ]` and the wrapper choice is `[ -x ./mvnw ]` / `[ -x ./gradlew ]` — EXECUTABLE, not
    # merely present. A wrapper that lost its exec bit falls through to a different-versioned
    # system tool, which is a silent-wrong-answer failure, not a loud one.
    prompt = _java_prompt()
    assert "[ -f pom.xml ]" in prompt
    assert "[ -x ./mvnw ]" in prompt
    assert "[ -x ./gradlew ]" in prompt
    assert "EXECUTABLE, not merely" in prompt


def test_java_prompt_states_the_post_gate_runner_prefers_the_wrapper():
    # java.py run_cmd prefers ./mvnw -B test / ./gradlew test. Saying plain `mvn -B test` would
    # mislead an agent whose repo pins a different Maven via the wrapper.
    prompt = _java_prompt()
    assert "./mvnw -B test" in prompt
    assert "./gradlew test" in prompt


def test_java_prompt_warns_about_a_natively_nested_pom():
    # Not only "do not MOVE the pom" — a repo that already keeps its pom below the root hits the
    # same gate failure with the agent having done nothing wrong.
    prompt = _java_prompt()
    assert "subdirectory" in prompt and "aggregator pom.xml" in prompt


def test_java_prompt_tells_the_agent_env_java_home_is_honored():
    # java.py reads ${JAVA_HOME:-/opt/java/openjdk}. `bash -l` resets PATH but NOT JAVA_HOME, so a
    # Dockerfile ENV JAVA_HOME is respected by the grader — the only escape hatch for a repo that
    # cannot build on JDK 17. No agent would infer this.
    prompt = _java_prompt()
    assert "ENV JAVA_HOME" in prompt


def test_java_prompt_forbids_moving_the_root_build_file():
    # The gate branches on `[ -f pom.xml ]` at the ROOT: a Maven project relocated into a
    # subdirectory falls through to the Gradle branch and fails on a perfectly healthy repo.
    # Case-sensitive on ROOT: the prompt shouts it, and a lowercase assertion would pass on
    # unrelated prose ("the root cause") while the actual warning went missing.
    prompt = _java_prompt()
    assert "pom.xml" in prompt and "ROOT" in prompt


def test_java_prompt_asks_for_the_compile_and_gives_the_real_reason():
    # Same rule as the Rust twin: the grader does not inspect the Dockerfile for a compile step, so
    # the justification is the exec timeout, not an invented requirement.
    prompt = _java_prompt()
    assert "time limit" in prompt
    assert "COMPILES the tests without executing" in prompt
    assert "REQUIRED work your Dockerfile must do" not in prompt


def test_java_prompt_handles_a_wrapperless_gradle_repo():
    # RAT's own java image apt-installs gradle; measured, that yields Gradle 4.4.1 (2017), which
    # cannot build a modern project. So the prompt tells the agent to fetch a real one instead —
    # strictly more capable than the reference environment, not merely at parity with it. And it
    # must say SYMLINK: unpacking a distribution under /usr/local/bin/gradle-8.5/ leaves `gradle`
    # itself unresolvable, which is the obvious way to follow this instruction and still fail.
    prompt = _java_prompt()
    assert "ships no EXECUTABLE ./gradlew" in prompt
    assert "apt-get install gradle" in prompt          # named as the thing NOT to do
    assert "symlink its `bin/gradle`" in prompt


def test_neither_new_prompt_claims_the_grader_passes_no_daemon():
    # java.py passes neither --no-daemon nor any daemon flag. Telling the agent to add one while
    # also saying the gate runs "EXACTLY" as quoted is self-contradictory.
    from producers._claudecode_helpers import JAVA_PROFILE, RUST_PROFILE
    for profile in (JAVA_PROFILE, RUST_PROFILE):
        assert "--no-daemon" not in profile.prompt_template


def test_java_prompt_states_the_one_turn_constraint():
    prompt = _java_prompt()
    assert "EXACTLY ONE TURN" in prompt
    assert "FOREGROUND" in prompt


def test_java_prompt_never_mentions_python_or_node_tooling():
    prompt = _java_prompt()
    for token in ("pip", "pytest", "npm ci", "package.json"):
        assert token not in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest producers/tests/test_language_profiles.py -q -k java`
Expected: FAIL — `ImportError: cannot import name 'JAVA_PROFILE'`.

- [ ] **Step 3: Write the Java workbench Dockerfile**

Create `docker/claude-runner-java.Dockerfile`:

```dockerfile
# docker/claude-runner-java.Dockerfile
# The WORKBENCH image for Java repos in the `claudecode-dockerfile` lane. This is where the agent
# explores and compiles live; it is NOT the base of the Dockerfile the agent emits (that is
# JAVA_PROFILE.default_base, also maven:3-eclipse-temurin-17) and it is never measured.
#
# maven:3-eclipse-temurin-17 sets JAVA_HOME=/opt/java/openjdk, which is exactly the default
# java.py falls back to, and puts mvn on the login-shell PATH. It ships NO gradle binary, and that
# is deliberate: JavaLanguage prefers ./gradlew, which 12 of the 13 Gradle repos in rat_java50.json
# ship. The RAT reference image apt-installs gradle, but measured that is Gradle 4.4.1 (2017),
# which cannot build a modern project — so baking it in would add ~150MB of false confidence. The
# agent has curl and sudo and is told by the prompt to fetch a real Gradle when a repo needs one.
FROM maven:3-eclipse-temurin-17

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl git ca-certificates sudo \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI.
RUN npm install -g @anthropic-ai/claude-code

# Non-root user: Claude Code refuses --permission-mode bypassPermissions as root. The agent needs
# a writable ~/.m2, which its own home provides.
#
# No `-u 1000` here, unlike the python and rust workbenches. This base is UBUNTU-derived and ships
# a stock `ubuntu` user already holding uid 1000, so pinning it makes useradd exit 4 and the build
# fail. Nothing in the harness cares about the number: the producer addresses this account purely
# by name (`docker exec -u agent`, `chown -R agent:agent /testbed`), so letting useradd pick the
# next free uid is correct and keeps us from deleting a user the base image put there.
RUN useradd -m -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

# The image default user intentionally stays root; the producer selects `agent` per-exec.
WORKDIR /testbed
```

- [ ] **Step 4: Write the Java prompt and profile**

In `producers/_claudecode_helpers.py`, add after `_RUST_PROMPT`:

```python
_JAVA_PROMPT = (
    "You are configuring a Java repository at /testbed so its EXISTING test suite can run, and "
    "then writing a Dockerfile that reproduces your setup from scratch.\n\n"
    "YOU GET EXACTLY ONE TURN. Nothing will re-invoke you, no scheduled wakeup will ever fire, "
    "and any backgrounded or detached process is killed the moment you stop. Run every command in "
    "the FOREGROUND and wait for it to finish. Never end your turn intending to resume later — if "
    "you are running short on budget, write the best Dockerfile you can to {gen_path} NOW instead "
    "of deferring.\n\n"
    "The grader rebuilds your Dockerfile from a clean base and then, inside the fresh image, runs "
    "EXACTLY this at /testbed, after `export PATH=\"$JAVA_HOME/bin:$PATH\"`:\n"
    "    if [ -f pom.xml ]; then\n"
    "        if [ -x ./mvnw ]; then ./mvnw -q -B test-compile; else mvn -q -B test-compile; fi\n"
    "    else\n"
    "        if [ -x ./gradlew ]; then ./gradlew -q testClasses; else gradle -q testClasses; fi\n"
    "    fi\n"
    "If that gate fails, NO tests are run at all and the repo scores ZERO. If it passes, the "
    "grader runs the suite the same way — `./mvnw -B test` or `mvn -B test`, else `./gradlew test` "
    "or `gradle test` — and reads the Surefire / Gradle JUnit XML.\n\n"
    "Four things about that gate decide whether your Dockerfile works:\n"
    "  - It picks its branch on `[ -f pom.xml ]` AT THE REPOSITORY ROOT. If this project keeps its "
    "real pom.xml in a subdirectory, the gate takes the GRADLE branch and fails on a healthy repo — "
    "so add an aggregator pom.xml at the root that builds the real modules, and re-encode it in "
    "the Dockerfile. Equally, never move, rename or delete a root build file that is already "
    "there.\n"
    "  - It picks the wrapper on `[ -x ./mvnw ]` / `[ -x ./gradlew ]` — EXECUTABLE, not merely "
    "present. A wrapper that lost its execute bit silently falls back to the system tool at a "
    "different version. If the repo ships a wrapper, `chmod +x` it and re-encode that as a RUN "
    "step.\n"
    "  - The image is JDK 17. If this project cannot compile on 17, install the JDK it needs and "
    "set `ENV JAVA_HOME=/path/to/that/jdk` in your Dockerfile — the grader reads JAVA_HOME and "
    "will honor it. (Do not try the same trick with PATH; the grader runs under a login shell "
    "that resets PATH.)\n"
    "  - It compiles from scratch with an empty dependency cache, under a time limit. A project "
    "that only downloads its dependencies at gate time can exhaust that limit and score ZERO when "
    "it was actually fine. Compiling in the Dockerfile is how you avoid that: end it with the "
    "gate's own compile command so ~/.m2 (or the Gradle cache) and the build outputs are baked "
    "into the image. That command COMPILES the tests without executing any, which is exactly what "
    "is wanted — it is not the forbidden step.\n\n"
    "If this project builds with Gradle and ships no EXECUTABLE ./gradlew, the gate falls back to a "
    "system `gradle` that the base image does not have, and the repo scores ZERO. In that case "
    "install one yourself and re-encode it in the Dockerfile — but do NOT use "
    "`apt-get install gradle`, which is several major versions behind and cannot build a modern "
    "project. Download the distribution the project expects, unpack it, and symlink its `bin/gradle` "
    "to /usr/local/bin/gradle so that `gradle` itself resolves on the default PATH.\n\n"
    "So, first, in THIS container: install any system packages the build needs "
    "(`sudo apt-get install -y ...`), then run the gate command above at /testbed until it exits "
    "0. You may edit configuration files. DO NOT modify, add, or delete any test files. DO NOT "
    "execute the test suite (running the gate command above to check your work is fine).\n\n"
    "Then write a self-contained Dockerfile to {gen_path} that reproduces this environment FROM A "
    "CLEAN BASE. It MUST:\n"
    "  - start `FROM {base}`;\n"
    "  - `RUN git clone https://github.com/{full_name} /testbed` and `WORKDIR /testbed` (do NOT "
    "rely on any files from this container — the build starts empty, and the grader requires "
    "/testbed to still be a git worktree);\n"
    "  - install the SAME system packages you installed, as RUN steps. The build runs as ROOT, so "
    "DROP every `sudo` prefix (use `apt-get install -y ...`);\n"
    "  - re-encode any edits you made to repo files as explicit RUN steps (e.g. `RUN sed -i ...` "
    "or a heredoc), since the clone is pristine;\n"
    "  - end with the gate's own compile command to bake the dependency cache;\n"
    "  - NOT run `mvn test`, `gradle test`, or any other command that EXECUTES tests.\n"
    "When the Dockerfile is written, stop.\n"
)
```

Add the profile:

```python
JAVA_PROFILE = LangProfile(
    key="java",
    # JAVA_HOME here is /opt/java/openjdk — exactly the default java.py falls back to — and mvn is
    # on the login-shell PATH. No gradle binary; JavaLanguage prefers ./gradlew, which 12 of the
    # 13 Gradle repos in rat_java50.json ship.
    default_base="maven:3-eclipse-temurin-17",
    prompt_template=_JAVA_PROMPT,
    workbench="claude-runner-java:latest",
    # None: Surefire and Gradle write JUnit XML natively, so there is nothing to install.
    test_runner=None,
)
```

- [ ] **Step 5: Register it and empty the Rust/Java parity set**

In `_PROFILES` add `"java": JAVA_PROFILE,`. In the test file change:

```python
_NO_PROFILE_YET = {"golang", "go"}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m pytest producers/tests -q -k "java or parity or alias"`
Expected: PASS.

- [ ] **Step 7: Build the workbench image for real**

Run: `docker build -t claude-runner-java:latest -f docker/claude-runner-java.Dockerfile docker/`
Then:
```bash
docker run --rm claude-runner-java:latest bash -lc \
  'echo "$JAVA_HOME" && mvn -v | head -1 && node --version && id agent'
```
Expected: `/opt/java/openjdk`, a Maven version line, a node version, and an `id` line for `agent` (uid 1001 — this base is Ubuntu and its stock `ubuntu` user holds 1000; see the Dockerfile comment). If `JAVA_HOME` is not `/opt/java/openjdk`, the base diverged from `java.py:11` and Task 3 is not done.

- [ ] **Step 8: Run the full suite and commit**

Run: `python3 -m pytest bench/tests/bench producers/tests runner/tests -q --deselect bench/tests/bench/test_e2e_smoke.py::test_itsdangerous_measures_green`
Expected: 377 passed, 6 skipped.

```bash
git add docker/claude-runner-java.Dockerfile producers/_claudecode_helpers.py producers/tests/test_language_profiles.py
git commit -m "feat(producers): Java profile + Java workbench image

The prompt tells the agent ENV JAVA_HOME is honored by the grader (java.py
reads \${JAVA_HOME:-...} and a login shell resets only PATH), which is the
sole escape hatch for repos that cannot build on JDK 17."
```

---

### Task 4: Language-aware preflight

`_ensure_claude_runner` currently builds exactly one image, `claude-runner:latest`. A Rust or Java run needs its own workbench built before any repo starts, for the same reason the original preflight exists: without it, every repo records `status="error"`, which on disk is indistinguishable from a run that scored zero.

**Files:**
- Modify: `runner/cli.py:62-89` (the preflight), `:166` (the call site)
- Test: `runner/tests/test_claude_runner_image.py`

**Interfaces:**
- Consumes: `LangProfile.workbench` (Task 1), `RUST_PROFILE`/`JAVA_PROFILE` (Tasks 2-3).
- Produces: `_ensure_claude_runner(model, repo_root=REPO_ROOT, runner=subprocess.run, repos_json=None)` — same name, one new keyword-only-in-practice argument, so existing calls keep working.

- [ ] **Step 1: Write the failing tests**

Append to `runner/tests/test_claude_runner_image.py`:

```python
def _write_dataset(tmp_path, languages):
    import json
    p = tmp_path / "ds.json"
    p.write_text(json.dumps([{"full_name": f"o/r{i}", "language": lang}
                             for i, lang in enumerate(languages)]))
    return str(p)


def test_builds_only_the_workbench_the_dataset_needs(tmp_path):
    # A Rust dataset must not build the python/node workbench, and vice versa: each build is ~1GB
    # and several minutes.
    fake = _FakeRun(1, 0)                    # inspect -> missing, build -> ok
    ds = _write_dataset(tmp_path, ["Rust", "Rust"])
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=ds)
    tags = [c[3] for c in fake.calls if c[:3] == ["docker", "image", "inspect"]]
    assert tags == ["claude-runner-rust:latest"]
    build = [c for c in fake.calls if c[1] == "build"][0]
    assert build[3] == "claude-runner-rust:latest"
    assert build[5].endswith("claude-runner-rust.Dockerfile")


def test_mixed_language_dataset_builds_every_needed_workbench(tmp_path):
    fake = _FakeRun(1, 0, 1, 0)              # two (inspect-missing, build-ok) pairs
    ds = _write_dataset(tmp_path, ["Java", "Python"])
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=ds)
    tags = sorted(c[3] for c in fake.calls if c[:3] == ["docker", "image", "inspect"])
    assert tags == ["claude-runner-java:latest", "claude-runner:latest"]


def test_no_dataset_keeps_the_original_single_workbench(tmp_path):
    # Tier/variety runs pass no --repos-json. Those are Python, and must behave exactly as before.
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=None)
    assert len(fake.calls) == 1
    assert fake.calls[0][3] == "claude-runner:latest"


def test_unreadable_dataset_falls_back_to_the_default_workbench(tmp_path):
    # A malformed dataset must not abort the run before it starts; the default is the safe guess.
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    fake = _FakeRun(0)
    _ensure_claude_runner("claudecode-dockerfile", repo_root=str(tmp_path), runner=fake,
                          repos_json=str(bad))
    assert fake.calls[0][3] == "claude-runner:latest"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest runner/tests/test_claude_runner_image.py -q`
Expected: FAIL — `TypeError: _ensure_claude_runner() got an unexpected keyword argument 'repos_json'`.

- [ ] **Step 3: Implement the language-aware preflight**

Replace `runner/cli.py:66-89` with:

```python
def _dataset_workbenches(repos_json) -> list:
    """The workbench tags a dataset needs, in stable order.

    Falls back to the single default workbench when there is no dataset (tier/variety runs, which
    are Python) or when the file cannot be read — a preflight must never be the thing that aborts
    a run before it starts."""
    from producers._claudecode_helpers import get_profile
    default = ["claude-runner:latest"]
    if not repos_json:
        return default
    try:
        with open(repos_json, encoding="utf-8") as fh:
            repos = json.load(fh)
        tags = {get_profile((r.get("language") or "")).workbench for r in repos}
    except Exception:                                    # noqa: BLE001 — degrade, never abort
        return default
    return sorted(tags) or default


def _ensure_claude_runner(model: str, repo_root: str = REPO_ROOT, runner=subprocess.run,
                          repos_json=None) -> None:
    """Build every claude-runner workbench the run needs, BEFORE any repo runs.

    These images are local-only (no registry — `docker pull claude-runner` fails), so a routine
    `docker system prune` silently removes them. When one is gone the ccdf producer's anti-vanish
    guard turns every repo into status="error", which on disk is indistinguishable from a
    completed run that scored zero. Building them up front makes the failure loud and early.

    Which images are needed depends on the dataset: a Rust repo's agent works inside
    claude-runner-rust (cargo present), not the python+node default. Building only what the
    dataset uses keeps a Python run from paying for three ~1GB builds.

    Idempotent: an existing image is a single `docker image inspect` and no build. `runner` is
    injectable so the preflight is unit-testable without Docker.
    """
    if model not in _CLAUDE_LANES:
        return
    override = os.environ.get("CLAUDE_RUNNER_IMAGE")
    tags = [override] if override else _dataset_workbenches(repos_json)
    ctx = os.path.join(repo_root, "docker")
    for tag in tags:
        if runner(["docker", "image", "inspect", tag], capture_output=True).returncode == 0:
            continue
        # Tag -> recipe by convention: claude-runner-rust:latest -> claude-runner-rust.Dockerfile
        dockerfile = os.path.join(ctx, tag.split(":")[0] + ".Dockerfile")
        print(f"[bench] {tag} missing — building from {dockerfile}", flush=True)
        rc = runner(["docker", "build", "-t", tag, "-f", dockerfile, ctx]).returncode
        if rc != 0:
            raise SystemExit(
                f"[bench] FATAL: could not build {tag} (rc={rc}). The {model} lane cannot run "
                f"without it; every repo would silently record status=\"error\".")
```

No import changes are needed: `runner/cli.py` already imports `json` (line 5), `os`, and
`subprocess`, and `producers/tests/test_language_profiles.py` already imports `NODE_PROFILE`,
`PYTHON_PROFILE`, `_PROFILES`, `build_prompt`, `get_profile` and `resolve_base` at module level —
which is everything Tasks 1-3's tests use apart from the new names they import locally.

- [ ] **Step 4: Update the call site**

Change `runner/cli.py:166` from `_ensure_claude_runner(model)` to:

```python
    _ensure_claude_runner(model, repos_json=args.repos_json)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest runner/tests -q`
Expected: PASS — the four new tests plus all pre-existing preflight tests (which pass `repos_json=None` implicitly and must be unaffected).

- [ ] **Step 6: Run the full suite and commit**

Run: `python3 -m pytest bench/tests/bench producers/tests runner/tests -q --deselect bench/tests/bench/test_e2e_smoke.py::test_itsdangerous_measures_green`
Expected: 381 passed, 6 skipped.

```bash
git add runner/cli.py runner/tests/test_claude_runner_image.py
git commit -m "feat(runner): build the workbench(es) the dataset actually needs

A Rust dataset needs claude-runner-rust, not the python+node default. No
dataset (tier/variety runs) keeps the original single-image behaviour."
```

---

### Task 5: Live two-repo smoke

Everything above is unit-tested against fakes. This task proves the seam works against real Docker and a real agent, on the smallest repo in each dataset, before anyone spends money on 100 repos.

**Files:**
- Create: none (a run, not code)

**Interfaces:**
- Consumes: everything from Tasks 1-4.

- [ ] **Step 1: Build the two-repo dataset**

Pick the smallest Rust and Java repos so the smoke is cheap. Run:

```bash
python3 - << 'EOF'
import json
sel = []
for lang in ("rust", "java"):
    d = json.load(open(f"datasets/rat_{lang}50.json"))
    small = sorted([x for x in d if x["size"] == "small"], key=lambda x: x["code_bytes"])
    sel.append(small[0])
json.dump(sel, open("/tmp/rj_smoke2.json", "w"), indent=2)
for x in sel:
    print(f"  {x['full_name']:<40} {x['language']:<6} {x['commit'][:8]} "
          f"{x['code_bytes']/1e6:.2f}MB tests={x['test_count']}")
EOF
```

- [ ] **Step 2: Verify the profiles route correctly before spending anything**

Run:

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from producers._claudecode_helpers import get_profile, resolve_base, resolve_workbench
from bench.languages import get_language
for lang in ('Rust','Java'):
    p, m = get_profile(lang), get_language(lang)
    print(f'{lang:<6} produce->{p.key:<7} base={resolve_base(p,None):<28} '
          f'workbench={resolve_workbench(p,None):<28} measure->{m.name:<7} '
          f'{\"OK\" if p.key==m.name else \"*** MISMATCH ***\"}')
"
```
Expected: both lines end `OK`. A `MISMATCH` here means Tasks 2-3 are incomplete; stop and fix before running.

- [ ] **Step 3: Run the smoke**

```bash
export CLAUDE_MAX_BUDGET_USD=2.0
./run_bench.sh claudecode-dockerfile --tier all --concurrency 2 --llm sonnet \
  --repos-json /tmp/rj_smoke2.json --run-name rj-smoke2
```
Watch for these two lines, which prove Task 4 works:
```
[bench] claude-runner-rust:latest missing — building from .../claude-runner-rust.Dockerfile
[bench] claude-runner-java:latest missing — building from .../claude-runner-java.Dockerfile
```

- [ ] **Step 4: Check the emitted Dockerfiles before trusting any score**

```bash
RUN=$(ls -d runs/claudecode-dockerfile/rj-smoke2-* | tail -1)
for d in $RUN/output/*/*/eval_build/Dockerfile; do echo "=== $d ==="; cat "$d"; done
```
The Rust one must start `FROM rust:1` and end with `RUN cargo test --no-run`. The Java one must
start `FROM maven:3-eclipse-temurin-17` and end with a `test-compile`/`testClasses` line. A
`FROM python:3.11` in either means `get_profile` is still falling back to Python.

- [ ] **Step 5: Check the measured rows**

```bash
RUN=$(ls -d runs/claudecode-dockerfile/rj-smoke2-* | tail -1)   # re-derive: new shell
python3 -c "
import json, glob, sys
for p in sorted(glob.glob('$RUN/measure/claudecode-dockerfile/*/*/row.json')):
    r = json.load(open(p)); n = '/'.join(p.split('/')[-3:-1])
    print(f\"{n:<40} clean={r['collect_clean']} exec={r['executed']} \"
          f\"total={r['total']} passed={r['passed']} rate={r['pass_rate']}\")
"
```
Success is `executed=True` with `total > 0` on both. Read failures as:
- `collect_clean=False` → the **gate** failed. Rust: the crate did not compile (or cargo was not
  found, meaning the prompt's `/usr/local/cargo/bin` clause did not take). Java: `test-compile`
  failed, most likely a JDK-version mismatch — check whether the agent used `ENV JAVA_HOME`.
- `collect_clean=True, executed=False` → the gate passed but no JUnit XML appeared. Rust: nextest
  did not write `target/nextest/ci/junit.xml`. Java: neither Surefire nor Gradle report path
  matched `java.py`'s `junit_glob`.

- [ ] **Step 6: Record the result in the plan and commit**

Append a short "Smoke result" section to this file with the two rows from Step 5 and the emitted
`FROM` lines, then:

```bash
git add docs/superpowers/plans/2026-07-27-rust-java-producer-support.md
git commit -m "docs: record the rust/java producer smoke result"
```

---

## Known limitations (deliberate, not oversights)

- **`google/tsunami-security-scanner` is the one at-risk Java repo.** It is the only 1 of 13 Gradle repos in `rat_java50.json` with no `./gradlew`, and no stock image carries a usable Gradle (the RAT reference apt-installs one, but that is Gradle 4.4.1 from 2017). Task 3's prompt tells the agent to download a real Gradle for exactly this case, which is more than the reference environment does — but it is an agent-dependent recovery, not a guarantee. If it fails, that is a known single zero, not a harness bug.
- **JDK 17 will not suit every repo.** This is the `node:20`→`node:22` problem again but bidirectional: a newer JDK breaks old Maven plugins, an older one cannot compile new sources. The prompt's `ENV JAVA_HOME` clause is the per-repo escape hatch; if a full run shows a systematic JDK-version failure, re-baseline `JAVA_PROFILE.default_base` the way `NODE_PROFILE` was moved to `node:22` (commit `933a8c2`), with measured evidence.
- **Rust large-tier repos may exceed the budget.** `RUN cargo test --no-run` on a large workspace can take longer than the whole $2 produce budget. The node-full50 run already capped 7 of 50 repos; expect Rust to be worse, and treat "hit the cap" as a recorded property of the run rather than a bug.
- **`test_count` in both datasets is a lower bound.** The curation counted annotation-style declarations only, so JUnit 3-style `public void testFoo()` methods are invisible (`elastic/elasticsearch` reads 200). It only ever under-counts, so it cannot have admitted a repo below the ≥10 threshold.
- **Go still has no profile.** `_NO_PROFILE_YET` remains `{"golang", "go"}` after Task 3, and the parity test keeps it honest.

---

## Smoke result (2026-07-27, run `rj-smoke3-20260727-134626`)

Three repos, not the two the plan specified. `RxAndroid` is the smallest Java repo but is Gradle
**plus** the Android SDK, so a failure there would have measured a missing SDK rather than this
seam; it was replaced by `pedrovgs/Algorithms` (Maven, pure Java) and `Netflix/concurrency-limits`
(Gradle, wrapper, no Android) so that BOTH `java.py` gate branches are exercised.

| repo | lang | clean | exec | total | passed | rate | cost |
|---|---|---|---|---|---|---|---|
| `denisidoro/navi` | Rust | ✓ | ✓ | 20 | 19 | 0.95 | $0.167 |
| `pedrovgs/Algorithms` | Java/Maven | ✓ | ✓ | 503 | 503 | 1.00 | $0.152 |
| `Netflix/concurrency-limits` | Java/Gradle | ✓ | **✗** | 0 | 0 | 0.00 | $0.380 |

`EBSR 1.0 · ESSR 0.975 · real_success 0.667 · total $0.699`

**The seam works.** Emitted Dockerfiles were exactly what the prompts asked for: `FROM rust:1`
ending `RUN cargo test --no-run`; `FROM maven:3-eclipse-temurin-17` ending `RUN mvn -q -B
test-compile`. The Java agent emitted `RUN chmod +x ./gradlew` unprompted-by-anything-but-the-new
clause — direct evidence that the `[ -x ./mvnw ]` executability wording (codex finding #3) changed
agent behaviour on its first live run. Per-language workbench selection was confirmed live: the
Java container ran on `claude-runner-java:latest`.

### BLOCKER for the Java dataset — `java.py` junit_glob is root-only

`Netflix/concurrency-limits` passed its gate and still scored zero. Cause, verified: it declares 5
subprojects in `settings.gradle`, so Gradle writes JUnit to
`concurrency-limits-core/build/test-results/test/*.xml`, while `java.py:45` globs only
`{W}/target/surefire-reports/*.xml {W}/build/test-results/test/*.xml` — the ROOT. `Algorithms` has
zero `<module>` entries, which is precisely why it worked.

Measured across `rat_java50.json`: **43 of 50 repos are multi-module** (large 19/20, medium 17/20,
small 7/10). As it stands the Java dataset would report a false zero on 43 repos.

This is a pre-existing measure-side defect, NOT part of this plan's producer work — Rust is
unaffected (`target/nextest/ci/junit.xml` is always at the workspace root). It must be fixed before
any scored Java run. The fix is not a one-line glob change: `measure.py` passes the spec to
`cat` through `bash -lc`, and `**` does not recurse without `globstar`, so it needs a `find`-based
collection (the multi-file merge in `_merge_junit` already handles many files).
