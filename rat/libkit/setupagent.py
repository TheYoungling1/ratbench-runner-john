"""
SetupAgent V2 - enhanced environment setup agent.

Combines analysis with real container-based verification.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict

import docker
import weave

from libkit.llm import LLMChat
from libkit.memory import EnvConfigMemory
from libkit.tools.retrieve_image import ImageRetriever
from libkit.utils.docker_utils import sanitize_docker_image_name
from libkit.utils.language_detector import detect_language
from libkit.dockerfile_templates import (
    TemplateManager,
    VersionSelector,
)


class SetupAgent:
    """
    Enhanced SetupAgent with:
    1. retrieve_image-based analysis
    2. Real Docker-container validation
    3. Automatic dependency installation and verification
    4. Multiple candidate image attempts
    """

    def __init__(
        self,
        full_name,
        root_path,
        llm_name="deepseek-chat",
        use_memory=True,
        memory_dir=".memory",
        use_llm_analysis=True,
        max_image_candidates=3,
    ):
        self.llm = LLMChat(llm_name)
        self.llm_name = llm_name
        self.full_name = full_name
        self.root_path = root_path
        self.use_llm_analysis = use_llm_analysis
        self.max_image_candidates = max_image_candidates
        self.docker_client = docker.from_env(timeout=600)

        # Initialize memory mechanism
        self.use_memory = use_memory
        if self.use_memory:
            self.memory = EnvConfigMemory(memory_dir)
            self.memory.update_working_memory("current_project", full_name)
        else:
            self.memory = None
        self.repo_path = Path(self.root_path) / "input" / "repo" / self.full_name
        self.test_results = []

        # Initialize template manager and version selector
        self.template_manager = TemplateManager()
        self.version_selector = VersionSelector()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit (ensures cleanup)."""
        self.cleanup()
        # Do not suppress exceptions
        return False

    def cleanup(self):
        """Clean up resources."""
        if self.docker_client:
            try:
                self.docker_client.close()
                self.docker_client = None
            except Exception as e:
                print(f"⚠️  Failed to close Docker client: {e}")

    @weave.op
    def run(self) -> Dict:
        """
        Main workflow:
        1. Analyze the repo using retrieve_image
        2. Get candidate images
        3. Validate each image in a real container
        4. Return the best configuration result
        """
        print("************** SetupAgent V2: analysis start **************")

        # Step 0: quick detection via the unified language detector
        print("🔍 Step 0: quick language detection...")
        detected_language = detect_language(
            full_name=self.full_name,
            project_path=str(self.repo_path),
            prefer_github_api=True,
        )
        print(f"  ✓ Detected primary language: {detected_language}")

        # Step 1: ImageRetriever analysis
        print("🔍 Step 1: analyzing repository structure and dependencies...")
        retriever = ImageRetriever(
            repo_path=str(self.repo_path),
            llm_name=self.llm_name,
        )
        analysis = retriever.retrieve_image()

        # Cross-check: warn if ImageRetriever and the unified detector disagree
        if analysis.get("language", "unknown").lower() != detected_language.lower():
            print(
                f"⚠️  Note: ImageRetriever detected {analysis.get('language')}, "
                f"but the unified detector detected {detected_language}"
            )
            # Prefer the GitHub API result (usually more accurate)
            if detected_language != "python":  # Not the default fallback
                print(f"  → Using unified detector result: {detected_language}")
                analysis["language"] = detected_language
        print("📊 Analysis summary:")
        print(f"  - Language: {analysis.get('language', 'unknown')}")
        print(f"  - Version: {analysis.get('version', 'N/A')}")
        print(f"  - Recommended image: {analysis.get('full_image', 'N/A')}")
        print(f"  - Frameworks: {', '.join(analysis.get('frameworks', []))}")
        print(f"  - Dependency count: {len(analysis.get('dependencies', []))}")
        print(f"  - Confidence: {analysis.get('confidence', 'N/A')}")
        print(f"  - Rationale: {analysis.get('reason', 'N/A')[:100]}...")

        # Step 2: generate Dockerfile
        detected_language = analysis.get("language", "").lower()
        supported_languages = ["rust", "python", "javascript", "java", "node"]

        if detected_language in supported_languages:
            language_emoji = {
                "rust": "🦀",
                "python": "🐍",
                "javascript": "📜",
                "node": "🟢",
                "java": "☕",
            }
            emoji = language_emoji.get(detected_language, "🔧")

            # Template mode
            print(
                f"\n{emoji} Detected {detected_language.title()} project; generating Dockerfile from template..."
            )
            dockerfile_result = self.build_dockerfile_with_template(analysis)
            dockerfile_content = dockerfile_result.get("content")
            build_successful = dockerfile_result.get("successful", False)
            selected_version = dockerfile_result.get("selected_version", "")
            image_with_tag = dockerfile_result.get("image_tag", "")

            if not build_successful:
                print("⚠️  Dockerfile build failed; falling back to default image")
                # Fall back to default image
                default_images = {
                    "rust": "rust:1.75",
                    "python": "python:3.10-slim",
                    "javascript": "node:18-slim",
                    "node": "node:18-slim",
                    "java": "eclipse-temurin:17",
                }
                image_with_tag = default_images.get(
                    detected_language, "python:3.10-slim"
                )
                # Regenerate Dockerfile
                print(f"🔄 Regenerating Dockerfile (using {image_with_tag})...")
                dockerfile_content = self.template_manager.generate_dockerfile(
                    detected_language, image_with_tag
                )
        else:
            # Unsupported language: use default config
            dockerfile_content = None
            build_successful = False
            selected_version = ""
            image_with_tag = "python:3.10-slim"

        print("\n************** SetupAgent V2: analysis done **************")
        result = {
            "recommended_base_image": image_with_tag,
            "language": analysis.get("language", "unknown"),
            "detected_version": analysis.get("version", "latest"),
            "selected_version": selected_version,  # Include selected version
            "reasoning": f"Selection based on analysis: {analysis.get('language', 'unknown')} {analysis.get('version', '')} + {', '.join(analysis.get('frameworks', [])[:3])}",
            "frameworks": analysis.get("frameworks", []),
            "dependencies": analysis.get("dependencies", []),
            "dockerfile_content": dockerfile_content,
            "build_successful": build_successful,
        }

        return result

    def _get_default_result(self, analysis: Dict) -> Dict:
        """
        Return a default configuration result (when all tests fail).
        """
        language = analysis.get("language", "python").lower()
        version = analysis.get("version", "latest")

        # Choose a default image by language
        default_images = {
            "python": "python:3.10",
            "node": "node:16",
            "javascript": "node:16",
            "java": "eclipse-temurin:11",
            "go": "golang:1.19",
            "rust": "rust:latest",
        }

        base_image = default_images.get(language, "python:3.10")

        # If a version is detected, try to use it
        if version and version != "latest":
            # Extract major/minor
            import re

            match = re.search(r"(\d+\.?\d*)", version)
            if match:
                version_num = match.group(1)
                base_image = f"{language}:{version_num}"

        # Return dict result
        return {
            "recommended_base_image": base_image,
            "language": language,
            "detected_version": version,
            "reasoning": "Using default configuration (no better image found)",
            "frameworks": analysis.get("frameworks", []),
            "dependencies": analysis.get("dependencies", []),
        }

    def _save_to_memory(self, result: Dict, analysis: Dict):
        """
        Save result to memory.
        """
        if not self.memory:
            return

        language = result.get("language", "")
        version = result.get("detected_version", "")
        image = result.get("recommended_base_image", "")
        reasoning = result.get("reasoning", "")
        score = result.get("test_score", 0)

        # Update working memory
        self.memory.update_working_memory("current_language", language)

        # Record decision
        self.memory.add_config_decision(
            {
                "decision_type": "base_image_selection_v2",
                "content": image,
                "reasoning": reasoning,
                "test_score": score,
            }
        )

        # Record image selection (success based on score)
        self.memory.record_image_selection(
            language=language,
            version=version,
            image=image,
            success=(score >= 50),
            notes=f"{reasoning} (score: {score})",
        )

        # Persist session to long-term memory
        self.memory.save_session_to_long_term(
            {
                "project": self.full_name,
                "language": language,
                "version": version,
                "base_image": image,
                "test_score": score,
                "installation_commands": result.get("installation_commands", []),
                "agent_version": "v2",
            }
        )

        # Print memory stats
        print("\n📊 Memory stats:")
        print(json.dumps(self.memory.get_statistics(), indent=2, ensure_ascii=False))

    @weave.op
    def build_dockerfile_with_template(self, analysis: Dict) -> Dict:
        """
        Build a Dockerfile using templates.

        Args:
            analysis: Repository analysis result.

        Returns:
            {
                "content": "Dockerfile content",
                "successful": True/False,
                "selected_version": "3.10-slim",
                "image_tag": "python:3.10-slim"
            }
        """
        print("🎯 Generating Dockerfile from template...")

        language = analysis.get("language", "").lower()

        if not self.template_manager or not self.version_selector:
            print("❌ Template manager not initialized")
            return {
                "content": None,
                "successful": False,
                "selected_version": "",
                "image_tag": "",
            }

        # Step 1: pick version/image
        # If analysis provides a high-confidence full image, use it directly
        if analysis.get("confidence") == "high" and analysis.get("full_image"):
            image_tag = analysis.get("full_image")
            version = analysis.get("version", "latest")
            variant = ""
            print(f"\n📌 Using high-confidence recommended image: {image_tag}")
        else:
            print(f"\n📌 Selecting version for {language} project...")
            version, variant = self.version_selector.select_version_from_analysis(
                language, analysis
            )

            # Step 2: construct full image tag
            image_tag = self.version_selector.format_image_tag(
                language, version, variant
            )
            print(f"  ✓ Selected image: {image_tag}")

        # Step 3: generate Dockerfile from template
        print("\n📝 Generating Dockerfile...")
        dockerfile_content = self.template_manager.generate_dockerfile(
            language, image_tag
        )

        if not dockerfile_content:
            print("❌ Failed to generate Dockerfile")
            return {
                "content": None,
                "successful": False,
                "selected_version": "",
                "image_tag": image_tag,
            }

        print("  ✓ Dockerfile generated")
        print("📝 Generated Dockerfile:")
        print("-" * 40)
        print(dockerfile_content)
        print("-" * 40)

        # Step 4: test build
        print("\n🔨 Testing Dockerfile build...")
        build_success, build_logs = self._build_dockerfile_in_temp(
            dockerfile_content,
            sanitize_docker_image_name(
                self.full_name, prefix="template_", suffix=f"_{version}"
            ),
        )

        if build_success:
            print("✅ Dockerfile build succeeded")
        else:
            print("❌ Dockerfile build failed")
            print(f"Build logs:\n{build_logs[:500]}")

        selected_version = f"{version}-{variant}" if variant else version

        return {
            "content": dockerfile_content,
            "successful": build_success,
            "selected_version": selected_version,
            "image_tag": image_tag,
            "build_logs": build_logs if not build_success else "",
        }

    def _build_dockerfile_in_temp(
        self, dockerfile_content: str, image_tag: str
    ) -> tuple:
        """
        Build the Dockerfile in a temporary directory.

        Returns:
            (success: bool, logs: str)
        """
        temp_dir = None
        try:
            # Create temp directory
            temp_dir = tempfile.mkdtemp(prefix="dockerfile_test_")
            dockerfile_path = os.path.join(temp_dir, "Dockerfile")

            # Write Dockerfile
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_content)

            # Build image
            print(f"📦 Building image: {image_tag}")
            result = subprocess.run(
                ["docker", "build", "-t", image_tag, temp_dir],
                capture_output=True,
                text=True,
                timeout=600,  # 10-minute timeout
            )

            build_logs = result.stdout + "\n" + result.stderr
            success = result.returncode == 0

            # Remove test image
            if success:
                try:
                    subprocess.run(
                        ["docker", "rmi", image_tag],
                        capture_output=True,
                        timeout=60,
                    )
                except Exception:
                    pass  # Cleanup failure is non-fatal

            return success, build_logs

        except subprocess.TimeoutExpired:
            return False, "Docker build timeout after 10 minutes"
        except Exception as e:
            return False, f"Build error: {str(e)}"
        finally:
            # Clean temp dir
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                except Exception:
                    pass  # Cleanup failure is non-fatal

    def _parse_build_error(self, build_logs: str) -> str:
        """Parse build logs and extract key error information."""
        # Common error patterns
        error_patterns = {
            "apt_404": r"Err:\d+.*404\s+Not Found",
            "package_not_found": r"E: Unable to locate package (.+)",
            "network_timeout": r"Could not resolve|Connection timed out",
            "pip_install_fail": r"ERROR: Could not find a version",
            "cargo_install_fail": r"error: failed to compile",
            "missing_dependency": r"error: no matching package named",
        }

        errors_found = []
        for error_type, pattern in error_patterns.items():
            if re.search(pattern, build_logs, re.IGNORECASE):
                errors_found.append(error_type)

        if errors_found:
            return f"Detected error types: {', '.join(errors_found)}"
        else:
            return "Unrecognized build error"

    def _generate_error_feedback(
        self, iteration: int, error_analysis: str, build_logs: str
    ) -> str:
        """Generate error feedback for the LLM."""
        # Truncate logs (keep last 1000 lines)
        log_lines = build_logs.split("\n")
        if len(log_lines) > 1000:
            truncated_logs = "\n".join(log_lines[-1000:])
        else:
            truncated_logs = build_logs

        return f"""Build failed on iteration {iteration}.

## Error analysis
{error_analysis}

## Build logs (last 1000 lines)
```
{truncated_logs}
```

Analyze the error and modify the Dockerfile. Common issues:
1. apt-get mirror issues -> add apt-get update or switch mirrors
2. Incorrect package name -> verify spelling/version
3. Network issues -> use reliable mirrors
4. Permission issues -> check permissions or use sudo
5. Dependency conflicts -> adjust versions/install order

Output the full updated Dockerfile.
"""


if __name__ == "__main__":
    # Test code
    import sys

    if len(sys.argv) < 3:
        print("Usage: python setupagent.py <full_name> <root_path>")
        sys.exit(1)

    full_name = sys.argv[1]
    root_path = sys.argv[2]

    agent = SetupAgent(full_name, root_path, use_llm_analysis=True)
    try:
        result = agent.run()

        print("\n" + "=" * 60)
        print("Final recommendation:")
        print("=" * 60)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        # Ensure Docker client cleanup
        agent.cleanup()
