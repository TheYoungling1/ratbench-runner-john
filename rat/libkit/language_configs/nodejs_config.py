#!/usr/bin/env python3
"""
Node.js Language Configuration
Supports JavaScript and TypeScript projects
"""

from typing import List, Dict
from libkit.language_config import LanguageConfig
from libkit.tool import ToolKit, BASH_FENCE


class NodeJSConfig(LanguageConfig):
    """Environment configuration for Node.js language (supports JavaScript and TypeScript)"""

    @property
    def language_name(self) -> str:
        return "Node.js"

    def get_toolkit(self) -> List:
        """Return Node.js-specific tool list"""
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
        """Return initial system prompt for Node.js"""
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
3. Run tests (run-npm-install and run-npm-test)
4. If tests fail, analyze the cause:
   - If it's module not found errors: May need to install missing dependencies
   - If it's version conflicts: Check version requirements in package.json
   - If it's other errors: Analyze whether it's a test case issue or environment issue
5. Fix issues and re-test
6. Call stop after tests pass

CLI Tool Usage Instructions:
All operations are performed inside Docker container {image_name}
Think about what to do first, then wrap commands with {BASH_FENCE[0]} ... {BASH_FENCE[1]}, for example:
### Thought: I need to verify the Node.js environment
### Action:
{BASH_FENCE[0]}
    node --version && npm --version
{BASH_FENCE[1]}

Available tools (callable but not terminal built-in commands):
{tools_list}

