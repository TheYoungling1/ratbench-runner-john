import argparse
import os
import subprocess
import threading
import time
from contextlib import contextmanager

from libkit.analyze import analyze_config_issues
from libkit.codeagent import CodeAgent
from libkit.command import (
    download_repo,
    init_output_and_repo,
    print_timing_summary,
    save_trajectory,
    setup_environment_config,
    stop_and_remove_container,
    timer,
)
from libkit.environment import Env

# Global variable to store timing data
_task_timings = []


@contextmanager
def task_timer(task_name):
    start = time.time()
    print(f"🚀 Starting execution: {task_name}...")
    yield
    end = time.time()
    elapsed = end - start
    _task_timings.append((task_name, elapsed))


def main():
    parser = argparse.ArgumentParser(
        description="Run script with repository full name as an argument."
    )
    parser.add_argument(
        "--full_name",
        type=str,
        help="The full name of the repository (e.g., user/repo).",
    )
    parser.add_argument("--root_path", type=str, help="Root path for operations")
    parser.add_argument("--num_turn", type=int, default=25, help="Number of interaction turns")
    parser.add_argument(
        "--llm", type=str, default="deepseek-chat", help="Base LLM model name"
    )
    parser.add_argument(
        "--save_mode",
        type=str,
        default="none",
        choices=["none", "dockerfile", "image"],
        help="Save mode: none-do not save (default), dockerfile-save Dockerfile and files, image-save as local image",
    )
    parser.add_argument(
        "--use-dockerfile",
        action="store_true",
        help="Use the repository's own Dockerfile (if exists) to build the image instead of a system-generated one",
    )
    parser.add_argument(
        "--custom-plan",
        action="store_true",
        help="Use custom plan mode; LLM will use or create a /repo/plan.md file to guide environment configuration",
    )
    parser.add_argument(
        "--use-uv",
        action="store_true",
        help="Use 'uv' as the Python package manager instead of pip/poetry",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Command execution timeout (seconds), default 300s (5 minutes)",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=600,
        help="Test command timeout (seconds), default 600s (10 minutes)",
    )

    args = parser.parse_args()
    root_path = args.root_path
    if not os.path.isabs(root_path):
        root_path = os.path.abspath(root_path)
    full_name = args.full_name
    llm_name = args.llm
    use_dockerfile = args.use_dockerfile
    use_uv = args.use_uv
    timeout = args.timeout
    test_timeout = args.test_timeout

    # Print timeout configuration
    print(f"⏱️  Timeout config: Default command={timeout}s, Test command={test_timeout}s")

    with task_timer("Phase 1: Initialize output and download repository"):
        init_output_and_repo(root_path, full_name, renew=True)
        download_repo(
            root_path, full_name, has_issue=True, use_repo_dockerfile=use_dockerfile
        )

    # Part 1: Configure environment (using Repo Dockerfile or SetupAgent analysis)
    with task_timer("Phase 2: SetupAgent starts configuration"):
        env_config = setup_environment_config(
            root_path, full_name, llm_name, use_dockerfile
        )
        language = env_config["language"]
        namespace = env_config["namespace"]
        repo_dockerfile_path = env_config.get("primary_dockerfile", None)
        dockerfile_content = env_config.get("dockerfile_content", None)

    # Start the timer thread
    timer_thread = threading.Thread(target=timer)
    timer_thread.daemon = True
    timer_thread.start()

    with task_timer("Phase 3: Create container"):
        trajectory = []
        code_environment = Env(
            namespace,
            language,
            full_name,
            root_path,
            save_mode=args.save_mode,
            use_repo_dockerfile=use_dockerfile,
            repo_dockerfile_path=repo_dockerfile_path,
            dockerfile_content=dockerfile_content,
            use_uv=use_uv,
        )

        # Create container; return value indicates if the repo Dockerfile was actually used
        actually_used_repo_dockerfile = code_environment.create_container()

    # If building fails and falls back to system Dockerfile, update the flag
    if use_dockerfile and not actually_used_repo_dockerfile:
        print("⚠️  Repository Dockerfile build failed, fell back to system-generated Dockerfile")

    print("🔍 Verifying environment inside container...")
    with task_timer("Phase 4: Configuring environment"):
        session = code_environment.get_session()
        code_agent = CodeAgent(
            code_environment,
            namespace,
            full_name,
            root_path,
            language,
            llm_name,
            args.num_turn,
            use_color=True,
            use_custom_plan=args.custom_plan,
            use_repo_dockerfile=actually_used_repo_dockerfile,  # Use actual Dockerfile type
            use_uv=use_uv,
            timeout=timeout,
            test_timeout=test_timeout,
        )
        msg, outer_commands = code_agent.run("/tmp", trajectory=trajectory)
        commands = code_environment.stop_container()

        save_trajectory(root_path, full_name, msg)

    with task_timer("Phase 5: Analyzing configuration issues"):
        print("\n" + "=" * 80)
        print("🔍 Analyzing issues during configuration...")
        analysis_result = analyze_config_issues(
            full_name, root_path, full_analysis=False
        )
        if analysis_result:
            print(analysis_result)
        else:
            print("Analysis failed or configuration file could not be read")


if __name__ == "__main__":
    try:
        stop_and_remove_container()
        subprocess.run(
            'docker rmi $(docker images --filter "dangling=true" -q) > /dev/null 2>&1',
            shell=True,
        )
    except:
        print("No dangling images")
    main()
    print_timing_summary(_task_timings)
