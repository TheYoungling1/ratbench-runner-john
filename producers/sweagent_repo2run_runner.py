#!/usr/bin/env python3
"""Run SWE-agent for ONE repo under the Repo2Run-paper config and recover /Dockerfile.

Subprocessed by producers/sweagent_repo2run.py under the py3.11 sweagent venv
($SWEAGENT_VENV_PY), because `sweagent` is `requires-python = ">=3.11"` while the runner venv is
3.10 — the same split the in-place `sweagent` lane uses.

This is a SEPARATE entry point from runner/live/sweagent_runner.py on purpose: that script drives
the RAT SWEAgentModel, whose wrapper hardcodes eval/sweagent/python-config.yaml and whose whole
job is the in-place run_pytest JSONs. Nothing there is reused, and nothing there is modified, so
the existing `sweagent` arm keeps running exactly as it does today.

The agent writes /Dockerfile INSIDE the deployment container, which is outside /repo and therefore
absent from SWE-agent's submitted patch. So the container is kept alive after the agent finishes
and the file is read out of it (`env.read_file`) — mirroring the keep_container branch that
rat/libkit/sweagent_wrapper.py already uses for its in-container pytest.

Result protocol: one stdout line `__SWEAGENT_DF_RESULT__<json>`; the Dockerfile text itself goes to
--out (a file), never through stdout, so a large or oddly-encoded Dockerfile cannot corrupt it.
"""
import argparse
import copy
import json
import os
import re
import shlex
import subprocess
import sys
import time

# Python puts THIS script's directory at sys.path[0], and that directory is `producers/`, which
# contains `sweagent.py` (the non-producer gate for the in-place lane). So a bare `import sweagent`
# resolves to that module instead of the installed package and dies with
# "No module named 'sweagent.agent'; 'sweagent' is not a package". Drop our own directory from the
# path before any sweagent import — this script is standalone and imports no siblings.
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _SELF_DIR]

RESULT_MARK = "__SWEAGENT_DF_RESULT__"
DOCKERFILE_PATH = "/Dockerfile"

# OpenRouter slugs LiteLLM's static cost map does not ship. SWE-agent computes the cost of every
# response via `litellm.cost_calculator.completion_cost(...)` and, if that raises while
# per_instance_cost_limit > 0, aborts the run with ModelConfigurationError — so an unmapped model
# fails on the FIRST turn, before the agent does anything. runner/live/sweagent_runner.py registers
# the in-place lane's model for exactly this reason; this arm needs its own.
# Prices are per-token, read from the OpenRouter model API. Registered under both the bare slug and
# the `openrouter/`-prefixed form, since which key LiteLLM resolves depends on its routing.
# LiteLLM's static cost map does not ship these slugs. SWE-agent prices every response via
# `litellm.cost_calculator.completion_cost(...)` and, if that raises while per_instance_cost_limit
# > 0, aborts the run with ModelConfigurationError — an unmapped model fails on the FIRST turn.
#
# TWO maps, because the same weights cost different amounts depending on how you reach them.
# Keyed exactly as litellm resolves them (`self.config.name`).

# DeepSeek's own API. Prices from api-docs.deepseek.com/quick_start/pricing (2026-09-03).
# These are the OFF-PEAK rates. DeepSeek doubles them during peak — 01:00-04:00 and 06:00-10:00
# UTC, Mon-Fri — and a static table cannot express that, so a run inside a peak window is
# UNDER-reported by exactly 2x. Input also swings 31x on cache: $0.007/1M on a hit versus $0.22/1M
# on a miss. `cache_read_input_token_cost` lets litellm price hits correctly; the flat
# `input_cost_per_token` is the miss rate, which is the conservative assumption.
_DEEPSEEK_DIRECT_COSTS = {
    "deepseek/deepseek-v4-flash": {
        "input_cost_per_token": 2.2e-7,             # cache MISS, off-peak ($0.22/1M)
        "cache_read_input_token_cost": 7e-9,        # cache HIT,  off-peak ($0.007/1M)
        "output_cost_per_token": 6.6e-7,            # off-peak ($0.66/1M)
        "max_input_tokens": 1048576,                # vendor: 1M context
        "max_output_tokens": 384000,                # vendor: 384K max output
        "max_tokens": 384000,
        "litellm_provider": "deepseek",
        "mode": "chat",
    },
}

