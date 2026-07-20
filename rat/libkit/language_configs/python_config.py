#!/usr/bin/env python3
"""
Python Language Configuration
"""

from typing import List, Dict
from libkit.language_config import LanguageConfig
from libkit.tool import ToolKit, BASH_FENCE


class PythonConfig(LanguageConfig):
    """Environment configuration for Python language"""

    def __init__(self, use_uv: bool = False):
        """Initialize Python configuration

        Args:
            use_uv: Whether to use uv as the package manager
        """
        self.use_uv = use_uv

    @property
    def language_name(self) -> str:
        return "Python"

    def get_toolkit(self) -> List:
        """Return Python-specific tool list"""
        return [
            ToolKit.retrieve_issue,
            ToolKit.detect_environment,
            ToolKit.cicd_config,
            # ToolKit.retrieve_trajectory,
            ToolKit.construct_test,
            ToolKit.change_python_version,
            ToolKit.run_test,
            ToolKit.run_pytest,
            ToolKit.run_pytest_collect,
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
        """Return initial system prompt for Python"""
        if use_custom_plan:
            return self._get_custom_plan_prompt(
                image_name, tools_list, self.language_name, max_turn
            )
        if use_repo_dockerfile:
            return self._get_repo_dockerfile_prompt(image_name, tools_list)
        return self._get_standard_plan_prompt(image_name, tools_list)

    def _get_repo_dockerfile_prompt(self, image_name: str, tools_list: str) -> str:
        """Configuration prompt when using repository Dockerfile"""
        pkg_manager = "uv pip install" if self.use_uv else "pip install"

        # Add uv installation instructions if using uv
        uv_install_note = ""
        if self.use_uv:
            uv_install_note = """
**Important: Using uv Package Manager**
- This environment is configured to use uv as the package manager (faster than pip)
- If you encounter `uv: command not found` error, install uv first:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="/root/.cargo/bin:$PATH" && ln -sf /root/.cargo/bin/uv /usr/local/bin/uv
  ```
- Verify after installation: `uv --version`
- Then use `uv pip install` instead of `pip install`
"""

        return f"""
You are an expert proficient in testing. The current environment has been built using the repository's Dockerfile (image: {image_name}), and the environment configuration should be complete.
{uv_install_note}

Your main tasks are:
1. Verify that the environment is working correctly
2. Create test cases
3. Run tests and ensure they pass

Workflow:
1. Quick environment verification: Check if key commands and dependencies are available
2. Use the construct-test tool to create test cases
3. Run tests (run-pytest-collect and run-pytest)
4. If tests fail, analyze the cause:
   - If it's ModuleNotFoundError/ImportError: May need to install missing dependencies
   - If it's other errors: Analyze whether it's a test case issue or environment issue
5. Fix issues and re-test
6. Call stop after tests pass

CLI Tool Usage Instructions:
All operations are performed inside Docker container {image_name}
Think about what to do first, then wrap commands with {BASH_FENCE[0]} ... {BASH_FENCE[1]}, for example:
### Thought: I need to verify the Python environment
### Action:
{BASH_FENCE[0]}
    python --version && pip --version
{BASH_FENCE[1]}

Available tools (callable but not terminal built-in commands):
{tools_list}

Important Notes:
1. Environment has been built via repository Dockerfile, most dependencies should already be installed
2. No need to configure environment from scratch, just verify and supplement
3. Prioritize using construct-test to create test cases
4. Test goal: Ensure run-pytest-collect and run-pytest can run successfully
5. If encountering ModuleNotFoundError, install the corresponding package (using mirror sources)
6. Do not modify test functions to make tests pass
7. Keep commands on a single line, use && to connect
8. To ensure tools are available, install pytest and openai first: {pkg_manager} -q pytest openai -i https://mirrors.aliyun.com/pypi/simple

Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_standard_plan_prompt(self, image_name: str, tools_list: str) -> str:
        """Standard Python configuration prompt"""
        pkg_manager = "uv pip install" if self.use_uv else "pip install"

        # Add uv installation instructions if using uv
        uv_install_note = ""
        if self.use_uv:
            uv_install_note = """
**Important: Using uv Package Manager**
- This environment is configured to use uv as the package manager (faster than pip)
- If you encounter `uv: command not found` error, install uv first:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="/root/.cargo/bin:$PATH" && ln -sf /root/.cargo/bin/uv /usr/local/bin/uv
  ```
- Verify after installation: `uv --version`
- Then use `uv pip install` instead of `pip install`
"""

        return f"""
You are an expert proficient in environment configuration. You can reference various environment configuration-related files and structures in the repository (such as requirements.txt, setup.py, etc.) and use tools to install and download corresponding third-party libraries in the specified Docker image. This ensures the repository can be successfully configured and execute specified tests correctly.
{uv_install_note}

You are now working inside Docker container {image_name}. The workflow is:
0. Explore the repository to understand the complete project structure.
1. Create viable test cases and locate the program entry point.
2. Examine the repository's directory structure, read and understand environment configuration-related files, which may include but are not limited to README.md, CONTRIBUTING.md, *.md, *.txt, docs/, information in .github folder, setup.py, setup.cfg, Pipfile, Pipfile.lock, environment.yml, poetry.lock, pyproject.toml, etc. From these, identify recommended installation methods and environment configuration plans.
3. Collect third-party library dependency lists such as pyproject.toml, requirements.txt, requirements_dev.txt, etc.
4. Install based on collected dependency files. The default source usually doesn't work, so use Tsinghua/Aliyun mirrors, for example: {pkg_manager} -q -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple.
5. Check if tests pass. If they pass, call stop.

CLI Tool Usage Instructions:
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
1. Test goal: Ensure the environment can run `run-pytest-collect` and `run-pytest` tools:
  - First ensure `run-pytest-collect` can successfully collect test cases
  - Then run test cases. You need to prioritize resolving `ModuleNotFoundError` and `ImportError` errors reported by `run-pytest`, as these are directly related to environment configuration
  - Other errors caused by test case issues or missing inaccessible APIs can be ignored, but you should still try your best to fix them
  - If run-pytest-collect collects 0 tests, then aim to make construct-test work
3. Do not modify test functions to make tests pass
4. Try to keep commands as single-line input to avoid parsing errors from multi-line commands. No need to execute all commands at once, and ensure each command is based on your existing knowledge of this repository.
5. Before executing any command, evaluate its potential impact on the system, and achieve the best results with minimal changes.
6. The configuration process allows flexibility. If unsure whether configuration is complete, you can run test programs at any time to check, and continue adjusting based on returned errors.
7. Code modification scenarios: Use the edit-file tool to modify code to help make things work when:
  - Creating new files
  - Fixing incorrect import paths (e.g., changing relative imports to absolute imports)
  - Adjusting configuration file parameters (e.g., modifying ports, paths, etc.)
  - Hard-coded dependency versions in code are incompatible with the environment
  - Fixing obvious code errors (e.g., syntax errors, API call errors, etc.)
  When using edit-file, prioritize using --mode llm for intelligent modifications, or use --mode search for precise replacements
8. If you find CI/CD files, you can call the cicd-config tool to configure the environment.
9. When version switching can solve the issue, consider switching Python versions (try not to edit existing files), use the change-python-version tool.
10. Do not use sed/awk to modify Python code
11. If downloads are needed during environment configuration, consider that you are in mainland China and need to use an appropriate mirror source to avoid timeouts. Handling timeout issues: If a package download times out, install that package separately, and check README for alternative installation methods
12. If you encounter problems you cannot resolve, use retrieve_issue to find solutions from GitHub issues.

Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_custom_plan_prompt(
        self, image_name: str, tools_list: str, language, max_turn
    ) -> str:
        """Python configuration prompt for custom plan mode"""
        pkg_manager = "uv pip install" if self.use_uv else "pip install"

        # Add uv installation instructions if using uv
        uv_install_note = ""
        if self.use_uv:
            uv_install_note = """
**Important: Using uv Package Manager**
- This environment is configured to use uv as the package manager (faster than pip)
- If you encounter `uv: command not found` error, install uv first:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh && export PATH="/root/.cargo/bin:$PATH" && ln -sf /root/.cargo/bin/uv /usr/local/bin/uv
  ```
- Verify after installation: `uv --version`
- Then use `uv pip install` instead of `pip install`
"""

        return f"""
You are an expert proficient in environment configuration. You need to configure the environment based on the repository's custom plan file `plan.md`. You can use the `/repo/plan.md` file as your task plan (it doesn't exist initially). This file contains the goals, phases, and progress of environment configuration.
Additionally, the repository you're configuring has {language} as its main language, so you need to configure the environment accordingly.

