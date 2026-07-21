# producers/dockeragent.py
#
# The PRODUCE half of today's dockeragent_model.predict (design §3, method 1): run our agent
# adapter -> a Dockerfile string + metadata. NO docker build, NO pytest, NO scoring — bench/
# rebuilds and scores from the packet `write_env_packet` lands.
from __future__ import annotations

import os
import re
import sys
import time

from producers.base import ProduceContext, ProducedEnv

# bench.schema is on the path via producers.base's shim (imported above).
from bench.schema import RepoSpec  # noqa: E402


def _load_adapter_cls(agent_root: str | None = None):
    """Lazily import MultiDockerEvalAdapter (a ~140KB module that pulls in weave etc.).

    Kept out of module import so `import producers` stays light and testable without the agent
    stack installed. Tests inject a stub via `DockerAgentProducer(adapter_cls=...)` and never
    reach this loader.

    `dockeragent` is a per-checkout METHOD FAMILY (design §B): radical / john-planner-v1 /
    Jayint-Planer / v3-core / john-v3-multi-lang are all `model="dockeragent"` on different
    branches, and EACH branch ships its OWN multi_docker_eval_adapter.py in its checkout. So the
    adapter is loaded from the AGENT CHECKOUT root — `agent_root` (from ProduceContext), else
    $DOCKERAGENT_ROOT — NOT from the harness tree (whose legacy copy was deleted). Neither set =>
    a loud RuntimeError beats silently loading a wrong/absent adapter.
    """
    root = agent_root or os.environ.get("DOCKERAGENT_ROOT")
    if not root:
        raise RuntimeError(
            "dockeragent adapter root is unknown: set ProduceContext.agent_root (the branch's "
            "checkout that holds multi_docker_eval_adapter.py) or the DOCKERAGENT_ROOT env var. "
            "The legacy harness copy has been removed and is no longer a fallback.")
    # Load the EXACT adapter file at THIS checkout root. A bare `from multi_docker_eval_adapter
    # import ...` is unsafe for a per-checkout method family: (a) if `root` lacks the adapter it
    # would silently fall through to some OTHER checkout's adapter already on sys.path (wrong
    # agent scored, no error); (b) sys.modules caches by name, so loading agent A then agent B in
    # one process would return A's adapter for B. So require the file here and exec it explicitly.
    adapter_path = os.path.join(root, "multi_docker_eval_adapter.py")
    if not os.path.isfile(adapter_path):
        raise RuntimeError(
            f"no multi_docker_eval_adapter.py at agent checkout root {root!r} "
            f"(dockeragent expects each branch to ship its own adapter)")
    # Keep the checkout on sys.path for the adapter's OWN transitive imports (agent.py, etc.)...
    if root not in sys.path:
        sys.path.insert(0, root)   # checkout FIRST so the branch's live deps win
    # ...but load THIS adapter file under a root-unique module name so a stale sys.modules entry
    # from another checkout can't shadow it and an invalid root can't silently pick a different
    # checkout's adapter. abs(hash(abspath)) is deterministic-within-run and unique-per-root.
    import importlib.util
    mod_name = "_dockeragent_adapter_" + str(abs(hash(os.path.abspath(root))))
    spec = importlib.util.spec_from_file_location(mod_name, adapter_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MultiDockerEvalAdapter


class DockerAgentProducer:
    """Direct producer for the dockeragent varieties (john-planner-v3, react, ...)."""
    name = "dockeragent"
    needs_llm = True
    measurable = True

    def __init__(self, llm: str | None = None, num_turn: int = 30,
                 base_image: str = "auto", adapter_cls=None):
        self.llm = llm
        self.num_turn = num_turn
        self.base_image = base_image
        self._adapter_cls = adapter_cls   # injectable for tests; None => lazy real adapter

    def produce(self, repo: RepoSpec, ctx: ProduceContext) -> ProducedEnv:
        # Anti-vanish invariant (design §1): produce() ALWAYS returns a ProducedEnv, even on
        # internal failure — the ENTIRE body (loader + adapter call + result normalization) is
        # guarded, so a malformed/non-dict adapter result can never propagate without a ProducedEnv.
        start = time.time()
        try:
            instance_id = repo.full_name.replace("/", "__")
            llm = ctx.llm or self.llm
            num_turn = ctx.num_turn if ctx.num_turn is not None else self.num_turn
            # Resolve the checkout that owns this branch's adapter: prefer the per-repo
            # ctx.agent_root, else $DOCKERAGENT_ROOT (both handled by the loader). Skipped
            # entirely when a stub adapter_cls is injected (tests).
            agent_root = ctx.agent_root or os.environ.get("DOCKERAGENT_ROOT")
            adapter_cls = self._adapter_cls or _load_adapter_cls(agent_root)

            res = adapter_cls(output_dir=ctx.workdir).process_single_instance(
                {"instance_id": instance_id, "repo_url": repo.repo_url, "language": repo.language},
                base_image=self.base_image, model=llm, max_steps=num_turn,
                enable_artifact_preflight=False)   # bench scores it; skip our own preflight

            res = res.get(instance_id, res)         # tolerate {id: result} or a bare result
            logs = res.get("logs") or {}
            economy = {
                "produce_s": round(time.time() - start, 2),
                "tokens_in": logs.get("tokens_in"),
                "tokens_out": logs.get("tokens_out"),
                "llm_calls": logs.get("llm_calls"),
                "turns_used": logs.get("turns_used"),
            }
            base_image = res.get("base_image") or self.base_image
            dockerfile = res.get("dockerfile")
            if not dockerfile:
                return ProducedEnv(repo=repo, dockerfile=None, status="error",
                                   note=f"agent produced no Dockerfile: {logs.get('error')}",
                                   base_image=base_image, producer_name=self.name, economy=economy)

            # Ensure-pytest nicety (matches today's model): the fresh-container measure needs pytest.
            if not re.search(r"\bpytest\b", dockerfile):
                dockerfile = dockerfile.rstrip() + "\nRUN pip install --no-cache-dir pytest\n"

            return ProducedEnv(repo=repo, dockerfile=dockerfile,
                               setup_scripts=(res.get("setup_scripts") or {}),
                               base_image=base_image, head_sha=res.get("head_sha") or "",
                               status="produced", conformance="native",
                               producer_name=self.name, economy=economy)
        except Exception as exc:                    # noqa: BLE001 — boundary guard, never propagate
            return ProducedEnv(repo=repo, dockerfile=None, status="error",
                               note=repr(exc), base_image=self.base_image,
                               producer_name=self.name,
                               economy={"produce_s": round(time.time() - start, 2)})
