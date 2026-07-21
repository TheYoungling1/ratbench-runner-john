#!/usr/bin/env python3
"""Repo2Run Model - Baseline using Repo2Run agent for environment configuration."""

import json
import os
import re
import subprocess
import sys
import time

import weave

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from libkit.command import init_output_and_repo

from eval.common.base_model import BaseEvalModel
from eval.common.utils import TimeoutException

# PRODUCE-ONLY is the DEFAULT (design §3 method 3): predict() runs repo2run, RE-HOMES its
# Dockerfile (mv /repo -> /testbed) into eval_build/Dockerfile + _meta.json{status:"produced",
# conformance:"rehomed"} and stops — bench/ rebuilds and scores in a fresh container. The raw
# repo2run Dockerfile is removed so harvest measures the RE-HOMED one, not the /repo-homed raw
# (which would score an empty /testbed — the false green this fixes).
# REPO2RUN_INLINE_SCORE=1 restores the legacy inline build+pytest+score path (escape hatch).
INLINE = os.environ.get("REPO2RUN_INLINE_SCORE") == "1"


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


def _write_repo2run_summary(output_dir, full_name, status, start_time, error=None):
    """Emit agent_run_summary.json so repo2run's tokens (track.json) + in-container pytest
    surface as token_usage + best_in_sandbox_test_result. Best-effort; never raises."""
    try:
        import os
        import json
        cost_tokens = None
        tp = os.path.join(output_dir, "track.json")
        if os.path.exists(tp):
            try:
                with open(tp) as fh:
                    track = json.load(fh)
                cost_tokens = next((e["cost_tokens"] for e in reversed(track)
                                    if isinstance(e, dict) and "cost_tokens" in e), None)
            except Exception:
                cost_tokens = None
        best = None
        rp = os.path.join(output_dir, "run_pytest_results.json")
        if os.path.exists(rp):
            try:
                with open(rp) as fh:
                    s = (json.load(fh).get("summary") or {})
                total = s.get("total_tests", 0) or 0
                passed = s.get("passed", 0) or 0
                skipped = s.get("skipped", 0) or 0
                failed = s.get("failed", 0) or 0
                errors = s.get("errors", 0) or 0
                eff = total - skipped
                pass_rate = (passed / eff) if eff > 0 else ((passed / total) if total > 0 else None)
                success = (passed > 0) and (failed == 0) and (errors == 0)
                best = {"command": "pytest", "success": bool(success), "pass_rate": pass_rate}
            except Exception:
                best = None
        summary = {
            "source": "repo2run",
            "status": status,
            "error": error,
            "best_in_sandbox_test_result": best,
            "token_usage": ({"total_tokens": cost_tokens} if cost_tokens is not None else None),
        }
        os.makedirs(output_dir, exist_ok=True)
        with open(os.path.join(output_dir, "agent_run_summary.json"), "w") as f:
            json.dump(summary, f, indent=2)
    except Exception:
        pass


