"""
SWE-agent Wrapper Module

This module provides a Python wrapper around SWE-agent Python API for integration
with the RAT evaluation framework.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

import weave
import yaml


class SWEAgentWrapper:
    """
    Wrapper class for SWE-agent integration.

    Uses SWE-agent Python API to run on repositories and
    integrates with existing Repo2Run infrastructure.
    """

    def __init__(
        self,
        full_name: str,
        root_path: str,
        llm_name: str = "deepseek-chat",
        max_turns: int = 30,
        timeout: int = 900,
        per_instance_cost_limit: float = 2.0,
    ):
        """
        Initialize SWE-agent wrapper.

        Args:
            full_name: Repository full name (e.g., "owner/repo")
            root_path: Root directory path
            llm_name: LLM model name
            max_turns: Maximum number of agent turns (not used with Python API)
            timeout: Timeout in seconds (not used with Python API)
            per_instance_cost_limit: Cost limit per instance in USD
        """
        from sweagent.utils.log import get_logger

        self.full_name = full_name
        self.root_path = Path(root_path)
        self.llm_name = self._convert_model_name(llm_name)
        self.max_turns = max_turns
        self.timeout = timeout
        self.per_instance_cost_limit = per_instance_cost_limit

        # Paths
        self.repo_path = self.root_path / "input" / "repo" / full_name
        self.output_dir = self.root_path / "output" / full_name
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # SWE-agent logger
        self.logger = get_logger("repo2run-sweagent", emoji="🔧")

    def _convert_model_name(self, llm_name: str) -> str:
        """
        Convert Repo2Run model names to SWE-agent compatible names.

        Args:
            llm_name: Model name from Repo2Run config

        Returns:
            SWE-agent compatible model name
        """
        model_mapping = {
            "deepseek-chat": "deepseek/deepseek-chat",
            "gpt-4o": "gpt-4o",
            "gpt-4o-2024-05-13": "gpt-4o-2024-05-13",
            "claude-sonnet-4": "claude-sonnet-4-20250514",
            "claude-3.5-sonnet": "claude-3-5-sonnet-20240620",
            "glm-4-flash": "glm-4-flash",
        }
        return model_mapping.get(llm_name, llm_name)

    def _render_template(self, template: str, variables: Dict[str, str]) -> str:
        """
        Render a template string by replacing {{variable}} placeholders.

        Args:
            template: Template string with {{variable}} placeholders
            variables: Dictionary of variable names to values

        Returns:
            Rendered template string
        """
        result = template
        for key, value in variables.items():
            placeholder = f"{{{{{key}}}}}"
            result = result.replace(placeholder, str(value))
        return result

    def _load_problem_statement_from_config(
        self, config_dict: Dict[str, Any], language: str = "python"
    ) -> str:
        """
        Load and render problem statement template from config.

        Args:
            config_dict: Loaded YAML configuration dictionary
            language: Programming language of the repository

        Returns:
            Rendered problem statement string
        """
        try:
            template = config_dict["agent"]["templates"]["problem_statement_template"]
            return self._render_template(template, {"language": language})
        except KeyError as e:
            self.logger.warning(
                f"Could not load problem_statement_template from config: {e}"
            )
            # Fallback to a simple default
            return f"This is a {language} repository that needs environment setup. Configure it so it can run and pass all tests."

    @weave.op
    def run(
        self, language: str = "python", keep_container: bool = False
    ) -> Dict[str, Any]:
        """
        Run SWE-agent on the repository using Python API.

        Args:
            language: Programming language of the repository
            keep_container: If True, keep the container running after execution

        Returns:
            Dictionary containing:
                - success: bool - Whether SWE-agent completed successfully
                - trajectory_path: str - Path to trajectory file
                - patch_path: str - Path to patch file (if any)
                - output: str - Summary output
                - error: str - Error message (if failed)
                - execution_time: float - Time taken in seconds
                - model_stats: dict - Model statistics (tokens, cost, etc.)
                - env: SWEEnv - The environment object (if keep_container=True)
                - runner: RunSingle - The runner object (if keep_container=True)
        """

        from sweagent.agent.problem_statement import TextProblemStatement
        from sweagent.run.run_single import RunSingle, RunSingleConfig

        start_time = time.time()

        print(f"\n{'=' * 60}")
        print(f"🤖 Running SWE-agent on: {self.full_name}")
        print(f"{'=' * 60}")

        # Prepare output directory
        trajectory_dir = self.output_dir / "sweagent_trajectories"
        trajectory_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Load base configuration from YAML file
            config_path = self.root_path / "eval" / "sweagent" / "python-config.yaml"
            self.logger.info(f"Loading config from: {config_path}")

            with open(config_path, "r") as f:
                config_dict = yaml.safe_load(f)

            # Load problem statement from config template
            problem_text = self._load_problem_statement_from_config(
                config_dict, language
            )

            # Override configuration with runtime parameters
            config_dict["agent"]["model"]["name"] = self.llm_name
            config_dict["agent"]["model"]["per_instance_cost_limit"] = (
                self.per_instance_cost_limit
            )
            config_dict["env"]["repo"]["path"] = str(self.repo_path)
            config_dict["output_dir"] = str(trajectory_dir)

            # Important: Don't auto-remove container if we want to keep it
            if keep_container:
                config_dict["env"]["deployment"]["remove_container"] = False

            # Create problem statement
            problem_statement = TextProblemStatement(
                text=problem_text, id=self.full_name.replace("/", "_")
            )
            config_dict["problem_statement"] = problem_statement

            print(f"📝 Problem statement:")
            print(f"   {problem_text[:200]}...")
            print(f"\n🚀 Executing SWE-agent via Python API...")
            print(f"   Model: {self.llm_name}")
            print(f"   Cost limit: ${self.per_instance_cost_limit}")
            print(f"   Repository: {self.repo_path}")
            print(f"   Output: {trajectory_dir}")
            if keep_container:
                print(f"   Container: Will be kept running")
            print(f"\n{'=' * 60}")
            print("📡 SWE-agent running...")
            print(f"{'=' * 60}\n")

            # Create RunSingleConfig from dictionary
            config = RunSingleConfig(**config_dict)

            # Create runner
            runner = RunSingle.from_config(config)

            # Run SWE-agent with custom flow to keep container alive if needed
            if keep_container:
                # Manual execution to control container lifecycle
                runner._chooks.on_start()
                runner.logger.info("Starting environment")
                runner.env.start()
                runner.logger.info("Running agent")
                runner._chooks.on_instance_start(
                    index=0, env=runner.env, problem_statement=runner.problem_statement
                )
                output_dir = runner.output_dir / runner.problem_statement.id
                output_dir.mkdir(parents=True, exist_ok=True)

                result = runner.agent.run(
                    problem_statement=runner.problem_statement,
                    env=runner.env,
                    output_dir=output_dir,
                )

                runner._chooks.on_instance_completed(result=result)
                runner.logger.info("Done")
                runner._chooks.on_end()
                from sweagent.run.common import save_predictions

                save_predictions(runner.output_dir, runner.problem_statement.id, result)

                # DON'T close the environment - keep container running
                self.logger.info("Container kept running for testing")
            else:
                # Normal execution - will close container automatically
                result = runner.run()

            execution_time = time.time() - start_time

            # Extract results
            success = result.info.get("exit_status") == "submitted"
            submission = result.info.get("submission")
            model_stats = result.info.get("model_stats", {})

            print(f"\n{'=' * 60}")
            if success:
                print(f"✅ SWE-agent completed successfully")
            else:
                print(
                    f"⚠️  SWE-agent finished with status: {result.info.get('exit_status')}"
                )
            print(f"⏱️  Execution time: {execution_time:.2f}s")
            if model_stats:
                print(f"💰 Cost: ${model_stats.get('instance_cost', 0):.4f}")
                print(f"🔢 API calls: {model_stats.get('api_calls', 0)}")
            print(f"{'=' * 60}\n")

            # Find trajectory and patch files
            trajectory_path = self._find_latest_trajectory(trajectory_dir)
            patch_path = self._find_patch_file(trajectory_dir)

            result_dict = {
                "success": success,
                "trajectory_path": str(trajectory_path) if trajectory_path else None,
                "patch_path": str(patch_path) if patch_path else None,
                "output": f"Status: {result.info.get('exit_status')}, Steps: {len(result.trajectory)}",
                "error": None if success else result.info.get("exit_status"),
                "execution_time": execution_time,
                "model_stats": model_stats,
                "submission": submission,
                "info": result.info,
            }

            # Include environment and runner if keeping container
            if keep_container:
                result_dict["env"] = runner.env
                result_dict["runner"] = runner

            return result_dict

        except Exception as e:
            execution_time = time.time() - start_time
            print(f"\n❌ SWE-agent execution failed: {e}")
            self.logger.exception("SWE-agent execution failed", exc_info=True)
            return {
                "success": False,
                "trajectory_path": None,
                "patch_path": None,
                "output": str(e),
                "error": str(e),
                "execution_time": execution_time,
                "model_stats": {},
            }

    def _find_latest_trajectory(self, trajectory_dir: Path) -> Optional[Path]:
        """
        Find the latest trajectory JSON file.

        Args:
            trajectory_dir: Directory containing trajectory files

        Returns:
            Path to latest trajectory file or None
        """
        if not trajectory_dir.exists():
            return None

        trajectory_files = list(trajectory_dir.glob("**/*.traj"))
        if not trajectory_files:
            trajectory_files = list(trajectory_dir.glob("**/*.json"))

        if not trajectory_files:
            return None

        # Return the most recently modified file
        return max(trajectory_files, key=lambda p: p.stat().st_mtime)

    def _find_patch_file(self, trajectory_dir: Path) -> Optional[Path]:
        """
        Find the patch file generated by SWE-agent.

        Args:
            trajectory_dir: Directory containing output files

        Returns:
            Path to patch file or None
        """
        if not trajectory_dir.exists():
            return None

        patch_files = list(trajectory_dir.glob("**/*.patch"))
        if not patch_files:
            return None

        return max(patch_files, key=lambda p: p.stat().st_mtime)

    def extract_statistics(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract statistics from SWE-agent result.

        Args:
            result: Result dictionary from run()

        Returns:
            Dictionary with extracted statistics
        """
        model_stats = result.get("model_stats", {})
        info = result.get("info", {})

        stats = {
            "success": result.get("success", False),
            "execution_time": result.get("execution_time", 0),
            "exit_status": info.get("exit_status", "unknown"),
            "num_actions": len(info.get("trajectory", []))
            if "trajectory" in info
            else 0,
            "api_calls": model_stats.get("api_calls", 0),
            "tokens_sent": model_stats.get("tokens_sent", 0),
            "tokens_received": model_stats.get("tokens_received", 0),
            "cost_estimate": model_stats.get("instance_cost", 0.0),
            "has_submission": result.get("submission") is not None,
        }

        # Try to parse trajectory for more detailed stats if available
        trajectory_path = result.get("trajectory_path")
        if trajectory_path and Path(trajectory_path).exists():
            try:
                with open(trajectory_path, "r") as f:
                    trajectory_data = json.load(f)

                # Override with trajectory data if available
                if "trajectory" in trajectory_data:
                    stats["num_actions"] = len(trajectory_data["trajectory"])

                if (
                    "info" in trajectory_data
                    and "model_stats" in trajectory_data["info"]
                ):
                    traj_model_stats = trajectory_data["info"]["model_stats"]
                    stats["cost_estimate"] = traj_model_stats.get(
                        "instance_cost", stats["cost_estimate"]
                    )
                    stats["api_calls"] = traj_model_stats.get(
                        "api_calls", stats["api_calls"]
                    )

            except Exception as e:
                self.logger.warning(f"Could not parse trajectory file: {e}")

        return stats


def test_wrapper():
    """Test function for SWEAgentWrapper"""
    # Example usage
    wrapper = SWEAgentWrapper(
        full_name="test-org/test-repo",
        root_path="/tmp/test",
        llm_name="deepseek-chat",
        max_turns=10,
        timeout=300,
    )

    print("Testing template rendering:")
    # Test the template rendering method
    config_path = wrapper.root_path / "eval" / "sweagent" / "python-config.yaml"
    if config_path.exists():
        with open(config_path, "r") as f:
            config_dict = yaml.safe_load(f)
        problem_text = wrapper._load_problem_statement_from_config(
            config_dict, "python"
        )
        print("Problem statement:")
        print(problem_text)
    else:
        print(f"Config file not found at: {config_path}")

    print(f"\nModel name mapping: {wrapper.llm_name}")


if __name__ == "__main__":
    test_wrapper()
