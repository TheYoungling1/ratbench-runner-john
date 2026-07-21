#!/usr/bin/env python3
"""Run the official RAT SWEAgentModel for ONE repo, under the py3.11 sweagent venv.

The main benchmark runner is Python 3.10 and cannot import `sweagent` (needs >=3.11).
This thin script is subprocessed by SweAgentSubprocessModel (3.10) under
/opt/sweagent_venv (3.11), reusing the official SWEAgentModel.predict so the
sweagent + pytest-tool + result-JSON logic lives in ONE place (DRY). It loads
sweagent_model.py via importlib to avoid eval/models/__init__'s heavy import chain.

Result protocol: prints one line `__SWEAGENT_RESULT__<json>` with the predict() dict.
The two pytest result JSONs are written to {root_path}/output/{full_name}/ by
predict() itself (the runner's scorers read those).
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys


def _load_sweagent_model(rat_root: str):
    sys.path.insert(0, rat_root)
    spec = importlib.util.spec_from_file_location(
        "_swe_model", os.path.join(rat_root, "eval", "models", "sweagent_model.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SWEAgentModel


# OpenRouter slugs that LiteLLM's static cost map doesn't ship. SWE-agent refuses to run an
# unmapped model when per_instance_cost_limit > 0 (its budget safety check raises
# ModelConfigurationError), so we register approximate per-token costs — observed from real
# OpenRouter usage — before the agent starts. This lets the SWE-agent baseline run on the
# SAME OpenRouter model + OPENROUTER_API_KEY the DockerAgent baseline uses
# (deepseek/deepseek-v4-flash) with its $ budget intact. Costs are approximate (~within 2x of
# the actual upstream provider), which only affects the economy metric, never solve/EBSR.
_KNOWN_MODEL_COSTS = {
    "deepseek/deepseek-v4-flash": {
        "input_cost_per_token": 1.32e-7,
        "output_cost_per_token": 2.64e-7,
        "max_input_tokens": 65536,
        "max_output_tokens": 8192,
        "max_tokens": 8192,
        "litellm_provider": "openrouter",
        "mode": "chat",
    },
}


def _ensure_sweagent_config(root_path: str, rat_root: str) -> None:
    """Materialize the SWE-agent YAML configs into {root_path}/eval/sweagent/ — the path the RAT
    SWEAgentWrapper loads from — patching the deployment base image to one that ships git.

    Two reasons we copy-and-patch rather than symlink the RAT tree's eval/:
      1. The wrapper resolves its config relative to root_path, which in our harness is the run
         bucket, separate from the RAT tree where eval/ lives.
      2. The wrapper's git-based repo reset (git fetch/restore/reset/clean) runs INSIDE the
         deployment container before the agent starts, so the base image must have git. The
         shipped config uses python:3.10-slim, which does not — every repo fails with rc=127.
    Override the base with SWEAGENT_DEPLOY_IMAGE (default python:3.10, a full image with git)."""
    import yaml  # available in the sweagent venv where this runner executes
    src_dir = os.path.join(rat_root, "eval", "sweagent")
    if not os.path.isdir(src_dir):
        return
    image = os.environ.get("SWEAGENT_DEPLOY_IMAGE", "python:3.10")
    eval_dir = os.path.join(root_path, "eval")
    # Drop a stale symlink from an older runner so we never write back into the RAT tree.
    if os.path.islink(eval_dir):
        try:
            os.unlink(eval_dir)
        except OSError:
            pass
    dst_dir = os.path.join(eval_dir, "sweagent")
    try:
        os.makedirs(dst_dir, exist_ok=True)
    except OSError:
        return
    for name in os.listdir(src_dir):
        if not name.endswith(".yaml"):
            continue
        src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        try:
            with open(src) as fh:
                cfg = yaml.safe_load(fh)
            dep = (cfg or {}).get("env", {}).get("deployment")
            if isinstance(dep, dict) and "image" in dep:
                dep["image"] = image
            # SWE-agent's function_calling parser rejects any response with != 1 tool call
            # (FunctionCallingFormatError "included multiple tool calls"), and deepseek-v4-flash
            # batches several calls per turn — so the agent exits on format errors before doing
            # anything. Two knobs fix the friction: (1) forbid parallel tool calls at the API so
            # the model emits one call per turn; (2) raise the requery budget so the occasional
            # residual multi-call response is re-queried instead of ending the run.
            agent = (cfg or {}).get("agent")
            if isinstance(agent, dict):
                agent["max_requeries"] = int(os.environ.get("SWEAGENT_MAX_REQUERIES", "8"))
                model = agent.get("model")
                if isinstance(model, dict):
                    ck = model.setdefault("completion_kwargs", {})
                    if isinstance(ck, dict):
                        ck.setdefault("parallel_tool_calls", False)
                        # Pin the OpenRouter upstream provider exactly as the DockerAgent
                        # baseline does (libkit/llm.py: provider order + allow_fallbacks=False),
                        # so SWE-agent hits the SAME upstream — not just the same key/model. The
                        # default (Alibaba) also tends to honor parallel_tool_calls, unlike some
                        # fallback providers that keep batching calls.
                        providers = [p.strip() for p in
                                     os.environ.get("OPENROUTER_PROVIDER", "Alibaba").split(",")
                                     if p.strip()]
                        if providers:
                            ck.setdefault("extra_body", {}).setdefault(
                                "provider", {"order": providers, "allow_fallbacks": False})
            with open(dst, "w") as fh:
                yaml.safe_dump(cfg, fh, sort_keys=False)
        except (OSError, yaml.YAMLError):
            try:
                shutil.copyfile(src, dst)  # at least make the config present
            except OSError:
                pass
    # The model copies its in-container test tools (run_pytest.py, run_pytest_collect.py, ...)
    # from {root_path}/libkit/tools, which also lives in the RAT tree. Symlink it read-only so
    # those tools are found; without them no run_pytest_results.json is produced for the scorers.
    libkit_dst = os.path.join(root_path, "libkit")
    libkit_src = os.path.join(rat_root, "libkit")
    if not os.path.exists(libkit_dst) and os.path.isdir(libkit_src):
        try:
            os.symlink(libkit_src, libkit_dst)
        except OSError:
            pass


def _use_collision_free_cwd(root_path: str) -> None:
    """SWE-agent rewrites any config string that looks like an EXISTING relative path into an
    absolute path (sweagent.utils.config._strip_abspath_from_dict, hit via RunSingle.from_config).
    If the process cwd holds a directory whose name collides with a bare-word config literal —
    e.g. /opt/harness has a `docker/` dir and the deployment config is `type: docker` — that
    literal is mangled into a path (`/opt/harness/docker`) and fails the discriminated-union
    validation. Run from an empty directory so no literal is mistaken for a path."""
    cwd = os.path.join(root_path, ".sweagent_cwd")
    try:
        os.makedirs(cwd, exist_ok=True)
        os.chdir(cwd)
    except OSError:
        pass


def _register_model_costs(llm_name: str) -> None:
    """Register costs for OpenRouter models LiteLLM doesn't know, so SWE-agent's cost-limit
    check passes. No-op for models already mapped (real OpenAI/Anthropic slugs)."""
    try:
        import litellm
    except Exception:
        return
    # The slug may be "openrouter/<vendor>/<model>"; LiteLLM costs by the bare name, so
    # register under both the prefixed and bare keys.
    base = llm_name[len("openrouter/"):] if llm_name.startswith("openrouter/") else llm_name
    cost = _KNOWN_MODEL_COSTS.get(base) or _KNOWN_MODEL_COSTS.get(llm_name)
    if not cost:
        return
    try:
        litellm.register_model({base: cost, f"openrouter/{base}": cost})
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-name", required=True)
    ap.add_argument("--root-path", required=True)
    ap.add_argument("--llm", default="claude-sonnet-4")
    ap.add_argument("--num-turn", type=int, default=15)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--cost-limit", type=float, default=2.0)
    a = ap.parse_args()
    rat_root = os.environ.get("RAT_ROOT", "/opt/rat_root")
    _ensure_sweagent_config(a.root_path, rat_root)
    _use_collision_free_cwd(a.root_path)
    _register_model_costs(a.llm)
    try:
        SWEAgentModel = _load_sweagent_model(rat_root)
        model = SWEAgentModel(root_path=a.root_path, timeout=a.timeout, llm=a.llm,
                              num_turn=a.num_turn, save_mode="none",
                              swe_agent_cost_limit=a.cost_limit)
        out = model.predict(a.full_name)
    except Exception as e:
        import traceback
        traceback.print_exc()
        out = {"status": "error", "failure_reason": "sweagent_exception",
               "error": str(e), "root_path": a.root_path, "full_name": a.full_name}
    sys.stdout.write("__SWEAGENT_RESULT__" + json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
