#!/usr/bin/env python3
"""
Rust Language Configuration
"""

from typing import List, Dict
from libkit.language_config import LanguageConfig
from libkit.tool import ToolKit, BASH_FENCE


class RustConfig(LanguageConfig):
    """Environment configuration for Rust language"""

    @property
    def language_name(self) -> str:
        return "Rust"

    def get_toolkit(self) -> List:
        """Return Rust-specific tool list"""
        return [
            ToolKit.retrieve_issue,
            ToolKit.detect_environment,
            ToolKit.cicd_config,
            ToolKit.construct_test,
            ToolKit.run_test,
            ToolKit.search_repo,
            ToolKit.read_file,
            ToolKit.view_outline,
            ToolKit.search_web,
            ToolKit.ls_structure,
            ToolKit.edit_file,
            ToolKit.stop,
        ]

    def get_init_prompt(
        self,
        image_name: str,
        tools_list: str,
        use_custom_plan: bool = False,
        max_turn: int = 25,
        use_repo_dockerfile: bool = False,
    ) -> str:
        """Return initial system prompt for Rust"""
        if use_custom_plan:
            return self._get_custom_plan_prompt(
                image_name, tools_list, self.language_name, max_turn
            )
        if use_repo_dockerfile:
            return self._get_repo_dockerfile_prompt(image_name, tools_list)
        return self._get_standard_plan_prompt(image_name, tools_list)

    def _get_repo_dockerfile_prompt(self, image_name: str, tools_list: str) -> str:
        """Configuration prompt when using repository Dockerfile"""
        return f"""
You are an expert proficient in testing. The current environment has been built using the repository's Dockerfile (image: {image_name}), and the environment configuration should be complete.

Your main tasks are:
1. Verify that the environment is working correctly
2. Create test cases
3. Run tests and ensure they pass

Workflow:
1. Quick environment verification: Check if key commands and dependencies are available
2. Use the construct-test tool to create test cases
3. Run tests (run-cargo-build and run-cargo-test)
4. If tests fail, analyze the cause:
   - If it's missing dependencies: May need to install missing crates
   - If it's compilation errors: Check if Rust version or dependency versions match
   - If it's other errors: Analyze whether it's a test case issue or environment issue
5. Fix issues and re-test
6. Call stop after tests pass

CLI Tool Usage Instructions:
All operations are performed inside Docker container {image_name}
Think about what to do first, then wrap commands with {BASH_FENCE[0]} ... {BASH_FENCE[1]}, for example:
### Thought: I need to verify the Rust environment
### Action:
{BASH_FENCE[0]}
    rustc --version && cargo --version
{BASH_FENCE[1]}

Available tools (callable but not terminal built-in commands):
{tools_list}

Important Notes:
1. Environment has been built via repository Dockerfile, most dependencies should already be installed
2. No need to configure environment from scratch, just verify and supplement
3. Prioritize using construct-test to create test cases
4. Test goal: Ensure run-cargo-build and run-cargo-test can run successfully
5. If encountering missing dependencies, install the corresponding crate
6. Do not modify test functions to make tests pass
7. Keep commands on a single line, use && to connect

Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_standard_plan_prompt(self, image_name: str, tools_list: str) -> str:
        """Standard Rust configuration prompt"""
        return f"""
You are an expert proficient in Rust environment configuration. You can reference various files and structures in the repository, such as Cargo.toml, Cargo.lock, etc., to configure Rust project environment in the specified Docker image. This ensures the repository can be successfully configured and execute specified tests correctly.
Note: This repository originally had no Dockerfile, or the existing Dockerfile has been deleted. Do not attempt to use the repository's original Dockerfile information.
Your workflow is:
0. Explore the repository to understand the complete project structure and the image configuration.
1. Read and understand important documentation in the repository, which may include but is not limited to README.md, CONTRIBUTING.md, *.md, *.txt, content in docs/, etc.
2. Read the repository directory and files related to environment configuration, such as Cargo.toml, Cargo.lock, rust-toolchain.toml, etc. Consider other files and structures that may be used for environment configuration.
3. Collect dependency information:
   - Check dependencies and dev-dependencies in Cargo.toml file
   - Check if it's a workspace (multi-crate project)
   - Check required Rust version and edition
4. Build the project:
   - Use the run-cargo-build tool to build the project (skip tests)
   - If build fails, analyze errors and fix
5. Run build to verify configuration. If build succeeds, call stop.

CLI Tool Usage Instructions:
All operations are performed inside Docker container {image_name}
Think about what to do first, then wrap commands with {BASH_FENCE[0]} ... {BASH_FENCE[1]}, for example:
### Thought: I need to understand the repository structure
### Action:
{BASH_FENCE[0]}
    ls /repo
{BASH_FENCE[1]}

Note: Do not make major modifications to the /repo folder, only make necessary adjustments.
Try to write commands on a single line, connected with &&; avoid using newlines, backslash line breaks, or HERE-DOC syntax (<<)

