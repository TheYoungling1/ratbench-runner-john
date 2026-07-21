#!/usr/bin/env python3
"""DockerAgentModel — plugs our DockerAgent into the RAT eval harness.

This model wraps multi_docker_eval_adapter.process_single_instance, builds a
per-repo Docker image from the returned Dockerfile, mounts RAT's pytest runner
tools, runs them inside the container, and copies the result JSON files back out
for the RAT scorers to consume.
"""
# eval/models/dockeragent_model.py   (lives in the RAT repo tree)
import os, re, sys, time, json, subprocess, weave

# Two repo roots — DISTINCT (this was the original draft's bug):
# RAT tree (libkit/, eval/) is <repo>/rat; from runner/models/ the repo root is two dirs
# up. The runner also sets RAT_ROOT and puts it on sys.path before importing this module.
RAT_ROOT   = os.environ.get("RAT_ROOT") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "rat")  # RAT: libkit/, eval/
AGENT_ROOT = os.environ["DOCKERAGENT_ROOT"]            # OUR repo, e.g. /Users/john/rat-bench-integration
sys.path[:0] = [RAT_ROOT, AGENT_ROOT]

from libkit.command import init_output_and_repo                 # RAT repo
from eval.common.base_model import BaseEvalModel                # RAT repo
from eval.common.utils import TimeoutException                  # RAT repo
# multi_docker_eval_adapter is imported LAZILY inside the predict methods (below). A BASELINE run
# (rat/repo2run, DOCKERAGENT_ROOT=/opt/harness) imports THIS module for the class but never calls
# those methods — it must not require the per-agent-checkout adapter to exist at import time.

RP  = f"{RAT_ROOT}/libkit/tools/run_pytest.py"
RPC = f"{RAT_ROOT}/libkit/tools/run_pytest_collect.py"
# Fairness: align the OUTER subprocess timeout to the in-container pytest cap so DockerAgent
# is not handicapped vs the baselines on slow suites (was a hardcoded 600s).
PYTEST_TIMEOUT = int(os.environ.get("RAT_PYTEST_TIMEOUT", "1800"))

# PRODUCE-ONLY is the DEFAULT (design §3 method 1): predict() emits eval_build/Dockerfile +
# _meta.json{status:"produced"} and stops — bench/ rebuilds and scores in a fresh container.
# DOCKERAGENT_INLINE_SCORE=1 restores the legacy inline build+pytest+score path (escape hatch).
INLINE = os.environ.get("DOCKERAGENT_INLINE_SCORE") == "1"


def _resolve_producers_root() -> str:
    """Return the directory that CONTAINS producers/base.py, robust to symlink-vs-copy deploy.

    Order (first hit wins, validated): $PRODUCERS_ROOT, then dirs derived from
    $AGENTS_ROOT/$DOCKERAGENT_ROOT/$RAT_ROOT (the checkout, its parent, its grandparent), then a
    realpath N-up from this file. Raises a clear error if producers/base.py is nowhere — better a
    loud failure than a silent wrong path (FIX 6: realpath N-up breaks when the model is COPIED,
    not symlinked, into the RAT tree)."""
    seen, cands = set(), []

    def _add(p):
        if p and p not in seen:
            seen.add(p)
            cands.append(p)

    _add(os.environ.get("PRODUCERS_ROOT"))
    for var in ("AGENTS_ROOT", "DOCKERAGENT_ROOT", "RAT_ROOT"):
        v = (os.environ.get(var) or "").rstrip("/")
        if v:
            _add(v)
            _add(os.path.dirname(v))
            _add(os.path.dirname(os.path.dirname(v)))
    real = os.path.realpath(__file__)                     # runner/models/<file> -> repo root
    _add(os.path.dirname(os.path.dirname(os.path.dirname(real))))
    for root in cands:
        if os.path.isfile(os.path.join(root, "producers", "base.py")):
            return root
    raise RuntimeError(
        "produce-only: cannot locate producers/base.py. Set PRODUCERS_ROOT to the directory "
        f"that contains producers/. Tried: {cands}")