# OpenRouter's blended resale rate for the same weights — matches neither DeepSeek price, because
# it is OpenRouter's own. Kept so switching the variety's pin back needs no code change. Limits are
# the VENDOR's, not OpenRouter's route metadata (which advertises 1,310,720 / 943,718 for -0731);
# DeepSeek is what actually rejects an oversized request.
_OPENROUTER_COSTS = {
    "deepseek/deepseek-v4-flash-0731": {
        "input_cost_per_token": 6.5e-8,
        "output_cost_per_token": 1.8e-7,
        "max_input_tokens": 1048576,
        "max_output_tokens": 384000,
        "max_tokens": 384000,
        "litellm_provider": "openrouter",
        "mode": "chat",
    },
    "deepseek/deepseek-v4-flash": {
        "input_cost_per_token": 8.8606e-8,
        "output_cost_per_token": 1.77212e-7,
        "max_input_tokens": 1048576,
        "max_output_tokens": 384000,
        "max_tokens": 384000,
        "litellm_provider": "openrouter",
        "mode": "chat",
    },
}

# Back-compat alias for tests/readers that expect one table.
_KNOWN_MODEL_COSTS = {**_OPENROUTER_COSTS, **_DEEPSEEK_DIRECT_COSTS}


def _register_model_costs() -> None:
    """Teach LiteLLM the per-token cost of the slugs it does not ship, before SWE-agent starts.
    Best-effort: a failure here must not stop the run — it only risks the budget check firing."""
    try:
        import litellm

        # Bare slug -> DeepSeek direct; `openrouter/`-prefixed -> OpenRouter's resale rate.
        # Registering one price under both routes would misprice whichever one you are not using.
        entries = dict(_DEEPSEEK_DIRECT_COSTS)
        for slug, cost in _OPENROUTER_COSTS.items():
            entries[f"openrouter/{slug}"] = cost
        litellm.utils.register_model(entries)
    except Exception as exc:  # noqa: BLE001 — never fatal; see docstring
        print(f"[sweagent_repo2run] could not register model costs: {exc!r}", flush=True)


def deployment_container_name(runner_obj) -> str | None:
    """The deployment container's name, read BEFORE the environment is closed.

    `env.close()` stops the container AND clears `deployment._container_name`, so reading it
    afterwards always yields None — which is exactly how the first version of this cleanup silently
    did nothing while looking like it worked. Capture it while the env is still live."""
    try:
        dep = getattr(runner_obj.env, "deployment", None)
        return getattr(dep, "_container_name", None) or getattr(dep, "container_name", None)
    except Exception:                            # noqa: BLE001 — never fatal
        return None