Important Notes:
1. Environment has been built via repository Dockerfile, most dependencies should already be installed
2. No need to configure environment from scratch, just verify and supplement
3. Prioritize using construct-test to create test cases
4. Test goal: Ensure run-npm-install and run-npm-test can run successfully
5. If encountering module not found errors, install the corresponding package (using npmmirror mirror source)
6. Do not modify test functions to make tests pass
7. Keep commands on a single line, use && to connect
8. To ensure tools are available, install basic dependencies first: npm install

Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_standard_plan_prompt(self, image_name: str, tools_list: str) -> str:
        """Standard Node.js configuration prompt"""
        return f"""
You are an expert proficient in Node.js environment configuration. You can reference various files and structures in the repository, such as package.json, package-lock.json, yarn.lock, etc., and install and configure corresponding dependencies in the specified Docker image. This ensures the repository can be successfully configured and execute specified tests correctly.
Note: This repository originally had no Dockerfile, or the existing Dockerfile has been deleted. Do not attempt to use the repository's original Dockerfile information.
Your workflow is:
0. Explore the repository to understand the complete project structure and the image configuration.
1. Locate the program entry point, create viable test cases, and check if tests can pass directly without additional configuration.
2. Read and understand important documentation in the repository, which may include but is not limited to README.md, CONTRIBUTING.md, *.md, *.txt, content in docs/, etc.
3. Read the repository directory and files related to environment configuration, such as package.json, package-lock.json, yarn.lock, pnpm-lock.yaml, .nvmrc, .npmrc, etc. Consider other files and structures that may be used for environment configuration.
4. Collect dependency information:
   - Check dependencies and devDependencies in package.json
   - Detect the package manager being used (npm/yarn/pnpm):
     * If yarn.lock exists, use yarn
     * If pnpm-lock.yaml exists, use pnpm
     * Otherwise use npm
5. Install dependencies:
   - Use the detected package manager to install dependencies
   - npm: npm install --registry=https://registry.npmmirror.com
   - yarn: yarn install --registry=https://registry.npmmirror.com
   - pnpm: pnpm install --registry=https://registry.npmmirror.com
6. Check if tests pass. If they pass, call stop.

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
2. Test goal: Ensure the environment can run `run-npm-install` and `run-npm-test` tools:
  - First ensure `run-npm-install` can successfully install all dependencies
  - Then run test cases. You need to prioritize resolving module not found errors and import errors reported by `run-npm-test`, as these are directly related to environment configuration
  - Other errors caused by test case issues or missing inaccessible APIs can be ignored, but you should still try your best to fix them
3. Do not modify test functions to make tests pass
4. Try to keep commands as single-line input to avoid parsing errors from multi-line commands
5. No need to execute all commands at once. Explore step by step, and ensure each command is based on your existing knowledge of this repository.
6. Before executing any command, evaluate its potential impact on the system, and achieve the best results with minimal changes. Do not use git clone, wget, or other external tools to batch download files in the /repo directory (or its subdirectories) to avoid major changes to the original repository.
7. The configuration process allows flexibility. If unsure whether configuration is complete, you can run test programs at any time to check, and continue adjusting based on returned errors.
8. Common Node.js package managers:
   - npm: Default package manager
   - yarn: Faster dependency installation
   - pnpm: Disk space-saving package manager
9. Code modification scenarios: Use the edit-file tool to modify code to help make things work when:
  - Fixing incorrect import paths (e.g., changing relative imports to absolute imports)
  - Adjusting configuration file parameters (e.g., modifying ports, paths, etc.)
  - Hard-coded dependency versions in code are incompatible with the environment
  - Need to comment out certain dependency checks that cannot be satisfied
  - Fixing obvious code errors (e.g., syntax errors, API call errors, etc.)
  When using edit-file, prioritize using --mode llm for intelligent modifications, or use --mode search for precise replacements
10. If you find CI/CD files, you can call the cicd-config tool to configure the environment.


Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_custom_plan_prompt(
        self, image_name: str, tools_list: str, language, max_turn
    ) -> str:
        """Node.js configuration prompt for custom plan mode"""
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
- [ ] Find dependency files (package.json, package-lock.json, etc.)
- **Status**: pending

### Phase 2: Dependency Installation
<!-- Install dependencies -->
- [ ] Detect package manager (npm/yarn/pnpm)
- [ ] Install dependencies
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
- [ ] Run tests
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
        Return Node.js Dockerfile generation logic

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

# Configure npm to use Chinese mirror source (npmmirror, formerly Taobao mirror)
RUN npm config set registry https://registry.npmmirror.com

# Globally install common tools
RUN npm install -g pnpm

# Configure yarn and pnpm mirror sources
RUN yarn config set registry https://registry.npmmirror.com
RUN pnpm config set registry https://registry.npmmirror.com

# Configure git safe directory
RUN git config --global --add safe.directory /repo
"""

    def get_dependency_files(self) -> List[str]:
        """Return Node.js dependency file list"""
        return [
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            ".npmrc",
            ".yarnrc",
            ".nvmrc",
        ]

    def get_test_runner_tools(self) -> Dict[str, Dict]:
        """Return Node.js test runner tool configuration"""
        return {
            "run-npm-install": {
                "script": "run_npm_install.py",
                "timeout": 600,
                "return_codes": [
                    0,
                    1,
                ],  # 0: all succeeded, 1: some failed but execution succeeded
                "description": "Install npm dependencies. Usage: run-npm-install (no parameters needed). Features: automatically detect package manager (npm/yarn/pnpm), install all dependencies, handle version conflicts, record installation errors. Results saved to /repo/logs/run_npm_install_results.json",
            },
            "run-npm-test": {
                "script": "run_npm_test.py",
                "timeout": 900,
                "return_codes": [
                    0,
                    1,
                ],  # 0: all passed, 1: some failed but execution succeeded
                "description": "Run npm tests. Usage: run-npm-test (no parameters needed). Features: execute npm test command, support Jest/Mocha/etc test frameworks, count passed/failed numbers, categorize error types. Results saved to /repo/logs/run_npm_test_results.json",
            },
        }

    def get_package_manager_commands(self) -> Dict[str, str]:
        """Return Node.js package manager commands"""
        return {
            "npm_install": "npm install",
            "npm_test": "npm test",
            "npm_build": "npm run build",
            "npm_list": "npm list",
            "yarn_install": "yarn install",
            "yarn_test": "yarn test",
            "pnpm_install": "pnpm install",
            "pnpm_test": "pnpm test",
            "mirror_npmmirror": "--registry=https://registry.npmmirror.com",
        }

    def get_env_detection_commands(self) -> List[str]:
        """Return Node.js environment detection commands"""
        return [
            "node --version",
            "npm --version",
            "yarn --version",
            "pnpm --version",
        ]

    def get_test_result_files(self) -> List[Dict[str, str]]:
        """Return Node.js test result file list"""
        return [
            {
                "source": "/repo/logs/run_npm_install_results.json",
                "dest_name": "run_npm_install_results.json",
                "description": "npm dependency installation results",
            },
            {
                "source": "/repo/logs/run_npm_test_results.json",
                "dest_name": "run_npm_test_results.json",
                "description": "npm test execution results",
            },
        ]

    def get_language_specific_config(self) -> Dict:
        """Return Node.js-specific additional configuration"""
        return {
            "package_managers": ["npm", "yarn", "pnpm"],
            "test_frameworks": ["jest", "mocha", "vitest", "ava", "tape"],
            "default_package_manager": "npm",
            "default_test_framework": "jest",
            "supports_typescript": True,
        }

    def get_version_change_tool(self) -> Dict[str, str]:
        """Return Node.js version switching tool configuration"""
        return {
            "tool_name": "change-node-version",
            "method_name": "change_node_version",
            "emoji": "🟢",
            "example": "change-node-version 20",
        }