Note: When installing Python packages, use the `{pkg_manager}` command, for example: {pkg_manager} -q -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
{uv_install_note}

## Two-Phase Workflow
### Phase One: Check and Create Plan (if it doesn't exist)
1. First check if the `/repo/plan.md` file exists:
2. If the file doesn't exist, you need to:
   - Explore repository structure to understand the project (especially Dockerfile or README files, which may contain the entire configuration process, best to capture from there)
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
- [ ] Find dependency files (requirements.txt, pyproject.toml, etc.)
- **Status**: pending

### Phase 2: Dependency Installation
<!-- Install dependencies -->
- [ ] Install system dependencies (if any)
- [ ] Handle version conflicts (if any)
- [ ] Install development dependencies 
- **Status**: pending

### Phase 3: Environment Configuration
<!-- Environment configuration -->
- [ ] Configure environment variables
- [ ] Set up database (if needed)
- [ ] Set up paths and permissions
- **Status**: pending

### Phase 4: Testing & Validation
<!-- Testing and validation -->
- [ ] Run basic tests
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
        Return Python Dockerfile generation logic

        Note: This method has been replaced by SetupAgent's dynamic generation.
        Only used as a fallback when SetupAgent build fails.
        """
        """Return Python Dockerfile generation logic"""
        # If using uv, return uv version of Dockerfile
        if self.use_uv:
            return self._get_uv_dockerfile(base_image, version)

        # Extract version number for comparison
        if version == "latest":
            is_new_version = True
        else:
            # Simple version comparison (assuming version format is x.y)
            try:
                major, minor = map(int, version.split(".")[:2])
                is_new_version = (major > 3) or (
                    major == 3 and minor >= 9
                )  # Python 3.9 and above use new version configuration
            except Exception:
                is_new_version = (
                    True  # If parsing fails, default to new version configuration
                )

        if is_new_version:
            return f"""FROM {base_image}:{version}
