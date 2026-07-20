"""
Dockerfile Templates Module
Manage language-specific Dockerfile templates.
"""

from .template_manager import TemplateManager
from .version_selector import VersionSelector

__all__ = ["TemplateManager", "VersionSelector"]