Available tools (callable but not terminal built-in commands):
{tools_list}

Important Notes:
1. Do not answer user questions, only execute environment configuration.
2. Build goal: Ensure the environment can run `run-cargo-build` and `run-cargo-test` tools:
  - Ensure the project can be successfully compiled and built
  - Prioritize resolving compilation errors and missing dependency issues
  - After successful build, need to run test cases to ensure tests pass
3. Do not modify core business code to pass the build
4. Try to keep commands as single-line input to avoid parsing errors from multi-line commands
5. No need to execute all commands at once. Explore step by step, and ensure each command is based on your existing knowledge of this repository.
6. Before executing any command, evaluate its potential impact on the system, and achieve the best results with minimal changes. Do not use git clone, wget, or other external tools to batch download files in the /repo directory (or its subdirectories) to avoid major changes to the original repository.
7. The configuration process allows flexibility. If unsure whether configuration is complete, you can run test programs at any time to check, and continue adjusting based on returned errors.
8. Common Rust project features:
   - Cargo: Rust's package manager and build tool
   - Workspace: Multi-crate project structure
   - Feature flags: Conditional compilation functionality
9. Code modification scenarios: Use the edit-file tool to modify code to help make things work when:
  - Configuration files need parameter adjustments (e.g., modifying versions, paths, etc.)
  - Hard-coded dependency versions in code are incompatible with the environment
  - Need to comment out certain dependency checks that cannot be satisfied
  - Fixing obvious code errors (e.g., syntax errors, API call errors, etc.)
  When using edit-file, prioritize using --mode llm for intelligent modifications, or use --mode search for precise replacements

Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_custom_plan_prompt(
        self, image_name: str, tools_list: str, language, max_turn
    ) -> str:
        """Rust configuration prompt for custom plan mode"""
        return f"""
You are an expert proficient in environment configuration. You need to configure the environment based on the repository's custom plan file `plan.md`. You can use the `/repo/plan.md` file as your task plan (it doesn't exist initially). This file contains the goals, phases, and progress of environment configuration.
Additionally, the repository you're configuring has {language} as its main language, so you need to configure the environment accordingly.

## Two-Phase Workflow
### Phase One: Check and Create Plan (if it doesn't exist)
1. First check if the `/repo/plan.md` file exists:
2. If the file doesn't exist, you need to:
   - Explore repository structure to understand the project
   - Create an initial `plan.md` file based on your repository analysis (make steps specific, don't have too many phases or environment configuration will be lengthy)
   - Use the `edit-file` tool to create the file, referencing the following template:

```markdown
# Task Plan: Environment Configuration for {image_name}

## Goal
[Goal of environment configuration, for example: Configure {image_name} environment, ensure tests pass, and considering you need to solve this within limited steps, you can execute a maximum of {max_turn} steps, so don't make it too complex]

### Phase 1: Repository Analysis
<!-- Analyze repository structure and dependencies -->
- [ ] Explore repository structure
- [ ] Identify main entry point
- [ ] Read README and documentation
- [ ] Find dependency files (Cargo.toml, Cargo.lock, etc.)
- **Status**: pending

### Phase 2: Dependency Installation
<!-- Install dependencies -->
- [ ] Check Rust version requirements
- [ ] Build dependencies
- [ ] Handle version conflicts (if any)
- **Status**: pending

### Phase 3: Environment Configuration
<!-- Environment configuration -->
- [ ] Configure environment variables
- [ ] Set up any required services
- [ ] Set up paths and permissions
- **Status**: pending

### Phase 4: Testing & Validation
<!-- Testing and validation -->
- [ ] Run cargo build
- [ ] Run cargo test
- **Status**: pending

## Current Phase
Phase 1

## Notes
[Any important notes]
```
3. If the file already exists, read and understand the existing plan:
### Phase Two: Execute the Plan
1. Read the current plan to understand the current phase and goals:
2. Find the current phase (marked as incomplete with `[ ]`)
3. Start executing tasks for the current phase:
   - If the phase status is not marked, use the `edit-file` tool to update the phase status to `in_progress`
   - Execute tasks for that phase (explore, install, configure, test)
   - After completing the phase, use the `edit-file` tool to update `[ ]` to `[x]` and mark the status as `complete`
4. Continue to the next phase until all phases are complete

## Important Rules
1. **Execute step by step** - Focus on only one portion of a phase at a time
2. **Update progress** - Use the `edit-file` tool to update phase status (`[ ]` → `[x]`)
3. **Record findings** - Add important findings or decisions to the `Notes` section of the plan file
4. **Error handling** - When encountering errors, record the error and solution in the plan file
5. **Completion verification** - When all phases are marked as complete, call the `stop` tool to end configuration
6. If you forget about the plan file, you can read it again with cat.

## Response Format Requirements
**Must use the following format:**
### Thought: [Your thought process]
Analyze the current situation, check plan file status, decide next action.

### Action:
{BASH_FENCE[0]}
[Command to execute, using edit-file tool or other tools]
{BASH_FENCE[1]}

**Example:**
### Thought: I need to check if the plan file exists. First view the /repo/plan.md file.
### Action:
{BASH_FENCE[0]}
cat /repo/plan.md 
{BASH_FENCE[1]}

## Initial Action Guidelines
2. Decide next step based on results: create plan or execute existing plan (note the [ ] format when using edit_file to let llm write todos)
3. Always use `### Thought:` and `### Action:` format
4. Commands must be placed between `{BASH_FENCE[0]}` and `{BASH_FENCE[1]}`

Available tools:
{tools_list}
"""

    def get_dockerfile_content(self, base_image: str, version: str, **kwargs) -> str:
        """
        Return Rust Dockerfile generation logic

        Note: This method has been replaced by SetupAgent's dynamic generation.
        Only used as a fallback when SetupAgent build fails.
        """
        return f"""FROM {base_image}:{version}

# Install necessary system dependencies (including Python, as tools are written in Python)
RUN apt-get update && apt-get install -y python3-pip curl git python-is-python3
# Link python3 as python for convenience
RUN mkdir -p ~/.pip && touch ~/.pip/pip.conf
RUN echo "[global]" >> ~/.pip/pip.conf && echo "[install]" >> ~/.pip/pip.conf
RUN curl -sSL https://install.python-poetry.org | python -
ENV PATH="/root/.local/bin:$PATH"
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

# Install Python tool dependencies
RUN pip install pytest openai -i https://mirrors.aliyun.com/pypi/simple

# Configure Cargo to use Chinese mirror source (USTC)
RUN mkdir -p ~/.cargo && \\
    echo '[source.crates-io]' > ~/.cargo/config.toml && \\
    echo 'replace-with = "ustc"' >> ~/.cargo/config.toml && \\
    echo '' >> ~/.cargo/config.toml && \\
    echo '[source.ustc]' >> ~/.cargo/config.toml && \\
    echo 'registry = "sparse+https://mirrors.ustc.edu.cn/crates.io-index/"' >> ~/.cargo/config.toml

# Configure git safe directory
RUN git config --global --add safe.directory /repo

# Install common Rust tools
RUN cargo install cargo-edit --locked || true
"""

    def get_dependency_files(self) -> List[str]:
        """Return Rust dependency file list"""
        return [
            "Cargo.toml",
            "Cargo.lock",
            "rust-toolchain",
            "rust-toolchain.toml",
        ]

    def get_test_runner_tools(self) -> Dict[str, Dict]:
        """Return Rust test runner tool configuration"""
        return {
            "run-cargo-build": {
                "script": "run_cargo_build.py",
                "timeout": 900,
                "return_codes": [0, 1],  # 0: build succeeded, 1: build failed
                "description": "Build Rust project (skip tests). Usage: run-cargo-build (no parameters needed). Features: execute cargo build, compile source code, resolve and download dependencies, count compilation errors and dependency issues. Results saved to /repo/logs/run_cargo_build_results.json",
            },
            "run-cargo-test": {
                "script": "run_cargo_test.py",
                "timeout": 900,
                "return_codes": [
                    0,
                    1,
                ],  # 0: all passed, 1: some failed but execution succeeded
                "description": "Run Rust tests. Usage: run-cargo-test (no parameters needed). Features: execute cargo test, run all tests, count passed/failed numbers, categorize error types. Results saved to /repo/logs/run_cargo_test_results.json",
            },
        }

    def get_package_manager_commands(self) -> Dict[str, str]:
        """Return Rust package manager commands"""
        return {
            "cargo_build": "cargo build",
            "cargo_test": "cargo test",
            "cargo_check": "cargo check",
            "cargo_clean": "cargo clean",
            "cargo_update": "cargo update",
            "cargo_install": "cargo install",
        }

    def get_env_detection_commands(self) -> List[str]:
        """Return Rust environment detection commands"""
        return [
            "rustc --version",
            "cargo --version",
            "rustup --version",
        ]

    def get_test_result_files(self) -> List[Dict[str, str]]:
        """Return Rust test result file list"""
        return [
            {
                "source": "/repo/logs/run_cargo_build_results.json",
                "dest_name": "run_cargo_build_results.json",
                "description": "Cargo build results",
            },
            {
                "source": "/repo/logs/run_cargo_test_results.json",
                "dest_name": "run_cargo_test_results.json",
                "description": "Cargo test results",
            },
        ]

    def get_language_specific_config(self) -> Dict:
        """Return Rust-specific additional configuration"""
        return {
            "build_tool": "cargo",
            "test_framework": "built-in",
            "supports_workspace": True,
            "default_edition": "2021",
        }

    def get_version_change_tool(self) -> Dict[str, str]:
        """Return Rust version switching tool configuration"""
        return {
            "tool_name": "change-rust-version",
            "method_name": "change_rust_version",
            "emoji": "🦀",
            "example": "change-rust-version 1.75",
        }
