"""
Language configurations package
"""

from libkit.language_configs.python_config import PythonConfig
from libkit.language_configs.java_config import JavaConfig
from libkit.language_configs.nodejs_config import NodeJSConfig
from libkit.language_configs.rust_config import RustConfig


def get_language_config(language: str, **kwargs):
    """
    Return a language configuration object based on the language name.

    Args:
        language: Language name; supports 'python', 'java', 'nodejs', 'rust' (case-insensitive)
        **kwargs: Extra params passed to config classes (e.g. JavaConfig: is_maven, is_gradle)

    Returns:
        LanguageConfig: The config object for the language

    Raises:
        ValueError: If the language is unsupported

    Examples:
        >>> config = get_language_config("python")
        >>> config = get_language_config("java", is_maven=True)
        >>> config = get_language_config("nodejs")
        >>> config = get_language_config("rust")
    """
    language_lower = language.lower().strip()

    if language_lower == "python":
        return PythonConfig(use_uv=kwargs.get("use_uv", False))
    elif language_lower == "java":
        return JavaConfig(
            is_maven=kwargs.get("is_maven", True),
            is_gradle=kwargs.get("is_gradle", False),
        )
    elif language_lower in [
        "nodejs",
        "node.js",
        "javascript",
        "typescript",
        "node",
        "js",
        "ts",
    ]:
        return NodeJSConfig()
    elif language_lower == "rust":
        return RustConfig()
    else:
        # Default to Python config (backwards compatibility)
        print(f"⚠️  Unknown language '{language}'; using default PythonConfig")
        return PythonConfig(use_uv=kwargs.get("use_uv", False))


__all__ = [
    "PythonConfig",
    "JavaConfig",
    "NodeJSConfig",
    "RustConfig",
    "get_language_config",
]
