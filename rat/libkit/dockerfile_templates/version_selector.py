"""
VersionSelector - select Docker image versions from an allowlist.
"""

import json
import re
from pathlib import Path
from typing import Dict, Optional, Tuple


class VersionSelector:
    """Select the most suitable Docker image version from an allowlist."""

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the version selector.

        Args:
            config_path: Path to ALLOWED_VERSIONS.json (defaults to this module dir)
        """
        if config_path is None:
            config_path = Path(__file__).parent / "ALLOWED_VERSIONS.json"

        self.config_path = Path(config_path)
        self.allowed_versions = self._load_config()

    def _load_config(self) -> Dict:
        """Load allowlisted version configuration."""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"❌ Failed to load version config: {e}")
            return {}

    def select_version_from_analysis(
        self, language: str, project_analysis: Dict
    ) -> Tuple[str, str]:
        """
        Select a version based on project analysis.

        Args:
            language: Programming language.
            project_analysis: Project analysis dict (version, dependencies, etc.).

        Returns:
            (version, variant) e.g. ("3.10", "slim")
        """
        language_lower = language.lower()

        if language_lower not in self.allowed_versions:
            print(f"⚠️  Unsupported language: {language}")
            return self.get_default_version(language_lower)

        # Extract version requirement from analysis
        detected_version = project_analysis.get("version", "")

        # Try to extract major/minor from detected version
        if detected_version:
            version = self._extract_major_minor_version(detected_version)
            if self.is_version_allowed(language_lower, version):
                variant = self._select_variant(language_lower, project_analysis)
                print(
                    f"  ✓ Selected from analysis: {version}{'-' + variant if variant else ''}"
                )
                return version, variant

        # Fall back to default
        print("  ℹ️  Could not determine version from analysis; using default")
        return self.get_default_version(language_lower)

    def select_version_with_llm(
        self, language: str, llm_response: str
    ) -> Tuple[str, str]:
        """
        Parse a version selection from an LLM response.

        Expected LLM response format (JSON):
        {"version": "3.10", "variant": "slim"}

        Args:
            language: Programming language.
            llm_response: LLM response text.

        Returns:
            (version, variant) e.g. ("3.10", "slim")
        """
        language_lower = language.lower()

        # Try extracting JSON from response
        try:
            # Find JSON
            json_match = re.search(r'\{[^}]*"version"[^}]*\}', llm_response)
            if json_match:
                data = json.loads(json_match.group())
                version = data.get("version", "")
                variant = data.get("variant", "")

                # Validate version/variant
                if self.validate_selection(language_lower, version, variant):
                    print(
                        f"  ✓ LLM selected: {version}{'-' + variant if variant else ''}"
                    )
                    return version, variant
                else:
                    print(f"  ⚠️  Invalid LLM selection: {version}-{variant}")
        except Exception as e:
            print(f"  ⚠️  Failed to parse LLM response: {e}")

        # Fall back to default
        print("  ℹ️  Using default version")
        return self.get_default_version(language_lower)

    def validate_selection(self, language: str, version: str, variant: str) -> bool:
        """
        Validate that a selected version/variant is allowed.

        Args:
            language: Programming language.
            version: Version string.
            variant: Variant (may be empty).

        Returns:
            True if valid.
        """
        if language not in self.allowed_versions:
            return False

        config = self.allowed_versions[language]

        # Check version
        if version not in config["versions"]:
            return False

        # Check variant
        if variant not in config["variants"]:
            return False

        return True

    def is_version_allowed(self, language: str, version: str) -> bool:
        """Check whether a version is in the allowlist."""
        if language not in self.allowed_versions:
            return False
        return version in self.allowed_versions[language]["versions"]

    def get_default_version(self, language: str) -> Tuple[str, str]:
        """
        Get default version.

        Args:
            language: Programming language.

        Returns:
            (version, variant)
        """
        if language not in self.allowed_versions:
            # For unsupported languages, fall back to Python defaults
            return "3.10", "slim"

        config = self.allowed_versions[language]
        return config["default_version"], config["default_variant"]

    def get_allowed_versions(self, language: str) -> Dict:
        """
        Get allowlisted versions.

        Args:
            language: Programming language.

        Returns:
            Dict containing versions and variants.
        """
        if language not in self.allowed_versions:
            return {"versions": [], "variants": []}

        config = self.allowed_versions[language]
        return {
            "versions": config["versions"],
            "variants": config["variants"],
            "base_image": config["base_image"],
        }

    def get_base_image_name(self, language: str) -> str:
        """Get base image name."""
        if language not in self.allowed_versions:
            return "python"  # Default
        return self.allowed_versions[language]["base_image"]

    def format_image_tag(self, language: str, version: str, variant: str) -> str:
        """
        Format a full image tag.

        Args:
            language: Programming language.
            version: Version.
            variant: Variant.

        Returns:
            Full image tag, e.g. "python:3.10-slim".
        """
        base_image = self.get_base_image_name(language)

        if variant:
            return f"{base_image}:{version}-{variant}"
        else:
            return f"{base_image}:{version}"

    def _extract_major_minor_version(self, version_string: str) -> str:
        """
        Extract a major/minor version from a version string.

        Args:
            version_string: Version string like "3.10.2" or ">=3.9,<4.0".

        Returns:
            Major/minor version, e.g. "3.10".
        """
        # Try matching X.Y
        match = re.search(r"(\d+\.\d+)", version_string)
        if match:
            return match.group(1)

        # Try matching a single major version (Rust, Node, etc.)
        match = re.search(r"(\d+)", version_string)
        if match:
            major = match.group(1)
            # For a single number we may need to add a minor version.
            # Keep it simple here; adjust per language if needed.
            return major

        return ""

    def _select_variant(self, language: str, project_analysis: Dict) -> str:
        """
        Select a variant based on project analysis.

        Args:
            language: Programming language.
            project_analysis: Project analysis dict.

        Returns:
            Variant name.
        """
        if language not in self.allowed_versions:
            return ""

        config = self.allowed_versions[language]
        variants = config["variants"]

        # If there's only one variant (often empty), use it
        if len(variants) == 1:
            return variants[0]

        # Prefer slim if available
        if "slim" in variants:
            return "slim"

        # Otherwise use the default variant
        return config["default_variant"]

    def get_supported_languages(self) -> list:
        """Return the list of supported languages."""
        return list(self.allowed_versions.keys())

    def generate_llm_prompt(self, language: str, project_analysis: Dict) -> str:
        """
        Generate a prompt asking the LLM to choose a version.

        Args:
            language: Programming language.
            project_analysis: Project analysis dict.

        Returns:
            Prompt string.
        """
        if language not in self.allowed_versions:
            return ""

        config = self.allowed_versions[language]
        versions_str = ", ".join(config["versions"])
        variants_str = ", ".join(
            [f'"{v}"' if v else '"standard"' for v in config["variants"]]
        )

        detected_version = project_analysis.get("version", "not detected")

        return f"""Based on the project analysis, choose the most appropriate {language} version from the allowlist.

## Project info
- Detected version requirement: {detected_version}
- Language: {language}

## Allowed versions
- Versions: {versions_str}
- Variants: {variants_str}

## Output format

Output strictly as JSON (no other text):

{{"version": "<selected version>", "variant": "<selected variant>"}}

Notes:
- version must be chosen from the allowed versions list
- variant can be "" (standard) or "slim", etc.
- If unsure, choose the default version: {config["default_version"]}, variant: {config["default_variant"] if config["default_variant"] else "standard"}
"""


# Convenience helper
def select_version_for_project(
    language: str, project_analysis: Dict, use_llm: bool = False, llm_response: str = ""
) -> Tuple[str, str]:
    """
    Convenience wrapper to select a Docker image version for a project.

    Args:
        language: Programming language.
        project_analysis: Project analysis dict.
        use_llm: Whether to use an LLM-provided selection.
        llm_response: LLM response (if use_llm=True).

    Returns:
        (version, variant)
    """
    selector = VersionSelector()

    if use_llm and llm_response:
        return selector.select_version_with_llm(language, llm_response)
    else:
        return selector.select_version_from_analysis(language, project_analysis)
