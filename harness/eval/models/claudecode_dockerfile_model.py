#!/usr/bin/env python3
"""ClaudeCodeDockerfileModel — agentic env-setup that ALSO emits a reusable Dockerfile.

Extends ClaudeCodeModel: the agent sets up + (harness-)verifies a live /testbed env
(the in-build signal A), writes /testbed/Dockerfile.gen, which we then build FRESH from a
clean base and score (the eval/headline signal B). The gap between A and B is the
synthesizer gap. See docs/superpowers/specs/2026-06-26-claudecode-dockerfile-design.md.
"""
import json
import os
import sys
import time
import subprocess

import weave

from eval.models.claudecode_model import (
    ClaudeCodeModel, RP, RPC, PYTEST_TIMEOUT, W, AUTH_KEYS,
    _normalize_model, _as_text, init_output_and_repo, download_repo,
)
from eval.common.utils import TimeoutException
from eval.models._claudecode_dockerfile_helpers import (
    build_prompt, parse_inbuild, write_instance_json, ensure_pytest, DOCKERFILE_GEN_PATH,
)

# Emitted Dockerfile base. Use the FULL python image (NOT -slim): it ships git +
# build-essential, matching the live claude-runner sandbox (slim + git + build tools).
# -slim lacks git, so the agent's `RUN git clone` would fail to build on every repo.
DOCKERFILE_BASE = os.environ.get("CLAUDE_DOCKERFILE_BASE", "python:3.11")
# Two full pytest runs now (live in-build + built-image eval); reserve more for them.
PYTEST_RESERVE = int(os.environ.get("CLAUDE_PYTEST_RESERVE", "900"))

# PRODUCE-ONLY is the DEFAULT (design §3 method 2): predict() runs the agent, pulls
# /testbed/Dockerfile.gen (already conforming — the agent clones to /testbed), and emits
# eval_build/Dockerfile + _meta.json{status:"produced"} + run_produced.json, then stops. NO
# docker build, NO pytest, NO scoring — bench/ rebuilds and scores in a fresh container.
# CLAUDECODE_INLINE_SCORE=1 restores the legacy inline in-build+build+score path (escape hatch).
INLINE = os.environ.get("CLAUDECODE_INLINE_SCORE") == "1"


def _resolve_producers_root() -> str:
    """Return the directory that CONTAINS producers/base.py, robust to symlink-vs-copy deploy.

    Order (first hit wins, validated): $PRODUCERS_ROOT, then dirs derived from
    $AGENTS_ROOT/$DOCKERAGENT_ROOT/$RAT_ROOT (the checkout, its parent, its grandparent), then a
    realpath N-up from this file. Raises a clear error if producers/base.py is nowhere — better a
    loud failure than a silent wrong path (a realpath N-up breaks when the model is COPIED, not
    symlinked, into the RAT tree)."""
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
    real = os.path.realpath(__file__)                     # harness/eval/models/<file> -> repo root
    _add(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(real)))))
    for root in cands:
        if os.path.isfile(os.path.join(root, "producers", "base.py")):
            return root
    raise RuntimeError(
        "produce-only: cannot locate producers/base.py. Set PRODUCERS_ROOT to the directory "
        f"that contains producers/. Tried: {cands}")


