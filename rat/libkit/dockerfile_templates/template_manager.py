"""
TemplateManager - manage Dockerfile templates.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional


class TemplateManager:
    """Manage Dockerfile templates."""

    # Map language -> template file
    LANGUAGE_TEMPLATE_MAP = {
        "python": "python.template",
        "rust": "rust.template",
        "javascript": "javascript.template",
        "node": "javascript.template",  # Node.js uses the JavaScript template
        "java": "java.template",
    }

    def __init__(self, template_dir: Optional[str] = None):
        """
        Initialize the template manager.

        Args:
            template_dir: Template directory (defaults to this module dir)
        """
        if template_dir is None:
            self.template_dir = Path(__file__).parent
        else:
            self.template_dir = Path(template_dir)

    def load_template(self, language: str) -> Optional[str]:
        """
        Load the template for a given language.

        Args:
            language: Programming language.

        Returns:
            Template content string, or None if missing.
        """
        language_lower = language.lower()
        template_file = self.LANGUAGE_TEMPLATE_MAP.get(language_lower)

        if not template_file:
            print(f"⚠️  Unsupported language: {language}")
            return None

        template_path = self.template_dir / template_file

        if not template_path.exists():
            print(f"⚠️  Template file not found: {template_path}")
            return None

        try:
            with open(template_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            print(f"❌ Failed to load template: {e}")
            return None

    def fill_template(
        self,
        template: str,
        base_image_with_version: str,
        timestamp: Optional[str] = None,
    ) -> str:
        """
        Fill template variables.

        Args:
            template: Template content.
            base_image_with_version: Full image tag, e.g. "python:3.10-slim".
            timestamp: Generation timestamp (optional).

        Returns:
            Filled Dockerfile content.
        """
        if timestamp is None:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        filled = template.replace("{BASE_IMAGE_WITH_VERSION}", base_image_with_version)
        filled = filled.replace("{TIMESTAMP}", timestamp)

        return filled

    def generate_dockerfile(
        self, language: str, base_image_with_version: str
    ) -> Optional[str]:
        """
        Generate the final Dockerfile in one step.

        Args:
            language: Programming language.
            base_image_with_version: Full image tag, e.g. "python:3.10-slim".

        Returns:
            Full Dockerfile content; None on failure.
        """
        # Load template
        template = self.load_template(language)
        if not template:
            return None

        # Fill template
        dockerfile = self.fill_template(template, base_image_with_version)

        return dockerfile

    def get_supported_languages(self) -> list:
        """Return supported languages."""
        return list(set(self.LANGUAGE_TEMPLATE_MAP.keys()))


# Convenience helper
def create_dockerfile(
    language: str, base_image_with_version: str, template_dir: Optional[str] = None
) -> Optional[str]:
    """
    Convenience helper to create a Dockerfile.

    Args:
        language: Programming language.
        base_image_with_version: Full image tag.
        template_dir: Template directory (optional).

    Returns:
        Generated Dockerfile content; None on failure.
    """
    manager = TemplateManager(template_dir)
    return manager.generate_dockerfile(language, base_image_with_version)