RUN mkdir -p ~/.pip && touch ~/.pip/pip.conf
RUN echo "[global]" >> ~/.pip/pip.conf && echo "[install]" >> ~/.pip/pip.conf

# Install necessary system dependencies
RUN apt-get update && apt-get install -y curl

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | python -
# Add Poetry's bin directory to PATH environment variable
ENV PATH="/root/.local/bin:$PATH"

RUN pip install pytest
RUN pip install openai -i https://mirrors.aliyun.com/pypi/simple
RUN pip install pipdeptree

RUN git config --global --add safe.directory /repo
"""
        else:
            return f"""FROM {base_image}:{version}
RUN mkdir -p ~/.pip && touch ~/.pip/pip.conf
RUN echo "[global]" >> ~/.pip/pip.conf && echo "[install]" >> ~/.pip/pip.conf

# Install necessary system dependencies
RUN apt-get update && apt-get install -y curl

RUN pip install pytest
RUN pip install openai -i https://mirrors.aliyun.com/pypi/simple

RUN pip install pipdeptree

RUN git config --global --add safe.directory /repo
"""

    def _get_uv_dockerfile(self, base_image: str, version: str) -> str:
        """Return Dockerfile using uv"""
        return f"""FROM {base_image}:{version}
RUN mkdir -p ~/.pip && touch ~/.pip/pip.conf
RUN echo "[global]" >> ~/.pip/pip.conf && echo "[install]" >> ~/.pip/pip.conf

# Install necessary system dependencies
RUN apt-get update && apt-get install -y curl

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Create symbolic link to ensure uv is available in standard path
RUN ln -sf /root/.cargo/bin/uv /usr/local/bin/uv || true