class Repo2RunModel(BaseEvalModel):
    """Repo2Run evaluation model (baseline using Repo2Run config agent)."""

    llm: str
    num_turn: int  # max_turn for Repo2Run Configuration agent

    @weave.op
    def predict(self, full_name: str) -> dict:
        # PRODUCE-ONLY is the default; REPO2RUN_INLINE_SCORE=1 selects the legacy inline path.
        # The two paths are kept SEPARATE so the escape hatch is byte-identical to the original
        # (its docker-cleanup finally covers the FULL flow) while produce-only stays docker-free.
        if INLINE:
            return self._predict_inline(full_name)
        return self._predict_produce_only(full_name)

    def _predict_produce_only(self, full_name: str) -> dict:
        """DEFAULT (design §3 method 3): run repo2run, RE-HOME its Dockerfile to /testbed, emit the
        env packet. NO docker build, NO pytest, NO scoring — bench/ rebuilds and scores.

        Steps 1-4 mirror the inline path (init -> clone -> SHA -> run Repo2Run -> read Dockerfile),
        then the raw /repo-homed Dockerfile is re-homed (`mv /repo -> /testbed`) and written to
        eval_build/Dockerfile via write_env_packet. The RAW output/<name>/Dockerfile is REMOVED so
        bench.harvest._find_dockerfile (which checks repo_dir/Dockerfile BEFORE eval_build/) measures
        the re-homed one, not the raw (an empty /testbed => the false green this fixes).
        """
        start_time = time.time()
        repo_path = f"{self.root_path}/input/repo/{full_name}"
        output_dir = f"{self.root_path}/output/{full_name}"
        ok = {"root_path": self.root_path, "full_name": full_name}
        meta = {"requested_model": self.llm, "base_image": None, "head_sha": ""}
        try:
            init_output_and_repo(self.root_path, full_name, renew=True)
            self._check_timeout(start_time, "init output")

            # Clone (shallow) + record SHA — same as the inline path.
            subprocess.run(
                f"git clone --depth=1 https://github.com/{full_name}.git {repo_path}",
                shell=True, check=True)
            sha = subprocess.run("git rev-parse HEAD", cwd=repo_path, shell=True, check=True,
                                 capture_output=True, text=True).stdout.strip()
            meta["head_sha"] = sha

            # Run Repo2Run -> output/<name>/Dockerfile (the raw, /repo-homed artifact).
            os.makedirs(f"{self.root_path}/utils/repo", exist_ok=True)
            repo2run_main = os.path.join(self.root_path, "Repo2Run/build_agent/main.py")
            if not os.path.exists(repo2run_main):
                raise Exception(f"Repo2Run entrypoint not found: {repo2run_main}.")
            remaining_timeout = max(60, self.timeout - (time.time() - start_time))
            result = subprocess.run(
                ["python3", repo2run_main, "--full_name", full_name, "--sha", sha,
                 "--root_path", self.root_path, "--num_turn", str(self.num_turn),
                 "--llm", self.llm],
                timeout=remaining_timeout, capture_output=False)
            if result.returncode != 0:
                raise Exception(f"Repo2Run failed (exit code: {result.returncode})")

            dockerfile_path = f"{output_dir}/Dockerfile"
            if not os.path.exists(dockerfile_path):
                return {"status": "error", "failure_reason": "no_dockerfile",
                        "error": "Repo2Run did not generate a Dockerfile", **ok, **meta}
            with open(dockerfile_path) as fh:
                raw_dockerfile = fh.read()
            base = re.search(r"^\s*FROM\s+(\S+)", raw_dockerfile, re.MULTILINE)
            if base:
                meta["base_image"] = base.group(1)

            return self._finish_produce_only(full_name, output_dir, raw_dockerfile, meta, ok,
                                             start_time)
        except subprocess.TimeoutExpired as e:
            _write_repo2run_summary(output_dir, full_name, "timeout", start_time, str(e))
            return {"status": "timeout", "failure_reason": "docker_timeout", "error": str(e),
                    **ok, **meta}
        except TimeoutException as e:
            _write_repo2run_summary(output_dir, full_name, "timeout", start_time, str(e))
            return {"status": "timeout", "failure_reason": "agent_timeout", "error": str(e),
                    **ok, **meta}
        except Exception as e:
            _write_repo2run_summary(output_dir, full_name, "error", start_time, str(e))
            return {"status": "error", "failure_reason": "repo_error", "error": str(e),
                    **ok, **meta}

    def _finish_produce_only(self, full_name, output_dir, raw_dockerfile, meta, ok, start_time):
        """PRODUCE-ONLY finish: re-home the Dockerfile, write the env packet + run_produced.json,
        remove the raw /repo-homed Dockerfile. Reuses the shared producer contract so the on-disk
        packet is identical to what a standalone Repo2RunProducer would land."""
        _root = _resolve_producers_root()
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from producers.base import ProducedEnv, write_env_packet
        from producers.repo2run import rehome_dockerfile
        from bench.schema import RepoSpec

        rehomed = rehome_dockerfile(raw_dockerfile)
        produce_s = round(time.time() - start_time, 2)
        env = ProducedEnv(
            repo=RepoSpec(full_name, f"https://github.com/{full_name}"),
            dockerfile=rehomed,
            base_image=meta.get("base_image"),
            head_sha=meta.get("head_sha") or "",
            status="produced",
            conformance="rehomed",
            producer_name="repo2run",
            economy={"produce_s": produce_s},
        )
        write_env_packet(os.path.join(self.root_path, "output"), env)
        # FIX A — the re-homed eval_build/Dockerfile MUST be the ONLY Dockerfile harvest can find.
        # harvest._find_dockerfile checks output_dir/Dockerfile BEFORE eval_build/Dockerfile, so a
        # surviving RAW /repo-homed Dockerfile would shadow the re-homed one and repo2run stays
        # non_conforming. Remove it and VERIFY it is gone before claiming success — if it still
        # exists (or can't be removed), fail LOUD rather than report a false produced-success.
        raw_path = os.path.join(output_dir, "Dockerfile")
        try:
            os.remove(raw_path)
        except OSError:
            pass
        meta["produce_s"] = produce_s
        if os.path.exists(raw_path):
            return {"status": "error", "failure_reason": "raw_dockerfile_not_removed",
                    "error": f"raw repo2run Dockerfile still present at {raw_path}; harvest would "
                             "measure the /repo-homed raw instead of the re-homed one",
                    **ok, **meta}
        with open(os.path.join(output_dir, "run_produced.json"), "w") as f:
            json.dump({"status": "produced", "conformance": "rehomed",
                       "base_image": meta.get("base_image"), "produce_s": produce_s}, f, indent=2)
        return {"status": "success", "produced": True, **ok, **meta}

    def _predict_inline(self, full_name: str) -> dict:
        """ESCAPE HATCH (REPO2RUN_INLINE_SCORE=1): the ORIGINAL inline build+pytest+score path,
        verbatim — the docker-cleanup finally covers the FULL flow, so an early failure or a
        KeyboardInterrupt before the build still removes the image/container."""
        start_time = time.time()

        print(f"\n{'=' * 60}")
        print(f"Processing: {full_name} (Repo2Run)")
        print(f"{'=' * 60}")

        # Container / image names
        container_name = f"repo2run-{full_name.lower().replace('/', '-')}"
        image_name = f"repo2run-eval-{full_name.lower().replace('/', '-')}"

        # Paths
        repo_path = f"{self.root_path}/input/repo/{full_name}"
        output_dir = f"{self.root_path}/output/{full_name}"
        repo2run_repo_path = f"{self.root_path}/utils/repo/{full_name}"

        try:
            try:
                # Step 1: Init output
                print("📁 Initializing output directory...")
                init_output_and_repo(self.root_path, full_name, renew=True)
                self._check_timeout(start_time, "init output")

                # Step 2: Download repo and get SHA
                print("📥 Downloading repository and getting SHA...")
                self._check_timeout(start_time, "download repo")

                # Clone to the standard location
                clone_cmd = f"git clone --depth=1 https://github.com/{full_name}.git {repo_path}"
                subprocess.run(clone_cmd, shell=True, check=True)

                # Get current HEAD SHA
                sha_result = subprocess.run(
                    "git rev-parse HEAD",
                    cwd=repo_path,
                    shell=True,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                sha = sha_result.stdout.strip()
                print(f"   SHA: {sha}")

                # Step 3: Run Repo2Run agent
                print("🤖 Running Repo2Run Configuration Agent...")
                self._check_timeout(start_time, "run Repo2Run")

                # Repo2Run expects to download into utils/repo/{full_name}
                utils_repo_dir = f"{self.root_path}/utils/repo"
                os.makedirs(utils_repo_dir, exist_ok=True)

                # Invoke Repo2Run
                repo2run_main = os.path.join(
                    self.root_path, "Repo2Run/build_agent/main.py"
                )

                if not os.path.exists(repo2run_main):
                    raise Exception(
                        f"Repo2Run entrypoint not found: {repo2run_main}. "
                        "Make sure Repo2Run is checked out in the expected location."
                    )

                repo2run_cmd = [
                    "python3",
                    repo2run_main,
                    "--full_name",
                    full_name,
                    "--sha",
                    sha,
                    "--root_path",
                    self.root_path,
                    "--num_turn",
                    str(self.num_turn),
                    "--llm",
                    self.llm,
                ]

                # Use remaining time as Repo2Run timeout
                elapsed = time.time() - start_time
                remaining_timeout = max(60, self.timeout - elapsed)

                print(f"   Command: {' '.join(repo2run_cmd)}")
                print(f"   Timeout: {remaining_timeout}s")

                result = subprocess.run(
                    repo2run_cmd,
                    timeout=remaining_timeout,
                    capture_output=False,  # Stream output directly
                )

                if result.returncode != 0:
                    raise Exception(f"Repo2Run failed (exit code: {result.returncode})")

                # Step 4: Validate Repo2Run outputs
                print("🔍 Checking Repo2Run outputs...")
                self._check_timeout(start_time, "check outputs")

                track_json = f"{output_dir}/track.json"
                dockerfile_path = f"{output_dir}/Dockerfile"

                if not os.path.exists(track_json):
                    raise Exception(
                        "Repo2Run did not generate track.json; configuration likely failed."
                    )

                if not os.path.exists(dockerfile_path):
                    raise Exception(
                        "Repo2Run did not generate Dockerfile; environment setup may be incomplete."
                    )

                print(f"   ✓ track.json: {track_json}")
                print(f"   ✓ Dockerfile: {dockerfile_path}")

                # Step 5: Build Docker image
                print("🐳 Building Docker image...")
                self._check_timeout(start_time, "build image")

                # Repo2Run puts the generated Dockerfile in output_dir.
                # It already includes git clone and dependency installation.
                build_cmd = ["docker", "build", "-t", image_name, output_dir]
                subprocess.run(build_cmd, check=True)

                # Step 6: Run test container
                print("🧪 Running test container...")
                self._check_timeout(start_time, "run tests")

                # Remove any existing container
                subprocess.run(
                    f"docker rm -f {container_name} > /dev/null 2>&1", shell=True
                )

                # Mount test tools
                run_pytest_tool_path = os.path.join(
                    self.root_path, "libkit/tools/run_pytest.py"
                )
                run_pytest_collect_tool_path = os.path.join(
                    self.root_path, "libkit/tools/run_pytest_collect.py"
                )

                if not os.path.exists(run_pytest_tool_path):
                    raise Exception(
                        f"run_pytest.py tool not found: {run_pytest_tool_path}"
                    )
                if not os.path.exists(run_pytest_collect_tool_path):
                    raise Exception(
                        f"run_pytest_collect.py tool not found: {run_pytest_collect_tool_path}"
                    )

                # Start container (Repo2Run Dockerfile WORKDIR is /)
                run_cmd = [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    container_name,
                    "-w",
                    "/repo",
                    "-v",
                    f"{run_pytest_tool_path}:/run_pytest.py",
                    "-v",
                    f"{run_pytest_collect_tool_path}:/run_pytest_collect.py",
                    image_name,
                    "tail",
                    "-f",
                    "/dev/null",
                ]
                subprocess.run(run_cmd, check=True)

                # Ensure logs directory exists
                exec_mkdir = [
                    "docker",
                    "exec",
                    container_name,
                    "mkdir",
                    "-p",
                    "/repo/logs",
                ]
                subprocess.run(exec_mkdir, check=True)

                # Run pytest collect
                print("🔍 Running pytest collect...")
                exec_collect = [
                    "docker",
                    "exec",
                    container_name,
                    "python3",
                    "/run_pytest_collect.py",
                ]
                subprocess.run(exec_collect, check=False)

                # Copy collect results
                print("📤 Copying collect results...")
                res_collect_json_container = (
                    "/repo/logs/run_pytest_collect_results.json"
                )
                res_collect_json_host = os.path.join(
                    output_dir, "run_pytest_collect_results.json"
                )
                cp_cmd_collect = [
                    "docker",
                    "cp",
                    f"{container_name}:{res_collect_json_container}",
                    res_collect_json_host,
                ]
                subprocess.run(cp_cmd_collect, check=True)

                # Run pytest
                print("🧪 Running pytest...")
                exec_test = [
                    "docker",
                    "exec",
                    container_name,
                    "python3",
                    "/run_pytest.py",
                ]
                subprocess.run(exec_test, check=False)

                # Copy test results
                print("📤 Copying test results...")
                res_json_container = "/repo/logs/run_pytest_results.json"
                res_json_host = os.path.join(output_dir, "run_pytest_results.json")

                cp_cmd = [
                    "docker",
                    "cp",
                    f"{container_name}:{res_json_container}",
                    res_json_host,
                ]
                subprocess.run(cp_cmd, check=True)

                execution_time = round(time.time() - start_time, 2)
                print(f"✅ Completed. Time: {execution_time}s")

                _write_repo2run_summary(output_dir, full_name, "success", start_time)
                return {
                    "status": "success",
                    "root_path": self.root_path,
                    "full_name": full_name,
                }

            except subprocess.TimeoutExpired as e:
                print(f"⏱️  Timeout: {e}")
                _write_repo2run_summary(output_dir, full_name, "timeout", start_time, str(e))
                return {
                    "status": "timeout",
                    "root_path": self.root_path,
                    "full_name": full_name,
                }
            except TimeoutException as e:
                print(f"⏱️  Timeout: {e}")
                _write_repo2run_summary(output_dir, full_name, "timeout", start_time, str(e))
                return {
                    "status": "timeout",
                    "root_path": self.root_path,
                    "full_name": full_name,
                }
            except subprocess.CalledProcessError as e:
                print(f"❌ Process execution failed: {e}")
                _write_repo2run_summary(output_dir, full_name, "error", start_time, str(e))
                return {
                    "status": "error",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "error": str(e),
                }
            except Exception as e:
                print(f"❌ Error: {e}")
                import traceback

                traceback.print_exc()
                _write_repo2run_summary(output_dir, full_name, "error", start_time, str(e))
                return {
                    "status": "error",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "error": str(e),
                }

            finally:
                # Cleanup container and image
                print("🧹 Cleaning up container and image...")
                subprocess.run(
                    f"docker rm -f {container_name} > /dev/null 2>&1", shell=True
                )
                subprocess.run(f"docker rmi {image_name} > /dev/null 2>&1", shell=True)

        except KeyboardInterrupt:
            print("\n⚠️  Interrupted...")
            subprocess.run(
                f"docker rm -f {container_name} > /dev/null 2>&1", shell=True
            )
            subprocess.run(f"docker rmi {image_name} > /dev/null 2>&1", shell=True)
            raise

        finally:
            # Compute duration
            execution_time = round(time.time() - start_time, 2)
            print(f"⏱️  Total time: {execution_time}s")
