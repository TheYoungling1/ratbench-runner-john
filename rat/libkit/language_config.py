#!/usr/bin/env python3
"""
Language config abstraction layer - provides a unified interface across languages.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class LanguageConfig(ABC):
    """Abstract base class for language environment configuration.

    Each language (Python, Java, Node.js, etc.) should implement this interface,
    providing language-specific tools, prompts, Dockerfile behavior, etc.
    """

    @property
    @abstractmethod
    def language_name(self) -> str:
        """Language name, e.g. 'Python', 'Java', 'Node.js'.

        Returns:
            str: Standard language name.
        """
        pass

    @abstractmethod
    def get_toolkit(self) -> List:
        """Return the tool list supported by this language.

        Returns:
            List[ToolKit]: ToolKit enum list.
        """
        pass

    @abstractmethod
    def get_init_prompt(
        self,
        image_name: str,
        tools_list: str,
        use_custom_plan: bool = False,
        max_turn: int = 25,
        use_repo_dockerfile: bool = False,
    ) -> str:
        """Return the initial system prompt for this language.

        Args:
            image_name: Docker image name.
            tools_list: Formatted tool list string.
            use_custom_plan: Whether to use a custom planning mode.
            max_turn: Maximum turns.
            use_repo_dockerfile: Whether to use the repo-provided Dockerfile.

        Returns:
            str: System init prompt.
        """
        pass

    def get_dockerfile_content(self, base_image: str, version: str, **kwargs) -> str:
        """Return Dockerfile generation logic (optional, deprecated).

        Note: this is superseded by SetupAgent's dynamic generation.
        Kept only as a fallback when SetupAgent build fails.

        Args:
            base_image: Base image name (e.g. 'python', 'eclipse-temurin').
            version: Version (e.g. '3.10', '11').

        Returns:
            str: Dockerfile content.

        Raises:
            NotImplementedError: If not implemented by a subclass.
        """
        raise NotImplementedError(
            f"{self.language_name} did not implement get_dockerfile_content; "
            "use SetupAgent to generate Dockerfile dynamically"
        )

    @abstractmethod
    def get_dependency_files(self) -> List[str]:
        """Return dependency file names.

        Returns:
            List[str]: Dependency config file names.
                      e.g. Python: ['requirements.txt', 'setup.py', 'pyproject.toml']
                      e.g. Java: ['pom.xml', 'build.gradle']
        """
        pass

    @abstractmethod
    def get_test_runner_tools(self) -> Dict[str, Dict]:
        """Return test runner tool configurations.

        Returns:
            Dict[str, Dict]: Tool config dict.
                key: tool name (e.g. 'run-pytest', 'run-maven-test')
                value: tool config dict containing:
                    - script: tool script filename
                    - timeout: timeout in seconds
                    - return_codes: list of successful return codes

        Example:
            {
                "run-pytest": {
                    "script": "run_pytest.py",
                    "timeout": 660,
                    "return_codes": [0, 1]
                }
            }
        """
        pass

    @abstractmethod
    def get_package_manager_commands(self) -> Dict[str, str]:
        """Return package manager command mapping.

        Returns:
            Dict[str, str]: Package manager commands.
                e.g. Python: {'install': 'pip install', 'list': 'pip list'}
                e.g. Java: {'install': 'mvn install', 'list': 'mvn dependency:list'}
        """
        pass

    @abstractmethod
    def get_test_result_files(self) -> List[Dict[str, str]]:
        """Return test result files to copy from container to host.

        Returns:
            List[Dict[str, str]]: List of file specs.
                - source: path inside container
                - dest_name: destination filename on host
                - description: description

        Example:
            [
                {
                    "source": "/repo/logs/run_pytest_results.json",
                    "dest_name": "run_pytest_results.json",
                    "description": "Pytest results",
                }
            ]
        """
        pass

    def get_language_specific_config(self) -> Dict:
        """Return extra language-specific configuration (optional).

        Returns:
            Dict: Extra config dict.
        """
        return {}

    def validate_environment(self, session) -> tuple:
        """Validate whether the environment is configured correctly (optional).

        Args:
            session: Environment session.

        Returns:
            tuple: (success, message)
        """
        return True, f"{self.language_name} environment validation not implemented"

    def get_env_detection_commands(self) -> List[str]:
        """Return environment detection commands (optional).

        Returns:
            List[str]: Commands to probe the environment.
                e.g. Python: ['python --version', 'pip --version']
                e.g. Java: ['java -version', 'mvn -version']
        """
        return []

    def get_version_change_tool(self) -> Optional[Dict[str, str]]:
        """Return version switch tool config (optional).

        Returns:
            Optional[Dict[str, str]]: Version switch tool config containing:
                - tool_name: tool name (e.g. 'change-python-version')
                - method_name: env method name (e.g. 'change_python_version')
                - emoji: display emoji (e.g. '🐍')
                - example: usage example (e.g. 'change-python-version 3.10')
            Return None if version switching is not supported.

        Example:
            {
                "tool_name": "change-python-version",
                "method_name": "change_python_version",
                "emoji": "🐍",
                "example": "change-python-version 3.10"
            }
        """
        return None