def remove_deployment_container(name: str | None, log=print) -> str | None:
    """Force-remove SWE-agent's deployment container after the Dockerfile has been read out.

    `remove_container: False` is deliberate — the container must outlive the agent so /Dockerfile
    can be read back — but nothing then removes it. `env.close()` STOPS the container; it does not
    delete it, so each repo leaves an `Exited` container behind holding its whole image layer
    (~291MB measured). Across 50 repos that is ~15GB of orphans, and the scheduler's disk gate
    would start pausing launches rather than failing loudly. producers/claudecode_dockerfile.py
    already does this for its own container; this is the same courtesy.

    Takes the name captured by deployment_container_name() before close(). Returns the name it
    removed, or None. Never raises: a cleanup failure must not lose a produce that has already been
    paid for — but it always LOGS, because a cleanup that silently does nothing is indistinguishable
    from one that works until the disk fills."""
    if not name:
        log("[sweagent_repo2run] cleanup: no container name captured — nothing removed")
        return None
    try:
        r = subprocess.run(["docker", "rm", "-f", str(name)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log(f"[sweagent_repo2run] cleanup: docker rm -f {name} rc={r.returncode} "
                f"{(r.stderr or '').strip()[:160]}")
            return None
        return str(name)
    except Exception as exc:                     # noqa: BLE001 — never fatal
        log(f"[sweagent_repo2run] cleanup: {exc!r}")
        return None


def _render(template: str, variables: dict) -> str:
    out = template
    for key, value in variables.items():
        out = out.replace("{{%s}}" % key, str(value))
    return out


_G2E_RC = re.compile(r"__G2E_RC=(\d+)")
_TEMPLATE_KEYS = ("system_template", "instance_template", "next_step_template",
                  "next_step_no_output_template", "problem_statement_template")


def apply_setup_spec(cfg: dict, spec: dict) -> dict:
    """Graph2Env setup-deliverable mode: substitute the two prompt placeholders and point the
    deployment and model at what the spec says. Pure; returns a new dict."""
    cfg = copy.deepcopy(cfg)
    variables = {"context_block": spec.get("context_block", ""), "base_image": spec["base_image"]}
    templates = cfg.setdefault("agent", {}).setdefault("templates", {})
    for key in _TEMPLATE_KEYS:
        if key in templates:
            templates[key] = _render(templates[key], variables)
    dep = cfg.setdefault("env", {}).setdefault("deployment", {})
    dep["image"] = spec["deploy_image"]
    dep["docker_args"] = list(spec.get("docker_args") or [])
    cfg["agent"].setdefault("model", {}).update(spec.get("model") or {})
    return cfg


def collect_script(handoff: dict, test_command: str) -> str:
    """One shell line: apply the handoff the way the evaluator does, run the test command,
    capture its output to /tmp/g2e_collect.txt, print the exit code marker."""
    parts = ["cd /repo"]
    for key, value in sorted((handoff.get("environment") or {}).items()):
        parts.append(f"export {key}={shlex.quote(str(value))}")
    for service in handoff.get("services") or []:
        start = str(service.get("start") or "").strip()
        check = str(service.get("check") or "").strip()
        if start:
            parts.append(start)
        if check:
            parts.append(f"for _i in $(seq 1 30); do ({check}) && break; sleep 1; done")
    parts.append(f"{test_command} > /tmp/g2e_collect.txt 2>&1; echo __G2E_RC=$?")
    return "; ".join(parts)


def parse_collect_rc(output: str) -> int | None:
    match = _G2E_RC.search(output or "")
    return int(match.group(1)) if match else None



def resolve_image_digest(tag: str, runner=subprocess.run) -> str | None:
    """Best-effort resolved digest of a local docker image `tag` (provenance item 4a: the
    agent's OWN deployment image — `python:3.10` is a moving tag). Prefers the pulled
    RepoDigest; falls back to the image ID when the image was built locally (no registry
    digest). `runner` is injectable so this is unit-testable without docker. Never raises —
    any failure (docker missing, image not found, timeout) degrades to None."""
    try:
        p = runner(
            ["docker", "inspect", "--format",
             "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}", tag],
            capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return None
        out = (p.stdout or "").strip()
        return out or None
    except Exception:                                    # noqa: BLE001 — anti-vanish, never propagate
        return None


def _apply_overrides(cfg: dict, a) -> dict:
    """Runtime overrides on the loaded YAML (documented at the top of the config file)."""
    agent = cfg.setdefault("agent", {})
    model = agent.setdefault("model", {})
    model["name"] = a.llm
    model["per_instance_cost_limit"] = a.cost_limit
    if os.environ.get("SWEAGENT_TEMPERATURE"):
        model["temperature"] = float(os.environ["SWEAGENT_TEMPERATURE"])
    # $SWEAGENT_THINKING = enabled|disabled flips DeepSeek's chain-of-thought so the same config
    # can run both arms of that comparison. Anything else is ignored rather than passed through —
    # an unknown value reaching the API would 400 mid-run, after the container is already up.
    thinking = (os.environ.get("SWEAGENT_THINKING") or "").strip().lower()
    if thinking in {"enabled", "disabled"}:
        model.setdefault("completion_kwargs", {}).setdefault("extra_body", {})["thinking"] = {
            "type": thinking}
    # The paper does not state a call limit. Left to the caller so this arm can be given the SAME
    # budget as the other produce-able arms rather than silently inheriting a different one.
    if a.call_limit:
        model["per_instance_call_limit"] = a.call_limit
    # Pin the OpenRouter upstream provider the way every other arm does (libkit/llm.py), so a
    # cross-method comparison differs by method and not by which provider served the tokens.
    providers = [p.strip() for p in os.environ.get("OPENROUTER_PROVIDER", "Alibaba").split(",")
                 if p.strip()]
    if providers and str(a.llm).startswith("openrouter/"):
        ck = model.setdefault("completion_kwargs", {})
        ck.setdefault("extra_body", {}).setdefault(
            "provider", {"order": providers, "allow_fallbacks": False})

    env = cfg.setdefault("env", {})
    repo = env.setdefault("repo", {})
    repo["type"] = "local"
    repo["path"] = a.repo_path
    if a.commit:
        repo["base_commit"] = a.commit          # dataset pin -> SWE-agent resets the copy to it
    dep = env.setdefault("deployment", {})
    dep["image"] = os.environ.get("SWEAGENT_DEPLOY_IMAGE", dep.get("image", "python:3.10"))
    dep["remove_container"] = False             # kept alive so /Dockerfile can be read back
    cfg["output_dir"] = a.trajectory_dir
    return cfg


# DeepSeek v4 intermittently serialises its command in its own DSML markup instead of the markdown
# fence `thought_action` requires:
#     <｜｜DSML｜｜bash>\nls -la /repo\n</｜｜DSML｜｜bash>
# The surrounding prose is identical to a well-formed turn, so this is a serialisation choice, not a
# confused model. Two facts make it fatal rather than cosmetic: the parser matches ``` only, and
# DeepSeek's prompt cache makes the response deterministic — all three of SWE-agent's requeries came
# back byte-identical, so the built-in format-retry cannot clear it and the episode dies after four
# turns with exit_status="exit_format". Measured 2026-09-03 at 1/8 calls on the VM and 0/8 from
# macOS with BYTE-IDENTICAL prompts (same sha256 for system and user), so it is host-independent
# chance, not an amd64, litellm, or config difference. Rewriting the tags into a fence leaves the
# paper's parser semantics untouched — it is the delimiter that differs, nothing else.
_DSML_BLOCK = re.compile(r"<[^<>]*DSML[^<>]*>[ \t]*\n?(.*?)\n?[ \t]*</[^<>]*DSML[^<>]*>", re.DOTALL)


def normalize_dsml_fences(text: str) -> str:
    """Rewrite DeepSeek DSML command tags into ``` fences; unchanged when no DSML tag is present."""
    if not text or "DSML" not in text:
        return text
    return _DSML_BLOCK.sub(lambda m: "```\n%s\n```" % m.group(1).strip("\n"), text)


def patch_thought_action_parser(log=print) -> bool:
    """Normalise DSML into fences before ThoughtActionParser sees the response.

    Returns True if the patch was applied, False if it was already in place. Idempotent so a
    re-import cannot stack wrappers."""
    from sweagent.tools.parsing import ThoughtActionParser

    if getattr(ThoughtActionParser, "_dsml_patched", False):
        return False
    original = ThoughtActionParser.__call__

    def patched(self, model_response, commands, strict=False):
        message = model_response.get("message") or ""
        fixed = normalize_dsml_fences(message)
        if fixed != message:
            # Always log: a silent rescue would hide how often the model does this, and that rate
            # is a finding about the model, not noise.
            log(f"[dsml] rewrote DeepSeek DSML tags into a fenced block ({len(message)} chars)")
            model_response = {**model_response, "message": fixed}
        return original(self, model_response, commands, strict=strict)

    ThoughtActionParser.__call__ = patched
    ThoughtActionParser._dsml_patched = True
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-name", required=True)
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--out", required=True, help="where to write the recovered Dockerfile")
    ap.add_argument("--trajectory-dir", required=True)
    ap.add_argument("--llm", required=True)
    ap.add_argument("--language", default="python")
    ap.add_argument("--commit", default=None)
    ap.add_argument("--cost-limit", type=float, default=2.0)
    ap.add_argument("--call-limit", type=int, default=0)
    ap.add_argument("--setup-spec", default=None, help="Graph2Env setup-deliverable spec JSON")
    a = ap.parse_args()

    import yaml

    _register_model_costs()   # must precede SWE-agent's first completion-cost call
    from sweagent.agent.problem_statement import TextProblemStatement
    from sweagent.run.run_single import RunSingle, RunSingleConfig

    start = time.time()
    with open(a.config) as fh:
        cfg = yaml.safe_load(fh)
    if (cfg.get("agent", {}).get("tools", {}).get("parse_function", {}) or {}).get("type") == "thought_action":
        patch_thought_action_parser()

    problem = _render(cfg["agent"]["templates"].get("problem_statement_template", ""), {
        "language": a.language,
        "full_name": a.full_name,
        "repo_url": f"https://github.com/{a.full_name}",
    })
    cfg = _apply_overrides(cfg, a)
    setup_spec = None
    if a.setup_spec:
        with open(a.setup_spec) as fh:
            setup_spec = json.load(fh)
        cfg = apply_setup_spec(cfg, setup_spec)
    cfg["problem_statement"] = TextProblemStatement(
        text=problem, id=a.full_name.replace("/", "_"))

    # RunSingle.from_config runs sweagent.utils.config._strip_abspath_from_dict, which rewrites any
    # config string that looks like an EXISTING RELATIVE PATH into an absolute one. Our repo root
    # contains a `docker/` directory and the deployment config is the bare literal `type: docker`,
    # so running from the repo root mangles it into "/Users/.../docker" and the discriminated-union
    # validation fails with 55 errors. Run from an empty directory so no literal can be mistaken
    # for a path. runner/live/sweagent_runner.py::_use_collision_free_cwd hit this first.
    safe_cwd = os.path.join(a.trajectory_dir, ".sweagent_cwd")
    os.makedirs(safe_cwd, exist_ok=True)
    os.chdir(safe_cwd)

    runner = RunSingle.from_config(RunSingleConfig(**cfg))

    # Manual lifecycle (mirrors sweagent_wrapper's keep_container branch): runner.run() closes the
    # environment, and a closed environment cannot be read from.
    runner._chooks.on_start()
    runner.env.start()

    # The paper's prompt hardcodes "/repo" and tells the agent to validate with
    # `pytest /repo --collect-only -q`. SWE-agent uploads a local tree to `/{repo_name}`, where
    # repo_name is the basename of the host clone dir — producers.sweagent_repo2run.repo_clone_dir
    # names it `repo` for exactly this reason. That is a coupling to SWE-agent internals, so check
    # it rather than trust it: a silent mismatch means the agent works from a path its own
    # instructions contradict for every one of its turns, and we would only find out from a bad
    # Dockerfile 100 turns later.
    landed = getattr(getattr(runner.env, "repo", None), "repo_name", None)
    if landed is not None and landed != "repo":
        runner.env.close()
        raise SystemExit(
            f"repo landed at /{landed}, but the prompt hardcodes /repo. SWE-agent's local-repo "
            "upload path changed; fix producers.sweagent_repo2run.repo_clone_dir or the config's "
            "instance_template together — they must agree.")
    if setup_spec:
        import asyncio
        from swerex.runtime.abstract import UploadRequest
        asyncio.run(runner.env.deployment.runtime.upload(
            UploadRequest(source_path=setup_spec["g2e_dir"], target_path="/g2e")))
    runner._chooks.on_instance_start(index=0, env=runner.env,
                                     problem_statement=runner.problem_statement)
    out_dir = runner.output_dir / runner.problem_statement.id
    out_dir.mkdir(parents=True, exist_ok=True)
    result = runner.agent.run(problem_statement=runner.problem_statement,
                              env=runner.env, output_dir=out_dir)
    runner._chooks.on_instance_completed(result=result)
    runner._chooks.on_end()

    collect_rc, setup_read_error = None, ""
    if setup_spec:
        artifacts_dir = setup_spec["artifacts_dir"]
        os.makedirs(artifacts_dir, exist_ok=True)
        handoff = {}
        for name in ("setup.sh", "runtime_handoff.json"):
            try:
                content = runner.env.read_file(f"/g2e/{name}") or ""
                with open(os.path.join(artifacts_dir, name), "w", encoding="utf-8") as fh:
                    fh.write(content)
                if name == "runtime_handoff.json":
                    handoff = json.loads(content) if content.strip() else {}
            except Exception as exc:                # noqa: BLE001 — record, do not abort
                setup_read_error += f"{name}: {type(exc).__name__}: {exc}; "
        try:
            output = runner.env.communicate(
                collect_script(handoff if isinstance(handoff, dict) else {}, setup_spec["test_command"]),
                timeout=int(setup_spec.get("collect_timeout", 1800)))
            collect_rc = parse_collect_rc(output)
            with open(os.path.join(artifacts_dir, "container_collect.txt"), "w", encoding="utf-8") as fh:
                fh.write(runner.env.read_file("/tmp/g2e_collect.txt") or "")
        except Exception as exc:                    # noqa: BLE001
            setup_read_error += f"collect: {type(exc).__name__}: {exc}; "

    # Capture BEFORE close() — close() clears deployment._container_name.
    container_name = deployment_container_name(runner)

    dockerfile, read_error = "", ""
    try:
        dockerfile = runner.env.read_file(DOCKERFILE_PATH) or ""
    except Exception as exc:                    # noqa: BLE001 — the agent may never have written it
        read_error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            runner.env.close()
        except Exception:                       # noqa: BLE001 — cleanup must not mask the result
            pass
        # close() only STOPS it; without this each repo leaks its container (~291MB).
        removed = remove_deployment_container(container_name)
        if removed:
            print(f"[sweagent_repo2run] removed deployment container {removed}", flush=True)

    if dockerfile:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(dockerfile)

    # Best-effort: which deployment image actually ran (a moving tag like python:3.10 drifts
    # underneath the run without this). Never fatal — a missing/unresolvable digest is just None.
    deploy_image_digest = resolve_image_digest(
        cfg.get("env", {}).get("deployment", {}).get("image", ""))

    # Report the thinking mode that ACTUALLY reached the wire — config default or env override.
    # The run's copied config only records the default, so an override would otherwise vanish, and
    # `reasoning_content` never reaches the .traj (it is in the .debug.log only).
    eff_thinking = (((cfg.get("agent") or {}).get("model") or {})
                    .get("completion_kwargs", {}).get("extra_body", {})
                    .get("thinking", {}).get("type"))

    stats = result.info.get("model_stats") or {}
    payload = {
        "status": "produced" if (dockerfile or (setup_spec and not setup_read_error)) else "error",
        "exit_status": result.info.get("exit_status"),
        "collect_returncode": collect_rc,
        "traj_path": str(out_dir / f"{runner.problem_statement.id}.traj"),
        "artifacts_dir": (setup_spec or {}).get("artifacts_dir", ""),
        "setup_read_error": setup_read_error,
        "agent_settings": {"thinking": eff_thinking, "temperature": (cfg["agent"]["model"]
                                                                     .get("temperature"))},
        "deploy_image_digest": deploy_image_digest,
        "note": "" if (dockerfile or setup_spec) else (
            read_error or f"no {DOCKERFILE_PATH} in the container"),
        "economy": {
            "llm_calls": stats.get("api_calls"),
            "cost_usd": stats.get("instance_cost"),
            "tokens_in": stats.get("tokens_sent"),
            "tokens_out": stats.get("tokens_received"),
            "turns_used": len(result.trajectory) if result.trajectory else None,
            "produce_s": round(time.time() - start, 2),
        },
    }
    print(RESULT_MARK + json.dumps(payload), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