# Verify uv installation
RUN uv --version || echo "Warning: uv installation may have issues"

RUN pip install pytest
RUN pip install openai -i https://mirrors.aliyun.com/pypi/simple
RUN pip install pipdeptree

RUN git config --global --add safe.directory /repo
"""

    def get_dependency_files(self) -> List[str]:
        """Return Python dependency file list"""
        return [
            "requirements.txt",
            "requirements_dev.txt",
            "requirements-dev.txt",
            "setup.py",
            "setup.cfg",
            "pyproject.toml",
            "Pipfile",
            "Pipfile.lock",
            "poetry.lock",
            "environment.yml",
        ]

    def get_test_runner_tools(self) -> Dict[str, Dict]:
        """Return Python test runner tool configuration"""
        return {
            "run-pytest": {
                "script": "run_pytest.py",
                "timeout": 660,
                "return_codes": [
                    0,
                    1,
                ],  # 0: all passed, 1: some failed but execution succeeded
                "description": "Automatically run all pytest tests in the repository (/repo). Usage: run-pytest (no parameters needed). Features: automatically discover test files, execute all tests, count passed/failed numbers, categorize error types (ModuleNotFoundError/ImportError/etc.). Results saved to /repo/logs/run_pytest_results.json",
            },
            "run-pytest-collect": {
                "script": "run_pytest_collect.py",
                "timeout": 120,
                "return_codes": [
                    0,
                    5,
                ],  # 0: collection succeeded, 5: nothing collected but execution succeeded
                "description": "Collect but do not execute pytest test cases in the repository (/repo). Usage: run-pytest-collect (no parameters needed). Features: automatically discover and collect all test files and test functions, verify tests can be recognized, check import errors. Results saved to /repo/logs/run_pytest_collect_results.json",
            },
        }

    def get_package_manager_commands(self) -> Dict[str, str]:
        """Return Python package manager commands"""
        if self.use_uv:
            return {
                "install": "uv pip install",
                "install_file": "uv pip install -r",
                "list": "uv pip list",
                "freeze": "uv pip freeze",
                "show": "uv pip show",
                "uninstall": "uv pip uninstall",
                "mirror_aliyun": "-i https://mirrors.aliyun.com/pypi/simple",
                "mirror_tsinghua": "-i https://pypi.tuna.tsinghua.edu.cn/simple",
            }
        return {
            "install": "pip install",
            "install_file": "pip install -r",
            "list": "pip list",
            "freeze": "pip freeze",
            "show": "pip show",
            "uninstall": "pip uninstall",
            "mirror_aliyun": "-i https://mirrors.aliyun.com/pypi/simple",
            "mirror_tsinghua": "-i https://pypi.tuna.tsinghua.edu.cn/simple",
        }

    def get_env_detection_commands(self) -> List[str]:
        """Return Python environment detection commands"""
        return [
            "python --version",
            "python3 --version",
            "pip --version",
            "pip list",
        ]

    def get_test_result_files(self) -> List[Dict[str, str]]:
        """Return Python test result file list"""
        return [
            {
                "source": "/repo/logs/run_pytest_results.json",
                "dest_name": "run_pytest_results.json",
                "description": "Pytest execution results",
            },
            {
                "source": "/repo/logs/run_pytest_collect_results.json",
                "dest_name": "run_pytest_collect_results.json",
                "description": "Pytest collection results",
            },
            {
                "source": "/repo/logs/junit_report.xml",
                "dest_name": "junit_report.xml",
                "description": "JUnit XML report",
            },
        ]

    def get_language_specific_config(self) -> Dict:
        """Return Python-specific additional configuration"""
        return {
            "virtual_env_support": True,
            "poetry_support": True,
            "conda_support": False,
            "default_test_framework": "pytest",
        }

    def get_version_change_tool(self) -> Dict[str, str]:
        """Return Python version switching tool configuration"""
        return {
            "tool_name": "change-python-version",
            "method_name": "change_python_version",
            "emoji": "🐍",
            "example": "change-python-version 3.10",
        }