class ClaudeCodeDockerfileModel(ClaudeCodeModel):
    @weave.op
    def predict(self, full_name: str) -> dict:
        # PRODUCE-ONLY is the default; CLAUDECODE_INLINE_SCORE=1 selects the legacy inline path.
        # The two paths are kept SEPARATE so the escape hatch is byte-identical to the original
        # (its docker-cleanup finally covers the FULL flow) while produce-only stays build-free.
        if INLINE:
            return self._predict_inline(full_name)
        return self._predict_produce_only(full_name)

    def _predict_produce_only(self, full_name: str) -> dict:
        """DEFAULT (design §3 method 2): run the agent, pull /testbed/Dockerfile.gen, emit the env
        packet. NO in-build verification, NO docker build, NO pytest, NO scoring — bench/ does it.

        The agent clones to /testbed, so the emitted Dockerfile is already CONFORMING (no re-home);
        write_env_packet lands it at eval_build/Dockerfile + _meta{status:"produced",
        conformance:"native"} + a run_produced.json resume marker. Only the live agent `container`
        is created (no eval image / run container), so cleanup is a single `docker rm -f`.
        """
        start = time.time()
        slug = full_name.lower().replace("/", "-")
        container = f"claudecode-dockerfile-{slug}"          # live agent sandbox (only env created)
        out_dir = f"{self.root_path}/output/{full_name}"
        ctx = f"{out_dir}/eval_build"                         # clean build context (Dockerfile only)
        ok = {"root_path": self.root_path, "full_name": full_name}
        meta = {"requested_model": self.llm, "base_image": self.base_image,
                "dockerfile_base": DOCKERFILE_BASE, "head_sha": ""}

        auth = {k: os.environ[k] for k in AUTH_KEYS if os.environ.get(k)}
        if not auth:
            return {"status": "error", "failure_reason": "no_auth",
                    "error": "set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY", **ok, **meta}

        try:
            try:
                init_output_and_repo(self.root_path, full_name, renew=True)
                download_repo(self.root_path, full_name, has_issue=False, use_repo_dockerfile=False)
                repo_src = f"{self.root_path}/input/repo/{full_name}"
                os.makedirs(out_dir, exist_ok=True)
                os.makedirs(ctx, exist_ok=True)
                self._check_timeout(start, "clone")

                # 1) Live container + repo at /testbed (root setup, then chown agent).
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
                run_cmd = ["docker", "run", "-d", "--name", container, "-w", W]
                for k, v in auth.items():
                    run_cmd += ["-e", f"{k}={v}"]
                run_cmd += ["-e", "DISABLE_AUTOUPDATER=1", self.base_image, "tail", "-f", "/dev/null"]
                subprocess.run(run_cmd, check=True, timeout=600)
                subprocess.run(["docker", "exec", container, "mkdir", "-p", W], check=True, timeout=60)
                subprocess.run(["docker", "cp", f"{repo_src}/.", f"{container}:{W}/"], check=True, timeout=600)
                subprocess.run(["docker", "exec", container, "chown", "-R", "agent:agent", W],
                               check=True, timeout=120)

                # 2) Run the agent: sets up the env AND writes /testbed/Dockerfile.gen.
                model = _normalize_model(self.llm)
                max_budget = os.environ.get("CLAUDE_MAX_BUDGET_USD", "2.0")
                prompt = build_prompt(full_name, DOCKERFILE_BASE)
                claude_cmd = [
                    "docker", "exec", "-u", "agent", "-w", W, container,
                    "claude", "-p", prompt,
                    "--permission-mode", "bypassPermissions",
                    "--max-budget-usd", str(max_budget),
                    "--model", model,
                    "--output-format", "stream-json", "--verbose",
                ]
                # produce-only skips the two in-build pytest runs, so no PYTEST_RESERVE carve-out.
                agent_budget = max(60, self.timeout - int(time.time() - start))
                agent_stdout, agent_stderr = "", ""
                try:
                    proc = subprocess.run(claude_cmd, capture_output=True, text=True, timeout=agent_budget)
                    agent_stdout, agent_stderr = proc.stdout or "", proc.stderr or ""
                except subprocess.TimeoutExpired as e:
                    meta["agent_timed_out"] = True
                    agent_stdout, agent_stderr = _as_text(e.stdout), _as_text(e.stderr)
                self._write_agent_logs(out_dir, agent_stdout, agent_stderr, meta)
                self._check_timeout(start, "agent")

                # 3) Pull the agent's Dockerfile.gen out of the live container (no build/score).
                dockerfile = self._copy_dockerfile(container, ctx)
                if not dockerfile:
                    return {"status": "error", "failure_reason": "no_dockerfile",
                            "error": "agent wrote no /testbed/Dockerfile.gen", **ok, **meta}
                dockerfile = ensure_pytest(dockerfile)
                return self._finish_produce_only(full_name, out_dir, dockerfile, meta, ok, start)

            except TimeoutException:
                return {"status": "timeout", "failure_reason": "agent_timeout",
                        "error": "exceeded per-repo timeout", **ok, **meta}
            except subprocess.TimeoutExpired as e:
                return {"status": "timeout", "failure_reason": "docker_timeout", "error": str(e), **ok, **meta}
            except subprocess.CalledProcessError as e:
                return {"status": "error", "failure_reason": "docker_error", "error": str(e), **ok, **meta}
            except Exception as e:
                return {"status": "error", "failure_reason": "repo_error", "error": str(e), **ok, **meta}
            finally:
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
        except KeyboardInterrupt:
            subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
            raise

    def _finish_produce_only(self, full_name, out_dir, dockerfile, meta, ok, start):
        """PRODUCE-ONLY finish: write the env packet + run_produced.json marker, no docker.

        Reuses the shared producer contract (producers.base.write_env_packet) so the on-disk packet
        is identical to what a standalone ClaudeCodeDockerfileProducer would land."""
        _root = _resolve_producers_root()
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from producers.base import ProducedEnv, write_env_packet
        from bench.schema import RepoSpec

        produce_s = round(time.time() - start, 2)
        env = ProducedEnv(
            repo=RepoSpec(full_name, f"https://github.com/{full_name}"),
            dockerfile=dockerfile,
            base_image=meta.get("base_image"),
            head_sha=meta.get("head_sha") or "",
            status="produced",
            conformance="native",
            producer_name="claudecode-dockerfile",
            economy={"produce_s": produce_s},
        )
        write_env_packet(os.path.join(self.root_path, "output"), env)
        with open(os.path.join(out_dir, "run_produced.json"), "w") as f:
            json.dump({"status": "produced", "conformance": "native",
                       "base_image": meta.get("base_image"), "produce_s": produce_s}, f, indent=2)
        meta["produce_s"] = produce_s
        return {"status": "success", "produced": True, **ok, **meta}

    def _predict_inline(self, full_name: str) -> dict:
        """ESCAPE HATCH (CLAUDECODE_INLINE_SCORE=1): the ORIGINAL inline in-build+build+score path,
        verbatim — the docker-cleanup finally covers the FULL flow, so an early failure or a
        KeyboardInterrupt before the build still removes the image/containers."""
        start = time.time()
        slug = full_name.lower().replace("/", "-")
        container = f"claudecode-dockerfile-{slug}"          # ENV 1: live agent sandbox
        image = f"claudecode-dockerfile-eval-{slug}"          # ENV 2: built artifact image
        run_container = f"claudecode-dockerfile-evalrun-{slug}"  # ENV 2: pytest container
        out_dir = f"{self.root_path}/output/{full_name}"
        ctx = f"{out_dir}/eval_build"                         # clean build context (Dockerfile only)
        ok = {"root_path": self.root_path, "full_name": full_name}
        meta = {"requested_model": self.llm, "base_image": self.base_image,
                "dockerfile_base": DOCKERFILE_BASE}

        auth = {k: os.environ[k] for k in AUTH_KEYS if os.environ.get(k)}
        if not auth:
            return {"status": "error", "failure_reason": "no_auth",
                    "error": "set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY", **ok, **meta}

        try:
            try:
                init_output_and_repo(self.root_path, full_name, renew=True)
                download_repo(self.root_path, full_name, has_issue=False, use_repo_dockerfile=False)
                repo_src = f"{self.root_path}/input/repo/{full_name}"
                os.makedirs(out_dir, exist_ok=True)
                os.makedirs(ctx, exist_ok=True)
                self._check_timeout(start, "clone")

                # 1) ENV 1: live container + repo at /testbed (root setup, then chown agent).
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
                run_cmd = ["docker", "run", "-d", "--name", container, "-w", W]
                for k, v in auth.items():
                    run_cmd += ["-e", f"{k}={v}"]
                run_cmd += ["-e", "DISABLE_AUTOUPDATER=1", self.base_image, "tail", "-f", "/dev/null"]
                subprocess.run(run_cmd, check=True, timeout=600)
                subprocess.run(["docker", "exec", container, "mkdir", "-p", W], check=True, timeout=60)
                subprocess.run(["docker", "cp", f"{repo_src}/.", f"{container}:{W}/"], check=True, timeout=600)
                subprocess.run(["docker", "exec", container, "chown", "-R", "agent:agent", W],
                               check=True, timeout=120)

                # 2) Run the agent: sets up the env AND writes /testbed/Dockerfile.gen.
                model = _normalize_model(self.llm)
                max_budget = os.environ.get("CLAUDE_MAX_BUDGET_USD", "2.0")
                prompt = build_prompt(full_name, DOCKERFILE_BASE)
                claude_cmd = [
                    "docker", "exec", "-u", "agent", "-w", W, container,
                    "claude", "-p", prompt,
                    "--permission-mode", "bypassPermissions",
                    "--max-budget-usd", str(max_budget),
                    "--model", model,
                    "--output-format", "stream-json", "--verbose",
                ]
                agent_budget = max(60, self.timeout - int(time.time() - start) - PYTEST_RESERVE)
                agent_stdout, agent_stderr = "", ""
                try:
                    proc = subprocess.run(claude_cmd, capture_output=True, text=True, timeout=agent_budget)
                    agent_stdout, agent_stderr = proc.stdout or "", proc.stderr or ""
                except subprocess.TimeoutExpired as e:
                    meta["agent_timed_out"] = True
                    agent_stdout, agent_stderr = _as_text(e.stdout), _as_text(e.stderr)
                self._write_agent_logs(out_dir, agent_stdout, agent_stderr, meta)
                self._check_timeout(start, "agent")

                # 3) ENV 1: IN-BUILD verification (harness-run) → inbuild_*.json (SIGNAL A).
                subprocess.run(["docker", "exec", "-u", "agent", container, "mkdir", "-p", f"{W}/logs"],
                               check=True, timeout=60)
                subprocess.run(["docker", "cp", RPC, f"{container}:/run_pytest_collect.py"], check=True, timeout=120)
                subprocess.run(["docker", "cp", RP, f"{container}:/run_pytest.py"], check=True, timeout=120)
                subprocess.run(["docker", "exec", "-u", "agent", "-w", W, container,
                                "python3", "/run_pytest_collect.py"], check=False, timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker", "cp", f"{container}:{W}/logs/run_pytest_collect_results.json",
                                f"{out_dir}/inbuild_run_pytest_collect_results.json"], check=False, timeout=120)
                subprocess.run(["docker", "exec", "-u", "agent", "-w", W, container,
                                "python3", "/run_pytest.py"], check=False, timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker", "cp", f"{container}:{W}/logs/run_pytest_results.json",
                                f"{out_dir}/inbuild_run_pytest_results.json"], check=False, timeout=120)
                build_success, test_success = parse_inbuild(out_dir)
                meta["inbuild_build_success"] = build_success
                meta["inbuild_test_success"] = test_success
                self._check_timeout(start, "inbuild")

                # 4) Write the agent-signal instance JSON BEFORE the dockerfile/build steps so
                #    it exists for EVERY outcome (no_dockerfile, build_failed included).
                write_instance_json(out_dir, full_name, build_success, test_success)

                # 5) Pull the agent's Dockerfile.gen out of the live container.
                dockerfile = self._copy_dockerfile(container, ctx)
                if not dockerfile:
                    return {"status": "error", "failure_reason": "no_dockerfile",
                            "error": "agent wrote no /testbed/Dockerfile.gen", **ok, **meta}

                # 6) ENV 2: build the reusable artifact FRESH from a clean base.
                dockerfile = ensure_pytest(dockerfile)
                with open(f"{ctx}/Dockerfile", "w") as f:
                    f.write(dockerfile)
                try:
                    subprocess.run(["docker", "build", "-t", image, ctx], check=True, timeout=3600)
                except subprocess.CalledProcessError as e:
                    return {"status": "error", "failure_reason": "build_failed",
                            "error": str(e), **ok, **meta}
                except subprocess.TimeoutExpired as e:
                    return {"status": "timeout", "failure_reason": "docker_timeout",
                            "error": str(e), **ok, **meta}

                # 7) ENV 2: eval in the built image → canonical run_pytest_results.json (SIGNAL B).
                subprocess.run(f"docker rm -f {run_container} >/dev/null 2>&1", shell=True)
                subprocess.run(["docker", "run", "-d", "--name", run_container, "-w", W,
                                "-v", f"{RP}:/run_pytest.py", "-v", f"{RPC}:/run_pytest_collect.py",
                                image, "tail", "-f", "/dev/null"], check=True, timeout=600)
                subprocess.run(["docker", "exec", run_container, "mkdir", "-p", f"{W}/logs"], check=True, timeout=600)
                subprocess.run(["docker", "exec", run_container, "python3", "/run_pytest_collect.py"],
                               check=False, timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker", "cp", f"{run_container}:{W}/logs/run_pytest_collect_results.json",
                                f"{out_dir}/run_pytest_collect_results.json"], check=True, timeout=600)
                subprocess.run(["docker", "exec", run_container, "python3", "/run_pytest.py"],
                               check=False, timeout=PYTEST_TIMEOUT)
                subprocess.run(["docker", "cp", f"{run_container}:{W}/logs/run_pytest_results.json",
                                f"{out_dir}/run_pytest_results.json"], check=True, timeout=600)
                return {"status": "success", "failure_reason": None, **ok, **meta}

            except TimeoutException:
                return {"status": "timeout", "failure_reason": "agent_timeout",
                        "error": "exceeded per-repo timeout", **ok, **meta}
            except subprocess.TimeoutExpired as e:
                return {"status": "timeout", "failure_reason": "docker_timeout", "error": str(e), **ok, **meta}
            except subprocess.CalledProcessError as e:
                return {"status": "error", "failure_reason": "docker_error", "error": str(e), **ok, **meta}
            except Exception as e:
                return {"status": "error", "failure_reason": "repo_error", "error": str(e), **ok, **meta}
            finally:
                subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
                subprocess.run(f"docker rm -f {run_container} >/dev/null 2>&1", shell=True)
                subprocess.run(f"docker rmi {image} >/dev/null 2>&1", shell=True)
        except KeyboardInterrupt:
            subprocess.run(f"docker rm -f {container} >/dev/null 2>&1", shell=True)
            subprocess.run(f"docker rm -f {run_container} >/dev/null 2>&1", shell=True)
            subprocess.run(f"docker rmi {image} >/dev/null 2>&1", shell=True)
            raise

    def _copy_dockerfile(self, container: str, ctx: str):
        """docker cp /testbed/Dockerfile.gen out; return its text, or None if absent/empty."""
        dst = f"{ctx}/Dockerfile.gen"
        try:
            subprocess.run(["docker", "cp", f"{container}:{DOCKERFILE_GEN_PATH}", dst],
                           check=True, timeout=120)
            with open(dst) as fh:
                text = fh.read().strip()
            return text or None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return None
