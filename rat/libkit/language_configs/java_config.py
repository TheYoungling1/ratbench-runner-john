#!/usr/bin/env python3
"""
Java Language Configuration
"""

from typing import List, Dict
from libkit.language_config import LanguageConfig
from libkit.tool import ToolKit, BASH_FENCE


class JavaConfig(LanguageConfig):
    """Environment configuration for Java language"""

    def __init__(self, is_maven: bool = True, is_gradle: bool = False):
        """Initialize Java configuration

        Args:
            is_maven: Whether it's a Maven project (default True)
            is_gradle: Whether it's a Gradle project (default False)
        """
        self.is_maven = is_maven
        self.is_gradle = is_gradle

    @property
    def language_name(self) -> str:
        return "Java"

    def get_toolkit(self) -> List:
        """Return Java-specific tool list"""
        return [
            ToolKit.retrieve_issue,
            ToolKit.detect_environment,
            ToolKit.cicd_config,
            ToolKit.construct_test,
            ToolKit.run_test,
            # Java-specific test tools will be dynamically added via get_test_runner_tools
            ToolKit.search_repo,
            ToolKit.read_file,
            ToolKit.view_outline,
            ToolKit.search_web,
            ToolKit.ls_structure,
            ToolKit.change_java_version,
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
        """Return initial system prompt for Java"""
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
3. Run tests (run-maven-test or run-gradle-test)
4. If tests fail, analyze the cause:
   - If it's missing dependencies: May need to install missing dependencies
   - If it's compilation errors: Check if Java version or dependency versions match
   - If it's other errors: Analyze whether it's a test case issue or environment issue
5. Fix issues and re-test
6. Call stop after tests pass

CLI Tool Usage Instructions:
All operations are performed inside Docker container {image_name}
Think about what to do first, then wrap commands with {BASH_FENCE[0]} ... {BASH_FENCE[1]}, for example:
### Thought: I need to verify the Java environment
### Action:
{BASH_FENCE[0]}
    java -version && mvn -version
{BASH_FENCE[1]}

Available tools (callable but not terminal built-in commands):
{tools_list}

Important Notes:
1. Environment has been built via repository Dockerfile, most dependencies should already be installed
2. No need to configure environment from scratch, just verify and supplement
3. Prioritize using construct-test to create test cases
4. Test goal: Ensure run-maven-test or run-gradle-test can run successfully
5. If encountering missing dependencies, install the corresponding package
6. Do not modify test functions to make tests pass
7. Keep commands on a single line, use && to connect
8. To ensure tools are available, install pytest and openai first (if not already installed)

Special Note:
Only output **one** {BASH_FENCE[0]} ... {BASH_FENCE[1]} wrapped command at a time
    """

    def _get_standard_plan_prompt(self, image_name: str, tools_list: str) -> str:
        """Standard Java configuration prompt"""
        return f"""
You are an expert proficient in Java environment configuration. You can reference various files and structures in the repository, such as pom.xml, build.gradle, etc., to configure Java project environment in the specified Docker image. This ensures the repository can be successfully configured and execute specified tests correctly.
Note: This repository originally had no Dockerfile, or the existing Dockerfile has been deleted. Do not attempt to use the repository's original Dockerfile information.
Your workflow is:
0. Explore the repository to understand the complete project structure and the image configuration.
1. Read and understand important documentation in the repository, which may include but is not limited to README.md, CONTRIBUTING.md, *.md, *.txt, content in docs/, etc.
2. Read the repository directory and files related to environment configuration, such as pom.xml, build.gradle, build.gradle.kts, settings.gradle, etc. Consider other files and structures that may be used for environment configuration.
3. Collect dependency information:
   - For Maven projects, check pom.xml file
   - For Gradle projects, check build.gradle or build.gradle.kts file
4. Install dependencies and build the project:
   - Maven project: Call the run-maven-install tool
   - Gradle project: Call the run-gradle-build tool
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
2. Build goal: Ensure the environment can run `run-maven-install` or `run-gradle-build` tools:
  - Ensure the project can be successfully compiled and built (without running tests)
  - Prioritize resolving compilation errors and missing dependency issues
  - Tools will execute mvnw/mvn clean install -DskipTests or gradlew/gradle clean build -x test
  - As long as the build succeeds, no need to run test cases
3. Do not modify core business code to pass the build
4. Try to keep commands as single-line input to avoid parsing errors from multi-line commands
5. No need to execute all commands at once. Explore step by step, and ensure each command is based on your existing knowledge of this repository.
6. Before executing any command, evaluate its potential impact on the system, and achieve the best results with minimal changes. Do not use git clone, wget, or other external tools to batch download files in the /repo directory (or its subdirectories) to avoid major changes to the original repository.
7. The configuration process allows flexibility. If unsure whether configuration is complete, you can run test programs at any time to check, and continue adjusting based on returned errors.
8. Common Java dependency management tools:
   - Maven: Use mvn command
   - Gradle: Use gradle or ./gradlew command
9. Code modification scenarios: Use the edit-file tool to modify code to help make things work when:
  - Fixing incorrect import paths
  - Adjusting configuration file parameters (e.g., modifying ports, paths, etc.)
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
        """Java configuration prompt for custom plan mode"""
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
        Return Java Dockerfile generation logic

        Note: This method has been replaced by SetupAgent's dynamic generation.
        Only used as a fallback when SetupAgent build fails.
        """

        # Get from kwargs or use instance attributes
        is_maven = kwargs.get("is_maven", self.is_maven)
        is_gradle = kwargs.get("is_gradle", self.is_gradle)

        # Also update instance attributes to ensure subsequent calls to get_test_runner_tools() use correct values
        self.is_maven = is_maven
        self.is_gradle = is_gradle

        if is_maven:
            print("is_maven", base_image)
            return f"""FROM {base_image}:{version}
# Install Maven (if not included in base image)
RUN apt-get update && apt-get install -y maven
RUN apt-get update && apt-get install -y python3-pip curl git python-is-python3
# Link python3 as python for convenience
RUN mkdir -p ~/.pip && touch ~/.pip/pip.conf
RUN echo "[global]" >> ~/.pip/pip.conf && echo "[install]" >> ~/.pip/pip.conf
RUN curl -sSL https://install.python-poetry.org | python -
ENV PATH="/root/.local/bin:$PATH"
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

RUN pip install pytest
RUN pip install openai -i https://mirrors.aliyun.com/pypi/simple
RUN pip install pipdeptree --no-deps
RUN git config --global --add safe.directory /repo
"""
        else:
            # print("is_gradle", base_image)
            return f"""FROM {base_image}:{version}
# Install Gradle
RUN apt-get update && apt-get install -y curl unzip
# RUN curl -sSL https://mirrors.cloud.tencent.com/gradle/gradle-8.5-bin.zip -o /tmp/gradle.zip \
#     && unzip -d /opt /tmp/gradle.zip \
#     && ln -s /opt/gradle-8.5/bin/gradle /usr/bin/gradle
# Install necessary tools
RUN apt-get install -y git
RUN git config --global --add safe.directory /repo

RUN apt-get update && apt-get install -y python3-pip curl git python-is-python3
# Link python3 as python for convenience
RUN mkdir -p ~/.pip && touch ~/.pip/pip.conf
RUN echo "[global]" >> ~/.pip/pip.conf && echo "[install]" >> ~/.pip/pip.conf
RUN curl -sSL https://install.python-poetry.org | python -
# Add Poetry's bin directory to PATH environment variable
ENV PATH="/root/.local/bin:$PATH"
# 2. [Core Step] Remove system environment restriction file (unlock system-level pip)
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

# 3. Now you can install directly without --break-system-packages and without venv
# RUN pip install --upgrade pip
RUN pip install pytest
RUN pip install openai -i https://mirrors.aliyun.com/pypi/simple
RUN pip install pipdeptree --no-deps
# RUN ln -s /usr/bin/python3.10 /usr/bin/python
RUN git config --global --add safe.directory /repo
"""

    def get_dependency_files(self) -> List[str]:
        """Return Java dependency file list"""
        return [
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradle.properties",
            "ivy.xml",
        ]

    def get_test_runner_tools(self) -> Dict[str, Dict]:
        """Return Java test runner tool configuration

        Returns corresponding test tools based on project's build tool type (Maven or Gradle)
        """
        tools = {}

        if self.is_maven:
            tools["run-maven-install"] = {
                "script": "run_maven_install.py",
                "timeout": 900,
                "return_codes": [0, 1],  # 0: build succeeded, 1: build failed
                "description": "Build Maven project (skip tests). Usage: run-maven-install (no parameters needed). Features: execute mvnw/mvn clean install -DskipTests, compile source code, resolve and download dependencies, package project, count compilation errors and dependency issues. Results saved to /repo/logs/run_maven_install_results.json",
            }

        if self.is_gradle:
            tools["run-gradle-build"] = {
                "script": "run_gradle_build.py",
                "timeout": 900,
                "return_codes": [0, 1],  # 0: build succeeded, 1: build failed
                "description": "Build Gradle project (skip tests). Usage: run-gradle-build (no parameters needed). Features: execute gradlew/gradle clean build -x test, compile source code, resolve and download dependencies, package project, count compilation errors and dependency issues. Results saved to /repo/logs/run_gradle_build_results.json",
            }

        return tools

    def get_package_manager_commands(self) -> Dict[str, str]:
        """Return Java package manager commands"""
        return {
            "maven_install": "mvn clean install",
            "maven_test": "mvn test",
            "maven_compile": "mvn compile",
            "maven_package": "mvn package",
            "maven_dependency": "mvn dependency:resolve",
            "gradle_build": "gradle build",
            "gradle_test": "gradle test",
            "gradle_compile": "gradle compileJava",
            "gradlew_build": "./gradlew build",
            "gradlew_test": "./gradlew test",
        }

    def get_env_detection_commands(self) -> List[str]:
        """Return Java environment detection commands"""
        return [
            "java -version",
            "javac -version",
            "mvn -version",
            "gradle -version",
        ]

    def get_test_result_files(self) -> List[Dict[str, str]]:
        """Return Java test result file list"""
        result_files = []
        if self.is_maven:
            result_files.append(
                {
                    "source": "/repo/logs/run_maven_install_results.json",
                    "dest_name": "run_maven_install_results.json",
                    "description": "Maven test results",
                }
            )
        if self.is_gradle:
            result_files.append(
                {
                    "source": "/repo/logs/run_gradle_build_results.json",
                    "dest_name": "run_gradle_build_results.json",
                    "description": "Gradle test results",
                },
            )
        return result_files

    def get_language_specific_config(self) -> Dict:
        """Return Java-specific additional configuration"""
        return {
            "build_tools": ["maven", "gradle"],
            "test_frameworks": ["junit", "testng"],
            "default_test_framework": "junit",
            "jdk_required": True,
        }

    def get_version_change_tool(self) -> Dict[str, str]:
        """Return Java version switching tool configuration"""
        return {
            "tool_name": "change-java-version",
            "method_name": "change_java_version",
            "emoji": "☕",
            "example": "change-java-version 17",
        }