class DockerAgentModel(BaseEvalModel):
    llm: str
    num_turn: int = 30
    base_image: str = "auto"

    @weave.op
    def predict(self, full_name: str) -> dict:
        # PRODUCE-ONLY is the default; DOCKERAGENT_INLINE_SCORE=1 selects the legacy inline path.
        # The two paths are kept SEPARATE so the escape hatch is byte-identical to the original
        # (its docker-cleanup finally covers the FULL flow) while produce-only stays docker-free.
        if INLINE:
            return self._predict_inline(full_name)
        return self._predict_produce_only(full_name)

    def _predict_inline(self, full_name: str) -> dict:
        """ESCAPE HATCH (DOCKERAGENT_INLINE_SCORE=1): the ORIGINAL inline build+pytest+score path,
        verbatim — the docker-cleanup finally covers the FULL flow (agent run through score), so an
        early failure or a KeyboardInterrupt before the build still removes the image/container."""
        start = time.time()
        slug = full_name.lower().replace("/", "-")
        image, container = f"dockeragent-eval-{slug}", f"dockeragent-{slug}"
        out_dir = f"{self.root_path}/output/{full_name}"
        ctx     = f"{out_dir}/eval_build"                  # CLEAN build context (avoid the agent's huge workplace/)
        ok = {"root_path": self.root_path, "full_name": full_name}
        # Best-effort metadata — populated incrementally; never let collection break the run.
        meta = {"requested_model": self.llm, "base_image": self.base_image, "head_sha": ""}
        try:
            try:
                init_output_and_repo(self.root_path, full_name, renew=True)
                os.makedirs(ctx, exist_ok=True)

                # 1) Run OUR agent -> docker_res dict. The eval Dockerfile (a STRING) is self-contained:
                #    it `git clone`s the repo into /testbed and bakes the verified setup recipe.
                from multi_docker_eval_adapter import MultiDockerEvalAdapter  # lazy: checkout-only
                res = MultiDockerEvalAdapter(output_dir=out_dir).process_single_instance(
                    {"instance_id": full_name.replace("/", "__"),
                     "repo_url": f"https://github.com/{full_name}", "language": "python"},
                    base_image=self.base_image, model=self.llm, max_steps=self.num_turn,
                    enable_artifact_preflight=False)           # RAT scores it; skip our Multi-Docker-Eval preflight
                res = res.get(full_name.replace("/", "__"), res)        # tolerate {id: result} or result
                # Propagate resolved base_image from agent result if available.
                try:
                    if res.get("base_image"):
                        meta["base_image"] = res["base_image"]
                except Exception:
                    pass
                dockerfile = res.get("dockerfile")
                if not dockerfile:
                    return {"status": "error", "failure_reason": "no_dockerfile",
                            "error": f"agent produced no Dockerfile: {res.get('logs', {}).get('error')}",
                            **ok, **meta}
                self._check_timeout(start, "agent")

                # 2) Ensure pytest, write a clean build context, build.
                if not re.search(r"\bpytest\b", dockerfile):
                    dockerfile = dockerfile.rstrip() + "\nRUN pip install --no-cache-dir pytest\n"
                with open(f"{ctx}/Dockerfile", "w") as f: f.write(dockerfile)
                for name, content in (res.get("setup_scripts") or {}).items():   # any files the Dockerfile COPYs
                    with open(f"{ctx}/{name}", "w") as f: f.write(content)
                try:
                    subprocess.run(["docker", "build", "-t", image, ctx], check=True, timeout=3600)
                except subprocess.CalledProcessError as e:
                    return {"status": "error", "failure_reason": "build_failed",
                            "error": str(e), **ok, **meta}
                except subprocess.TimeoutExpired as e:
                    return {"status": "timeout", "failure_reason": "docker_timeout",
                            "error": str(e), **ok, **meta}

                # 3) Mount RAT's tools, run them AT /testbed (CWD == repo), copy result JSONs to out_dir.
                W = "/testbed"
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
                subprocess.run(["docker","run","-d","--name",container,"-w",W,
                                "-v",f"{RP}:/run_pytest.py","-v",f"{RPC}:/run_pytest_collect.py",
                                image,"tail","-f","/dev/null"], check=True, timeout=600)
                subprocess.run(["docker","exec",container,"mkdir","-p",f"{W}/logs"], check=True, timeout=600)

                # 3a) Best-effort: capture HEAD sha from the cloned repo inside the image.
                try:
                    sha_proc = subprocess.run(
                        ["docker","exec",container,"git","-C",W,"rev-parse","HEAD"],
                        check=True, capture_output=True, text=True, timeout=60)
                    meta["head_sha"] = sha_proc.stdout.strip()
                except Exception:
                    pass  # head_sha stays ""

                subprocess.run(["docker","exec",container,"python3","/run_pytest_collect.py"], check=False, timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker","cp",f"{container}:{W}/logs/run_pytest_collect_results.json",
                                f"{out_dir}/run_pytest_collect_results.json"], check=True, timeout=600)    # check=True: match baselines (missing => error, not silent success)
                subprocess.run(["docker","exec",container,"python3","/run_pytest.py"], check=False, timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker","cp",f"{container}:{W}/logs/run_pytest_results.json",
                                f"{out_dir}/run_pytest_results.json"], check=True, timeout=600)             # check=True: match baselines
                return {"status": "success", "failure_reason": None, **ok, **meta}
            except TimeoutException:
                return {"status": "timeout", "failure_reason": "agent_timeout", **ok, **meta}
            except subprocess.TimeoutExpired as e:
                return {"status": "timeout", "failure_reason": "docker_timeout",
                        "error": str(e), **ok, **meta}
            except Exception as e:
                return {"status": "error", "failure_reason": "repo_error",
                        "error": str(e), **ok, **meta}
            finally:
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
                subprocess.run(f"docker rmi {image} >/dev/null 2>&1", shell=True)
        except KeyboardInterrupt:
            subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True); raise

    def _predict_produce_only(self, full_name: str) -> dict:
        """DEFAULT: run the agent and emit the env packet (design §3 method 1). NO docker at all.

        Deliberately does NOT create eval_build/ — write_env_packet creates it ONLY when a
        Dockerfile is produced (FIX 3: a pre-created, empty eval_build/ reads as a *vanish* to
        bench.harvest). On no-Dockerfile/error we return a plain error dict and leave no eval_build/.
        """
        start = time.time()
        out_dir = f"{self.root_path}/output/{full_name}"
        ok = {"root_path": self.root_path, "full_name": full_name}
        meta = {"requested_model": self.llm, "base_image": self.base_image, "head_sha": ""}
        try:
            init_output_and_repo(self.root_path, full_name, renew=True)
            # Run OUR agent -> docker_res dict (the eval Dockerfile is a self-contained STRING).
            from multi_docker_eval_adapter import MultiDockerEvalAdapter  # lazy: checkout-only
            res = MultiDockerEvalAdapter(output_dir=out_dir).process_single_instance(
                {"instance_id": full_name.replace("/", "__"),
                 "repo_url": f"https://github.com/{full_name}", "language": "python"},
                base_image=self.base_image, model=self.llm, max_steps=self.num_turn,
                enable_artifact_preflight=False)
            res = res.get(full_name.replace("/", "__"), res)        # tolerate {id: result} or result
            try:
                if res.get("base_image"):
                    meta["base_image"] = res["base_image"]
            except Exception:
                pass
            dockerfile = res.get("dockerfile")
            if not dockerfile:
                # No artifact: leave NO eval_build/ (FIX 3). The runner records the failure.
                return {"status": "error", "failure_reason": "no_dockerfile",
                        "error": f"agent produced no Dockerfile: {res.get('logs', {}).get('error')}",
                        **ok, **meta}
            self._check_timeout(start, "agent")
            if not re.search(r"\bpytest\b", dockerfile):
                dockerfile = dockerfile.rstrip() + "\nRUN pip install --no-cache-dir pytest\n"
            return self._finish_produce_only(full_name, out_dir, res, dockerfile, meta, ok, start)
        except TimeoutException:
            return {"status": "timeout", "failure_reason": "agent_timeout", **ok, **meta}
        except Exception as e:
            return {"status": "error", "failure_reason": "repo_error",
                    "error": str(e), **ok, **meta}

    def _finish_produce_only(self, full_name, out_dir, res, dockerfile, meta, ok, start):
        """PRODUCE-ONLY finish: write the env packet + run_produced.json marker, no docker.

        Reuses the shared producer contract (producers.base.write_env_packet) so the on-disk
        packet is identical to what a standalone producer would land: eval_build/Dockerfile +
        _meta.json{status:"produced", base_image, produce_s, economy}. bench/ is the scorer.
        """
        produce_s = round(time.time() - start, 2)
        meta["produce_s"] = produce_s
        logs = res.get("logs") or {}
        economy = {"produce_s": produce_s}
        for k in ("tokens_in", "tokens_out", "llm_calls", "turns_used"):
            v = logs.get(k)
            economy[k] = v
            if v is not None:
                meta[k] = v
        # Make producers/ + bench/ importable (FIX 6: robust to symlink-vs-copy deploy).
        _root = _resolve_producers_root()
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from producers.base import ProducedEnv, write_env_packet
        from bench.schema import RepoSpec
        env = ProducedEnv(
            repo=RepoSpec(full_name, f"https://github.com/{full_name}"),
            dockerfile=dockerfile,
            setup_scripts=(res.get("setup_scripts") or {}),
            base_image=meta.get("base_image"),
            head_sha=meta.get("head_sha") or "",
            status="produced",
            conformance="native",
            producer_name="dockeragent",
            economy=economy,
        )
        write_env_packet(os.path.join(self.root_path, "output"), env)
        # Resume marker: unique to produce-only (claudecode-dockerfile also writes
        # eval_build/Dockerfile, so the runner keys resume/repair-skip on run_produced.json).
        with open(os.path.join(out_dir, "run_produced.json"), "w") as f:
            json.dump({"status": "produced", "base_image": meta.get("base_image"),
                       "produce_s": produce_s}, f, indent=2)
        return {"status": "success", "produced": True, **ok, **meta}
