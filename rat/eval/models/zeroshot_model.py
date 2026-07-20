#!/usr/bin/env python3
"""ZeroShot Model - Simple baseline using direct LLM Dockerfile generation."""

import os
import subprocess
import time
import traceback
from pathlib import Path
from typing import Optional

import weave

from eval.common.base_model import BaseEvalModel
from eval.common.utils import TimeoutException
from libkit.command import download_repo, init_output_and_repo
from libkit.llm import LLMChat


class ZeroShotModel(BaseEvalModel):
    """
    ZeroShot Model for Direct Dockerfile Generation

    This is a simple baseline that:
    1. Collects repository information (README, file structure, key files)
    2. Sends it to an LLM with truncation to prevent context overflow
    3. Asks the LLM to directly generate a Dockerfile
    4. Builds and tests the Docker image
    """

    llm: str
    max_context_tokens: int = 8000  # Maximum tokens for repository context

    def _collect_repo_context(self, repo_path: str) -> str:
        """
        Collect repository context including README, file structure, and important files.

        Args:
            repo_path: Path to the repository

        Returns:
            String containing repository context with truncation
        """
        context_parts = []
        char_budget = self.max_context_tokens * 3  # Rough estimate: ~3 chars per token

        # 1. Collect README
        readme_content = self._find_and_read_readme(repo_path)
        if readme_content:
            truncated_readme = self._truncate_text(readme_content, char_budget // 3)
            context_parts.append(f"=== README ===\n{truncated_readme}\n")
            char_budget -= len(truncated_readme)

        # 2. Collect file structure
        file_structure = self._get_file_structure(repo_path)
        truncated_structure = self._truncate_text(
            file_structure, min(char_budget // 3, 5000)
        )
        context_parts.append(f"=== FILE STRUCTURE ===\n{truncated_structure}\n")
        char_budget -= len(truncated_structure)

        # 3. Collect important files (requirements.txt, setup.py, package.json, etc.)
        important_files = self._collect_important_files(repo_path, char_budget)
        if important_files:
            context_parts.append(important_files)

        return "\n".join(context_parts)

    def _find_and_read_readme(self, repo_path: str) -> Optional[str]:
        """Find and read README file."""
        for readme_name in ["README.md", "README.rst", "README.txt", "README"]:
            readme_path = os.path.join(repo_path, readme_name)
            if os.path.exists(readme_path):
                try:
                    with open(readme_path, "r", encoding="utf-8", errors="ignore") as f:
                        return f.read()
                except Exception:
                    pass
        return None

    def _get_file_structure(self, repo_path: str, max_depth: int = 3) -> str:
        """Get repository file structure using tree command or fallback."""
        try:
            # Try using tree command with depth limit
            result = subprocess.run(
                ["tree", "-L", str(max_depth), "-F", "--dirsfirst"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        # Fallback: use find command
        try:
            result = subprocess.run(
                ["find", ".", "-maxdepth", str(max_depth), "-type", "f"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout
        except subprocess.TimeoutExpired:
            pass

        return "Unable to collect file structure"

    def _collect_important_files(self, repo_path: str, char_budget: int) -> str:
        """Collect content from important configuration files."""
        important_files = [
            "requirements.txt",
            "setup.py",
            "pyproject.toml",
            "environment.yml",
            "Pipfile",
            "package.json",
            "Cargo.toml",
            "pom.xml",
            "build.gradle",
        ]

        collected = []
        remaining_budget = char_budget

        for filename in important_files:
            if remaining_budget <= 0:
                break

            filepath = os.path.join(repo_path, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    truncated = self._truncate_text(
                        content, min(remaining_budget, 2000)
                    )
                    collected.append(f"=== {filename} ===\n{truncated}\n")
                    remaining_budget -= len(truncated)
                except Exception:
                    pass

        return "\n".join(collected) if collected else ""

    def _truncate_text(self, text: str, max_chars: int) -> str:
        """Truncate text to maximum character count."""
        if len(text) <= max_chars:
            return text
        return (
            text[:max_chars]
            + f"\n... (truncated, {len(text) - max_chars} chars omitted)"
        )

    def _generate_dockerfile_with_llm(self, repo_context: str, repo_name: str) -> str:
        """
        Use LLM to generate Dockerfile based on repository context.

        Args:
            repo_context: Repository context (README, structure, files)
            repo_name: Repository name

        Returns:
            Generated Dockerfile content
        """
        # Use the unified LLM chat interface
        llm_chat = LLMChat(model=self.llm)

        # Construct prompt
        system_prompt = """You are an expert DevOps engineer specializing in containerization.
Your task is to generate a Dockerfile that can successfully setup a given repository.

Requirements:
1. Use an appropriate base image for the programming language
2. Set up the correct working directory
3. Copy all necessary files
4. Install all dependencies
5. Set appropriate environment variables if needed

Output ONLY the Dockerfile content without any explanation or markdown code blocks."""

        user_prompt = f"""Based on the following repository information, generate a production-ready Dockerfile:

Repository: {repo_name}

{repo_context}

Generate a Dockerfile that:
- Installs all required dependencies
- Sets up all variables and configuration correctly
- Uses best practices for the detected language/framework

Output the Dockerfile content directly:"""

        try:
            # Use LLMChat.chat method
            dockerfile_content, usage = llm_chat.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,  # Low temperature for consistency
                max_tokens=2000,
            )

            if dockerfile_content is None or not dockerfile_content:
                raise Exception("LLM returned empty response")

            dockerfile_content = dockerfile_content.strip()

            # Clean up markdown code blocks if present
            if dockerfile_content.startswith("```dockerfile"):
                dockerfile_content = dockerfile_content[len("```dockerfile") :].strip()
            elif dockerfile_content.startswith("```"):
                dockerfile_content = dockerfile_content[3:].strip()
            if dockerfile_content.endswith("```"):
                dockerfile_content = dockerfile_content[:-3].strip()

            return dockerfile_content

        except Exception as e:
            raise Exception(f"LLM Dockerfile generation failed: {str(e)}")

    @weave.op
    def predict(self, full_name: str) -> dict:
        """
        Process a single repository and return its status.

        Args:
            full_name: Repository full name (owner/repo)

        Returns:
            Result dict with status and metrics
        """
        start_time = time.time()
        print(f"\n{'=' * 60}")
        print(f"[ZeroShot] Processing: {full_name}")
        print(f"{'=' * 60}")

        # Prepare output directory
        init_output_and_repo(self.root_path, full_name, renew=True)

        # Download repository
        print("📥 Downloading repository...")
        try:
            download_repo(
                self.root_path,
                full_name,
                has_issue=False,
                use_repo_dockerfile=False,
            )
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

        repo_name_simple = full_name.split("/")[-1]
        image_name = f"zeroshot-{full_name.replace('/', '-').lower()}"
        container_name = f"{image_name}-test"
        output_dir = os.path.join(self.root_path, "output", full_name)

        try:
            # Step 1: Collect repository context
            print("📚 Collecting repository context...")
            self._check_timeout(start_time, "context collection")

            repo_context = self._collect_repo_context(repo_abs_path)
            print(f"   Context collected: {len(repo_context)} characters")

            # Step 2: Generate Dockerfile using LLM
            print("🤖 Generating Dockerfile with LLM...")
            self._check_timeout(start_time, "LLM generation")

            dockerfile_content = self._generate_dockerfile_with_llm(
                repo_context, repo_name_simple
            )
            print("   Dockerfile generated successfully")
            print(dockerfile_content)

            # Save Dockerfile to output directory
            os.makedirs(output_dir, exist_ok=True)
            dockerfile_path = os.path.join(output_dir, "Dockerfile")
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_content)
            print(f"   Dockerfile saved to: {dockerfile_path}")

            # Step 3: Build Docker image
            print("🐳 Building Docker image...")
            self._check_timeout(start_time, "Docker build")

            # Copy repository to output directory for building
            build_context = os.path.join(output_dir, "build_context")
            os.makedirs(build_context, exist_ok=True)

            # Copy repo files
            subprocess.run(
                f"cp -r {repo_abs_path}/* {build_context}/",
                shell=True,
                check=True,
            )

            # Copy Dockerfile
            subprocess.run(
                f"cp {dockerfile_path} {build_context}/Dockerfile",
                shell=True,
                check=True,
            )

            # Build image
            build_cmd = ["docker", "build", "-t", image_name, build_context]
            build_result = subprocess.run(
                build_cmd,
                capture_output=True,
                text=True,
            )

            if build_result.returncode != 0:
                print(f"❌ Docker build failed")
                return {
                    "status": "error",
                    "error": f"Docker build failed: {build_result.stderr[:500]}",
                    "full_name": full_name,
                }

            print(f"✅ Build succeeded. Image: {image_name}")

            # Step 4: Run tests
            print("🧪 Running tests...")
            self._check_timeout(start_time, "Testing")

            # Remove existing container if any
            subprocess.run(
                f"docker rm -f {container_name}",
                shell=True,
                stderr=subprocess.DEVNULL,
            )

            # Get test tools
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

            # Create logs directory
            logs_path = os.path.join(workdir, "logs")
            subprocess.run(
                ["docker", "exec", container_name, "mkdir", "-p", logs_path],
                check=True,
            )

            # Run pytest collect
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

            # Run pytest
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
            print(f"⏱️  Timeout: {e}")
            return {
                "status": "timeout",
                "error": str(e),
                "full_name": full_name,
                "root_path": self.root_path,
            }

        except Exception as e:
            print(f"❌ Error: {e}")
            traceback.print_exc()
            return {
                "status": "error",
                "error": str(e),
                "full_name": full_name,
                "root_path": self.root_path,
            }

        finally:
            print(f"🧹 Cleaning up resources: {full_name}...")
            # Remove container
            subprocess.run(
                f"docker rm -f {container_name}",
                shell=True,
                stderr=subprocess.DEVNULL,
            )
            # Remove image to save space
            subprocess.run(
                f"docker image rm {image_name}",
                shell=True,
                stderr=subprocess.DEVNULL,
            )
