"""Unified language detection utility.

Provides multiple detection strategies:
1. GitHub API (most accurate, requires network)
2. Local file detection (offline, based on common config files)

Examples:
    detector = LanguageDetector()

    # Option 1: GitHub API
    language = detector.detect_language(full_name="owner/repo")

    # Option 2: local file detection
    language = detector.detect_language(project_path="/path/to/project")
"""

from pathlib import Path
from typing import Optional

import requests

from libkit.config import config


class LanguageDetector:
    """Unified programming language detector."""

    def __init__(self):
        """Initialize the detector."""
        self.github_token = config.GITHUB_TOKEN

    def detect_language(
        self,
        full_name: Optional[str] = None,
        project_path: Optional[str] = None,
        prefer_github_api: bool = True,
    ) -> str:
        """
        Detect the primary programming language of a project.

        Strategy priority:
        1. If full_name is provided and prefer_github_api=True, use GitHub API
        2. If GitHub API fails or is not provided, use local file detection
        3. If all fail, return the default value "python"

        Args:
            full_name: GitHub repo full name, "owner/repo".
            project_path: Local project path.
            prefer_github_api: Prefer GitHub API when possible (default: True).

        Returns:
            Detected language (lowercase), e.g. "python", "java", "node".
        """
        detected_language = "python"  # Default

        # 1. Try GitHub API
        if full_name and prefer_github_api:
            try:
                language = self._detect_from_github_api(full_name)
                if language:
                    return language
            except Exception as e:
                print(
                    f"⚠️  GitHub API detection failed: {e}; falling back to local detection"
                )

        # 2. Fall back to local detection
        if project_path:
            try:
                language = self._detect_locally(project_path)
                if language:
                    return language
            except Exception as e:
                print(f"⚠️  Local file detection failed: {e}")

        # 3. Return default
        return detected_language

    def _detect_from_github_api(self, full_name: str) -> Optional[str]:
        """
        Detect language using the GitHub API.

        Args:
            full_name: GitHub repo full name "owner/repo".

        Returns:
            Detected primary language name; None on failure.
        """
        url = f"https://api.github.com/repos/{full_name}/languages"

        # Build request headers
        headers = {}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            languages = response.json()

            if not languages:
                return None

            # Return the language with the most code
            main_language = max(languages, key=languages.get)

            # Normalize language name
            return self._normalize_language_name(main_language)

        except requests.exceptions.RequestException as e:
            raise Exception(f"GitHub API request failed: {e}")

    def _detect_locally(self, project_path: str) -> Optional[str]:
        """
        Detect language from local files.

        Args:
            project_path: Local project path.

        Returns:
            Detected language.
        """
        path = Path(project_path)
        if not path.exists():
            return None

        # Config file -> language mapping
        config_files = {
            "pom.xml": "java",
            "build.gradle": "java",
            "build.gradle.kts": "java",
            "package.json": "node",
            "go.mod": "go",
            "Cargo.toml": "rust",
            "requirements.txt": "python",
            "setup.py": "python",
            "pyproject.toml": "python",
            "setup.cfg": "python",
            "Pipfile": "python",
            "poetry.lock": "python",
        }

        # Check config files
        for config_file, language in config_files.items():
            if (path / config_file).exists():
                return language

        return None

    def _normalize_language_name(self, language: str) -> str:
        """
        Normalize language name.

        Args:
            language: Raw language name.

        Returns:
            Normalized (lowercase) language name.
        """
        language = language.lower()

        # Language name mapping
        name_map = {
            "javascript": "node",
            "typescript": "node",
            "c++": "cpp",
            "golang": "go",
        }

        return name_map.get(language, language)


# Global singleton instance (convenience)
_default_detector = None


def get_detector() -> LanguageDetector:
    """Get the global default detector instance."""
    global _default_detector
    if _default_detector is None:
        _default_detector = LanguageDetector()
    return _default_detector


def detect_language(
    full_name: Optional[str] = None,
    project_path: Optional[str] = None,
    prefer_github_api: bool = True,
) -> str:
    """
    Convenience helper: detect project language.

    Uses the global default detector instance.
    """
    detector = get_detector()
    return detector.detect_language(
        full_name=full_name,
        project_path=project_path,
        prefer_github_api=prefer_github_api,
    )
