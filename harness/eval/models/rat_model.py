#!/usr/bin/env python3
"""RAT (Repository Analysis and Test) Model - Our evaluation approach."""

import json
import subprocess
import sys
import time
import os

import weave

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from libkit.codeagent import CodeAgent
from libkit.command import (
    download_repo,
    get_default_namespace,
    init_output_and_repo,
)
from libkit.environment import Env

try:
    from libkit.ablation_config import ENABLE_SETUP_ABLATION
except ImportError:
    ENABLE_SETUP_ABLATION = False

if ENABLE_SETUP_ABLATION:
    from libkit.setupagent_ablation import SetupAgent
else:
    from libkit.setupagent import SetupAgent

from eval.common.base_model import BaseEvalModel
from eval.common.utils import TimeoutException


class RATModel(BaseEvalModel):
    """Repository Analysis and Test (RAT) evaluation model."""

    llm: str
    num_turn: int
    save_mode: str

    @weave.op
    def predict(self, full_name: str) -> dict:
        """
        Process a single repository and return its status.

        Args:
            repo: Repository info {"full_name": "...", "clone_url": "...", ...}

        Returns:
            {"status": "success" | "error" | "timeout", "language": "..."}
        """
        start_time = time.time()
        detected_language = "unknown"

        print(f"\n{'=' * 60}")
        print(f"Processing: {full_name}")
        print(f"{'=' * 60}")

        code_environment = None
        try:
            try:
                # parallel-safety: global dangling rmi removed; per-repo cleanup happens in finally

                # Step 2: Initialize output dir and repo
                print("📁 Initializing output directory...")
                self._check_timeout(start_time, "init output")
                init_output_and_repo(self.root_path, full_name, renew=False)

                # Step 3: Download repo
                print("📥 Downloading repository...")
                self._check_timeout(start_time, "download repo")
                download_repo(
                    self.root_path,
                    full_name,
                    use_repo_dockerfile=False,
                    use_pipreqs=True,
                )

                # Step 4: Run SetupAgent to analyze environment
                print("🔍 Analyzing repository environment...")
                self._check_timeout(start_time, "analyze environment")
                setup_agent = SetupAgent(full_name, self.root_path, self.llm)
                try:
                    setup_result = setup_agent.run()
                    detected_language = setup_result.get("language", "unknown")
                    base_image = setup_result.get(
                        "recommended_base_image",
                        get_default_namespace(detected_language),
                    )
                    dockerfile_content = setup_result.get("dockerfile_content", None)

                    print(f"   Recommended base image: {base_image}")
                finally:
                    # Ensure SetupAgent releases Docker client resources
                    setup_agent.cleanup()

                # Step 5: Create container environment
                print("🐳 Creating Docker container...")
                self._check_timeout(start_time, "create container")
                try:
                    code_environment = Env(
                        base_image,
                        detected_language,
                        full_name,
                        self.root_path,
                        save_mode=self.save_mode,
                        use_repo_dockerfile=False,
                        dockerfile_content=dockerfile_content,
                    )
                    code_environment.create_container()
                except Exception as e:
                    # If the recommended image fails, clean up and fall back to default
                    print(
                        f"⚠️  Failed to create with recommended image: {e}; trying default"
                    )
                    if code_environment:
                        try:
                            code_environment.stop_container()
                        except:
                            pass

                    base_image = get_default_namespace(detected_language)
                    code_environment = Env(
                        base_image,
                        detected_language,
                        full_name,
                        self.root_path,
                        save_mode=self.save_mode,
                        use_repo_dockerfile=False,
                    )
                    code_environment.create_container()

                # Step 6: Run CodeAgent
                print("🤖 Running CodeAgent...")
                self._check_timeout(start_time, "run CodeAgent")
                trajectory = []
                code_agent = CodeAgent(
                    code_environment,
                    base_image,
                    full_name,
                    self.root_path,
                    language=detected_language,
                    llm_name=self.llm,
                    max_turn=self.num_turn,
                )
                msg, outer_commands = code_agent.run(
                    "/tmp", trajectory=trajectory, hitl=False
                )

                # Step 7: Stop container
                print("🛑 Stopping container...")
                _ = code_environment.stop_container()

                # Step 8: Save trajectory
                output_dir = f"{self.root_path}/output/{full_name}"
                trajectory_path = f"{output_dir}/trajectory.json"
                with open(trajectory_path, "w", encoding="utf-8") as f:
                    json.dump(msg, f, ensure_ascii=False, indent=2)

                # Done
                execution_time = round(time.time() - start_time, 2)
                print(f"✅ Completed. Time: {execution_time}s")
                return {
                    "status": "success",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "language": detected_language,
                }

            except TimeoutException as e:
                print(f"⏱️  Timeout: {e}")
                return {
                    "status": "timeout",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "language": detected_language,
                }

            except subprocess.CalledProcessError as e:
                if "git clone" in str(e.cmd):
                    print(f"❌ Download failed: {e}")
                else:
                    print(f"❌ Process error: {e}")
                return {
                    "status": "error",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "language": detected_language,
                }

            except Exception as e:
                print(f"❌ Error: {e}")
                return {
                    "status": "error",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "language": detected_language,
                }

            finally:
                # Ensure cleanup of the current task container
                if code_environment and code_environment.container:
                    try:
                        print("🧹 Cleaning up current task container...")
                        code_environment.stop_container()
                    except Exception as cleanup_error:
                        print(f"⚠️  Failed to clean up container: {cleanup_error}")

        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted; cleaning up...")
            if code_environment and code_environment.container:
                try:
                    code_environment.stop_container()
                except:
                    pass
            raise

        finally:
            # Compute duration
            execution_time = round(time.time() - start_time, 2)
            print(f"⏱️  Time: {execution_time}s")
