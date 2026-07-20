#!/usr/bin/env python3
"""Installamatic Model for Repository Setup and Testing."""

import os
import subprocess
import time
import traceback
from pathlib import Path

import weave

# Import Installamatic components
from install_test.agent import GatherAgent, RepairAgent

from eval.common.base_model import BaseEvalModel
from eval.common.utils import TimeoutException
from libkit.command import download_repo, init_output_and_repo

INSTALLAMATIC_ROOT = Path(__file__).resolve().parent.parent.parent / "installamatic"


class InstallamaticModel(BaseEvalModel):
    """
    Installamatic Model for Repository Setup and Testing

    Uses Installamatic's gather and repair agents to automatically
    generate Dockerfiles and set up repository environments.
    """

    llm: str
    num_turn: int
    n_tries: int = 2
    max_gather_iterations: int = 50
    max_summarise_iterations: int = 20
    max_diagnosis_iterations: int = 20

    @weave.op
    def predict(self, full_name: str) -> dict:
        """
        Process a single repository and return its status.

        Args:
            repo: Repository info {"full_name": "...", ...}

        Returns:
            {"status": "success" | "error" | "timeout", ...}
        """
        start_time = time.time()
        print(f"\n" + "=" * 60)
        print(f"[Installamatic] Processing: {full_name}")
        print(f"=" * 60)

        # Prepare output dir
        init_output_and_repo(self.root_path, full_name, renew=True)

        # Download repo using libkit
        print("📥 Downloading repository...")
        try:
            download_repo(self.root_path, full_name, use_repo_dockerfile=False)
        except Exception as e:
            print(f"❌ Failed to download repository: {e}")
            return {
                "status": "error",
                "error": f"Download failed: {str(e)}",
                "full_name": full_name,
            }

        repo_abs_path = os.path.join(self.root_path, "input/repo", full_name)
        if not os.path.exists(repo_abs_path):
            return {
                "status": "error",
                "error": f"Repository not found: {repo_abs_path}",
                "full_name": full_name,
            }

        repo_url = repo_abs_path  # Pass local path as URL
        repo_name_simple = full_name.split("/")[-1]
        image_name = f"installamatic-{full_name.replace('/', '-').lower()}"
        container_name = f"{image_name}-test"

        original_cwd = os.getcwd()

        try:
            # Change CWD to Installamatic root for prompt loading etc
            os.chdir(INSTALLAMATIC_ROOT)

            # Create necessary directories for Installamatic
            os.makedirs("logs/dockerfiles", exist_ok=True)
            os.makedirs("logs/summary", exist_ok=True)

            # Use environment variables for LLM configuration
            if "DEEPSEEK_API_KEY" in os.environ and "OPENAI_API_KEY" not in os.environ:
                os.environ["OPENAI_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
            if (
                "DEEPSEEK_BASE_URL" in os.environ
                and "OPENAI_BASE_URL" not in os.environ
            ):
                os.environ["OPENAI_BASE_URL"] = os.environ["DEEPSEEK_BASE_URL"]

            # Step 1: Gather (Documentation gathering)
            print("🔍 Gathering docs...")
            self._check_timeout(start_time, "Gather")

            try:
                agent = GatherAgent(
                    model=self.llm,
                    system=GatherAgent.init_system_message(repo_url),
                    count_tokens=True,
                    prev_messages=None,
                )

                documents, contents = agent.gather(
                    repo_url, max_iterations=self.max_gather_iterations
                )
                print(f"   Collected {len(documents)} documents")

                agent.summarise(
                    repo_url,
                    documents,
                    contents,
                    max_iterations=self.max_summarise_iterations,
                )
            except Exception as e:
                print(f"❌ Installamatic Agent error (Gather): {e}")
                if os.getcwd() == INSTALLAMATIC_ROOT:
                    os.chdir(original_cwd)
                return {
                    "status": "error",
                    "error": f"Gather failed: {str(e)}",
                    "full_name": full_name,
                }

            # Generate Dockerfile
            print("📝 Generating Dockerfile...")
            try:
                dockerfile_content = agent.gen_dockerfile(repo_url, repo_name_simple)
            except Exception as e:
                print(f"❌ Installamatic Agent error (gen_dockerfile): {e}")
                if os.getcwd() == INSTALLAMATIC_ROOT:
                    os.chdir(original_cwd)
                return {
                    "status": "error",
                    "error": f"Failed to generate Dockerfile: {str(e)}",
                    "full_name": full_name,
                }

            # Step 2: Repair/Build
            print("🔧 Building and repairing Dockerfile...")
            self._check_timeout(start_time, "Repair")

            repair_agent = RepairAgent(
                model=self.llm,
                system=RepairAgent.init_system_message(repo_url, dockerfile_content),
                verbose=True,
            )

            try:
                status, attempts = repair_agent.repair_dockerfile(
                    url=repo_url,
                    dockerfile=dockerfile_content,
                    repo_name=repo_name_simple,
                    n_tries=self.n_tries,
                    image_name=image_name,
                    keep_image=True,
                    max_diagnosis_iterations=self.max_diagnosis_iterations,
                )
            except Exception as e:
                print(f"❌ Installamatic internal error: {e}")
                if os.getcwd() == INSTALLAMATIC_ROOT:
                    os.chdir(original_cwd)
                return {
                    "status": "error",
                    "error": f"Installamatic crashed: {str(e)}",
                    "full_name": full_name,
                }

            # Switch back CWD
            os.chdir(original_cwd)

            if status != "success":
                print(f"❌ Build failed after {attempts} attempts")
                return {
                    "status": "error",
                    "error": f"Build failed after {attempts} attempts",
                    "full_name": full_name,
                }

            # Verify image exists
            if (
                subprocess.run(
                    ["docker", "image", "inspect", image_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ).returncode
                != 0
            ):
                print(
                    f"❌ Error: Installamatic reported success but image {image_name} is missing (silent build failure)"
                )
                return {
                    "status": "error",
                    "error": "Silent build failure (image missing)",
                    "full_name": full_name,
                }

            print(f"✅ Build succeeded. Image: {image_name}")

            # Step 3: Run Tests (using libkit tools on the built image)
            print("🤖 Running tests...")
            self._check_timeout(start_time, "Testing")

            # Remove existing container if any
            subprocess.run(
                f"docker rm -f {container_name}", shell=True, stderr=subprocess.DEVNULL
            )

            run_pytest_tool = os.path.join(self.root_path, "libkit/tools/run_pytest.py")
            run_pytest_collect = os.path.join(
                self.root_path, "libkit/tools/run_pytest_collect.py"
            )

            # Start container
            run_cmd = [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-v",
                f"{run_pytest_tool}:/run_pytest.py",
                "-v",
                f"{run_pytest_collect}:/run_pytest_collect.py",
                image_name,
                "tail",
                "-f",
                "/dev/null",
            ]
            subprocess.run(run_cmd, check=True)

            # Detect WORKDIR
            workdir_res = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Config.WorkingDir}}",
                    container_name,
                ],
                capture_output=True,
                text=True,
            )
            workdir = workdir_res.stdout.strip()
            if not workdir or workdir == "/":
                workdir = "/app"  # Default fallback
            print(f"   Detected WORKDIR: {workdir}")

            output_dir = os.path.join(self.root_path, "output", full_name)
            os.makedirs(output_dir, exist_ok=True)

            # Create logs dir relative to WORKDIR
            logs_path = os.path.join(workdir, "logs")
            subprocess.run(
                ["docker", "exec", container_name, "mkdir", "-p", logs_path], check=True
            )

            # Collect
            print("   Running pytest collect...")
            subprocess.run(
                ["docker", "exec", container_name, "python3", "/run_pytest_collect.py"],
                check=False,
            )

            # Copy collect results
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container_name}:{logs_path}/run_pytest_collect_results.json",
                    f"{output_dir}/run_pytest_collect_results.json",
                ],
                check=False,
            )

            # Test
            print("   Running pytest...")
            subprocess.run(
                ["docker", "exec", container_name, "python3", "/run_pytest.py"],
                check=False,
            )

            # Copy test results
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container_name}:{logs_path}/run_pytest_results.json",
                    f"{output_dir}/run_pytest_results.json",
                ],
                check=False,
            )

            execution_time = round(time.time() - start_time, 2)
            print(f"✅ Evaluation completed. Time: {execution_time}s")

            return {
                "status": "success",
                "full_name": full_name,
                "root_path": self.root_path,
                "execution_time": execution_time,
            }

        except TimeoutException as e:
            if os.getcwd() == INSTALLAMATIC_ROOT:
                os.chdir(original_cwd)
            print(f"⏱️  Timeout: {e}")
            return {
                "status": "timeout",
                "error": str(e),
                "full_name": full_name,
                "root_path": self.root_path,
            }

        except Exception as e:
            if os.getcwd() == INSTALLAMATIC_ROOT:
                os.chdir(original_cwd)
            print(f"❌ Error: {e}")
            traceback.print_exc()
            return {
                "status": "error",
                "error": str(e),
                "full_name": full_name,
                "root_path": self.root_path,
            }

        finally:
            if os.getcwd() == INSTALLAMATIC_ROOT:
                os.chdir(original_cwd)

            print(f"🧹 Cleaning up resources: {full_name}...")
            # Remove container
            subprocess.run(
                f"docker rm -f {container_name}", shell=True, stderr=subprocess.DEVNULL
            )
            # Remove image to save space
            subprocess.run(
                f"docker image rm {image_name}", shell=True, stderr=subprocess.DEVNULL
            )
