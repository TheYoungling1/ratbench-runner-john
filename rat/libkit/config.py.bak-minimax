"""Unified environment variable configuration.

Usage:
    from libkit.config import config

    # Get LLM config
    deepseek_key = config.DEEPSEEK_API_KEY
    github_token = config.GITHUB_TOKEN

Priority: system env vars > .env file > default hardcoded values
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Look for a .env file at the project root
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(
    dotenv_path=env_path, override=True
)  # override=False means system env vars win
_dotenv_loaded = True


class Config:
    """Config class - centrally manages all environment variables."""

    # ---------------------------
    # LLM API configuration
    # ---------------------------

    # DeepSeek API
    DEEPSEEK_API_KEY: str = os.getenv(
        "DEEPSEEK_API_KEY",
        "sk-<your_deepseek_api_key>",  # Development default
    )
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # GLM-4 (Zhipu AI) API
    GLM_API_KEY: str = os.getenv(
        "GLM_API_KEY",
        "<your_glm_api_key>",  # Development default
    )
    GLM_BASE_URL: str = os.getenv(
        "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"
    )

    # OpenAI API
    OPENAI_API_KEY: str = os.getenv(
        "OPENAI_API_KEY",
        "sk-<your_openai_api_key>",  # Development default
    )
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

    # Tongyi Qianwen (Alibaba Cloud) API
    TONGYI_API_KEY: str = os.getenv(
        "TONGYI_API_KEY",
        "sk-<your_tongyi_api_key>",  # Development default
    )
    TONGYI_BASE_URL: str = os.getenv(
        "TONGYI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    # DeepSeek (another config used by llm_analysis.py)
    DEEPSEEK_ANALYSIS_API_KEY: str = os.getenv(
        "DEEPSEEK_ANALYSIS_API_KEY",
        os.getenv(
            "DEEPSEEK_API_KEY", "sk-<your_deepseek_analysis_api_key>"
        ),  # Development default
    )

    # ---------------------------
    # GitHub API configuration
    # ---------------------------

    GITHUB_TOKEN: str = os.getenv(
        "GITHUB_TOKEN",
        "<your_github_token>",  # Development default
    )

    # ---------------------------
    # Weave/WandB configuration
    # ---------------------------

    WANDB_API_KEY: Optional[str] = os.getenv("WANDB_API_KEY", None)
    WEAVE_PROJECT: str = os.getenv("WEAVE_PROJECT", "rat")
    WEAVE_ENTITY: Optional[str] = os.getenv("WEAVE_ENTITY", None)
    WANDB_HTTP_TIMEOUT: int = int(os.getenv("WANDB_HTTP_TIMEOUT", "300"))
    WEAVE_PARALLELISM: int = int(os.getenv("WEAVE_PARALLELISM", "2"))
    WEAVE_USE_SERVER_CACHE: bool = (
        os.getenv("WEAVE_USE_SERVER_CACHE", "true").lower() == "true"
    )
    WEAVE_SERVER_CACHE_SIZE_LIMIT: int = int(
        os.getenv("WEAVE_SERVER_CACHE_SIZE_LIMIT", "1000000000")
    )
    WEAVE_SERVER_CACHE_DIR: Optional[str] = os.getenv("WEAVE_SERVER_CACHE_DIR", None)

    # ---------------------------
    # Docker configuration
    # ---------------------------

    DOCKER_TIMEOUT: int = int(os.getenv("DOCKER_TIMEOUT", "600"))

    # ---------------------------
    # Helpers
    # ---------------------------

    @classmethod
    def get_llm_config(cls, model_name: str) -> dict:
        """
        Get LLM configuration for a model name.

        Args:
            model_name: Model name, e.g. 'deepseek-chat', 'glm-4-flash', 'gpt', 'tongyi'
                        or any OpenRouter model slug containing '/' (e.g. 'alibaba/qwen-turbo')

        Returns:
            Dict containing 'key' and 'base_url'
        """
        # OpenRouter path: model slug contains "/" OR provider is forced via env
        if "/" in model_name or os.getenv("LLM_API_PROVIDER") == "openrouter":
            return {
                "base_url": os.getenv(
                    "OPENROUTER_API_BASE", "https://openrouter.ai/api/v1"
                ),
                "key": os.getenv("OPENROUTER_API_KEY"),
            }

        model_configs = {
            "deepseek-chat": {
                "base_url": cls.DEEPSEEK_BASE_URL,
                "key": cls.DEEPSEEK_API_KEY,
            },
            "glm-4-flash": {"base_url": cls.GLM_BASE_URL, "key": cls.GLM_API_KEY},
            "gpt": {"key": cls.OPENAI_API_KEY, "base_url": cls.OPENAI_BASE_URL},
            "tongyi": {"key": cls.TONGYI_API_KEY, "base_url": cls.TONGYI_BASE_URL},
            "deepseek": {"key": cls.DEEPSEEK_ANALYSIS_API_KEY},
        }

        if model_name not in model_configs:
            raise ValueError(
                f"Unknown model: {model_name}. Available models: {list(model_configs.keys())}"
            )

        return model_configs[model_name]

    @classmethod
    def validate(cls) -> bool:
        """
        Validate that required configuration is set.

        Returns:
            True if all required config is set
        """
        required_vars = [
            ("DEEPSEEK_API_KEY", cls.DEEPSEEK_API_KEY),
            ("GITHUB_TOKEN", cls.GITHUB_TOKEN),
        ]

        missing = []
        for var_name, var_value in required_vars:
            if not var_value or var_value.startswith("your_"):
                missing.append(var_name)

        if missing:
            print(
                f"Warning: The following required environment variables are not set: {', '.join(missing)}"
            )
            print("Please set them in .env file or as system environment variables.")
            return False

        return True

    @classmethod
    def print_config(cls, show_secrets: bool = False) -> None:
        """
        Print current configuration (for debugging).

        Args:
            show_secrets: Whether to show full secrets (default shows only first/last 4 chars)
        """

        def mask_secret(value: str) -> str:
            if not show_secrets and len(value) > 8:
                return f"{value[:4]}...{value[-4:]}"
            return value

        print("=" * 60)
        print("Current Configuration:")
        print("=" * 60)
        print(f"DEEPSEEK_API_KEY: {mask_secret(cls.DEEPSEEK_API_KEY)}")
        print(f"DEEPSEEK_BASE_URL: {cls.DEEPSEEK_BASE_URL}")
        print(f"GLM_API_KEY: {mask_secret(cls.GLM_API_KEY)}")
        print(f"GLM_BASE_URL: {cls.GLM_BASE_URL}")
        print(f"OPENAI_API_KEY: {mask_secret(cls.OPENAI_API_KEY)}")
        print(f"OPENAI_BASE_URL: {cls.OPENAI_BASE_URL}")
        print(f"TONGYI_API_KEY: {mask_secret(cls.TONGYI_API_KEY)}")
        print(f"TONGYI_BASE_URL: {cls.TONGYI_BASE_URL}")
        print(f"GITHUB_TOKEN: {mask_secret(cls.GITHUB_TOKEN)}")
        print(
            f"WANDB_API_KEY: {mask_secret(cls.WANDB_API_KEY) if cls.WANDB_API_KEY else 'Not set'}"
        )
        print(f"WEAVE_PROJECT: {cls.WEAVE_PROJECT}")
        print(f"WANDB_HTTP_TIMEOUT: {cls.WANDB_HTTP_TIMEOUT}")
        print(f"WEAVE_PARALLELISM: {cls.WEAVE_PARALLELISM}")
        print(f"WEAVE_USE_SERVER_CACHE: {cls.WEAVE_USE_SERVER_CACHE}")
        print(f"WEAVE_SERVER_CACHE_SIZE_LIMIT: {cls.WEAVE_SERVER_CACHE_SIZE_LIMIT}")
        print(
            f"WEAVE_SERVER_CACHE_DIR: {cls.WEAVE_SERVER_CACHE_DIR if cls.WEAVE_SERVER_CACHE_DIR else 'Not set'}"
        )
        print(f"DOCKER_TIMEOUT: {cls.DOCKER_TIMEOUT}")
        print(f".env file loaded: {_dotenv_loaded}")
        print("=" * 60)


# Create a global config instance
config = Config()
