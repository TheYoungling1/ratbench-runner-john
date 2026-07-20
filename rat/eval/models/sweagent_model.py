#!/usr/bin/env python3
"""SWE-agent Model for Repository Analysis and Testing."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import weave

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from libkit.command import download_repo, init_output_and_repo
from libkit.sweagent_wrapper import SWEAgentWrapper
from libkit.utils.language_detector import detect_language

from eval.common.base_model import BaseEvalModel
from eval.common.utils import TimeoutException


class SWEAgentModel(BaseEvalModel):
    """
    SWE-agent Model for Repository Analysis and Testing

    This model uses SWE-agent for environment configuration instead of
    SetupAgent + CodeAgent, but reuses the existing test infrastructure
    for fair comparison.
    """

    llm: str
    num_turn: int
    save_mode: str
    swe_agent_cost_limit: float

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
        print(f"[SWE-agent] Processing: {full_name}")
        print(f"{'=' * 60}")

        code_environment = None
        try:
            try:
                # Step 1: Remove dangling images
                print("🧹 Cleaning up dangling images...")
                self._check_timeout(start_time, "cleanup")
                subprocess.run(
                    'docker rmi $(docker images --filter "dangling=true" -q) > /dev/null 2>&1',
                    shell=True,
                )

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
                    has_issue=False,
                    use_repo_dockerfile=True,
                )

                # Step 4: Detect language (fast, no LLM)
                print("🔍 Detecting repository language...")
                self._check_timeout(start_time, "detect language")
                repo_path = Path(self.root_path) / "input" / "repo" / full_name
                detected_language = detect_language(
                    full_name=full_name,
                    project_path=repo_path,
                    prefer_github_api=True,
                )
                print(f"   Detected language: {detected_language}")

                # Step 5: Run SWE-agent for environment setup (keep container running)
                print("🤖 Running SWE-agent for environment setup...")
                self._check_timeout(start_time, "run SWE-agent")

                swe_wrapper = SWEAgentWrapper(
                    full_name=full_name,
                    root_path=self.root_path,
                    llm_name=self.llm,
                    max_turns=self.num_turn,
                    timeout=self.timeout
                    - int(time.time() - start_time),  # Remaining time
                    per_instance_cost_limit=self.swe_agent_cost_limit,
                )

                # Run SWE-agent and keep container running
                swe_result = swe_wrapper.run(
                    language=detected_language, keep_container=True
                )

                # Get environment object
                swe_env = swe_result.get("env")
                if not swe_env:
                    print("❌ Failed to obtain SWE-agent environment object")
                    return {
                        "status": "error",
                        "root_path": self.root_path,
                        "full_name": full_name,
                        "language": detected_language,
                    }

                # Save SWE-agent results
                output_dir = f"{self.root_path}/output/{full_name}"
                swe_result_path = f"{output_dir}/sweagent_result.json"
                # Remove non-serializable objects
                swe_result_to_save = {
                    k: v for k, v in swe_result.items() if k not in ["env", "runner"]
                }
                with open(swe_result_path, "w", encoding="utf-8") as f:
                    json.dump(swe_result_to_save, f, ensure_ascii=False, indent=2)

                # Extract statistics
                swe_stats = swe_wrapper.extract_statistics(swe_result)
                print(f"\n📊 SWE-agent stats:")
                print(f"   - Success: {swe_stats['success']}")
                print(f"   - Execution time: {swe_stats['execution_time']:.2f}s")
                print(f"   - Actions: {swe_stats['num_actions']}")
                print(f"   - Estimated cost: ${swe_stats['cost_estimate']:.4f}")

                if not swe_result["success"]:
                    print("⚠️  SWE-agent did not complete setup successfully")
                    # Continue with tests even if SWE-agent setup fails

                # Step 6: Copy test tools into SWE-agent container
                print("\n📦 Copying test tools into container...")
                self._check_timeout(start_time, "copy tools")

                try:
                    # Copy tools directory into the container
                    tools_src = f"{self.root_path}/libkit/tools"

                    # Create destination directory
                    try:
                        swe_env.communicate("mkdir -p /home/tools", timeout=10)
                    except Exception as e:
                        print(f"   ⚠️  Warning while creating /home/tools: {e}")

                    # Copy tool files
                    tool_files_copied = 0
                    for tool_file in os.listdir(tools_src):
                        if tool_file.endswith(".py"):
                            tool_path = os.path.join(tools_src, tool_file)
                            try:
                                with open(tool_path, "r", encoding="utf-8") as f:
                                    content = f.read()
                                swe_env.write_file(f"/home/tools/{tool_file}", content)
                                tool_files_copied += 1
                            except Exception as e:
                                print(f"   ⚠️  Failed to copy {tool_file}: {e}")

                    print(f"   ✅ Copied {tool_files_copied} tool files into container")

                    # Set permissions
                    try:
                        swe_env.communicate("chmod -R 755 /home/tools", timeout=10)
                    except Exception as e:
                        print(f"   ⚠️  Warning while setting permissions: {e}")

                except Exception as e:
                    print(f"   ⚠️  Failed to copy tools into container: {e}")
                    import traceback

                    traceback.print_exc()

                # Step 7: Run test suite inside SWE-agent container
                print("\n🧪 Running test suite in SWE-agent container...")
                self._check_timeout(start_time, "run tests")

                # Get language config and test tools
                from libkit.language_configs import get_language_config

                language_config = get_language_config(detected_language)

                # Determine repo path inside the container.
                # In SWE-agent images it's /{repo_name}, e.g. /face_recognition
                if swe_env.repo and hasattr(swe_env.repo, "repo_name"):
                    # repo_name may be "ageitgey/face_recognition"; we only need the suffix
                    repo_name = swe_env.repo.repo_name
                    # If it contains '/', take the last segment
                    if "/" in repo_name:
                        repo_name = repo_name.split("/")[-1]
                    repo_dir = f"/{repo_name}"
                else:
                    # Fallback: derive from full_name
                    repo_name = full_name.split("/")[-1]
                    repo_dir = f"/{repo_name}"

                print(f"   📁 Repo path in container: {repo_dir}")

                # Run commands via SWE-agent deployment
                test_runners = language_config.get_test_runner_tools()
                print(
                    f"   Found {len(test_runners)} test runners: {list(test_runners.keys())}"
                )

                test_results = {}
                for tool_name, tool_config in test_runners.items():
                    try:
                        print(f"   🔧 Running {tool_name}...")

                        # Get script filename
                        script = tool_config.get("script", "")
                        if not script:
                            print(f"      ⚠️  {tool_name} has no script configured")
                            continue

                        # Build command: python /home/tools/run_pytest.py
                        command = f"python /home/tools/{script}"
                        full_command = f"cd {repo_dir} && {command}"

                        print(f"      📋 Command: {full_command}")

                        try:
                            output = swe_env.communicate(
                                full_command, timeout=tool_config.get("timeout", 300)
                            )
                            test_results[tool_name] = {
                                "output": output,
                                "success": True,
                            }
                            print(f"      ✅ {tool_name} succeeded")
                            print(f"         First 200 chars: {output[:200]}")
                        except Exception as cmd_error:
                            error_msg = str(cmd_error)
                            test_results[tool_name] = {
                                "output": error_msg,
                                "success": False,
                            }
                            print(f"      ⚠️  {tool_name} error: {error_msg[:100]}")

                    except Exception as e:
                        print(f"      ❌ {tool_name} failed: {e}")
                        test_results[tool_name] = {
                            "output": str(e),
                            "success": False,
                        }

                # Save test results
                test_results_path = f"{output_dir}/test_results.json"
                with open(test_results_path, "w", encoding="utf-8") as f:
                    json.dump(test_results, f, ensure_ascii=False, indent=2)

                print("\n📊 Test results summary:")
                successful = sum(1 for r in test_results.values() if r.get("success"))
                print(f"   - Success: {successful}/{len(test_results)}")

                # Step 8: Copy test result files
                print("\n📋 Copying test result files...")
                self._copy_test_results_from_swe_env(
                    swe_env, language_config, full_name, repo_dir
                )

                # Step 9: Stop container
                print("\n🛑 Stopping SWE-agent container...")
                try:
                    swe_env.close()
                except Exception as e:
                    print(f"   ⚠️  Failed to close container: {e}")

                # Done
                execution_time = round(time.time() - start_time, 2)
                print(f"✅ Completed. Total time: {execution_time}s")

                return {
                    "status": "success",
                    "root_path": self.root_path,
                    "full_name": full_name,
                    "language": detected_language,
                    "swe_agent_stats": swe_stats,
                    "test_results": test_results,
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
                import traceback

                traceback.print_exc()
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
            print(f"⏱️  Total time: {execution_time}s")

    def _copy_test_results_from_swe_env(
        self, swe_env, language_config, full_name, repo_dir="/repo"
    ):
        """Copy test results from a SWE-agent env to the host output directory.

        Args:
            swe_env: SWE-agent environment object.
            language_config: Language config object.
            full_name: Full repo name.
            repo_dir: Repo path inside container, e.g. /face_recognition.
        """
        files_to_copy = language_config.get_test_result_files()
        output_dir = f"{self.root_path}/output/{full_name}"
        os.makedirs(output_dir, exist_ok=True)

        for file_info in files_to_copy:
            try:
                # Replace /repo with the actual repo_dir
                source_path = file_info["source"].replace("/repo", repo_dir)
                dest_path = f"{output_dir}/{file_info['dest_name']}"

                # Use SWE-agent read_file API
                try:
                    content = swe_env.read_file(source_path)
                    with open(dest_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    print(f"   ✅ Copied {file_info['description']}: {source_path}")
                except FileNotFoundError:
                    print(f"   ℹ️  {file_info['description']} not found: {source_path}")
                except Exception as e:
                    print(f"   ⚠️  Failed to copy {file_info['description']}: {e}")
            except Exception as e:
                print(f"   ⚠️  Copy error: {e}")
