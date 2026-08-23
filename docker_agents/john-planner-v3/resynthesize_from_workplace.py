#!/usr/bin/env python3
"""Re-run setup-log summary + recipe synthesis for an existing workplace."""

from __future__ import annotations

import argparse
import json

from src.constants import DEFAULT_LLM_MODEL
from src.workplace_replay import resynthesize_dockerfile_from_existing_workplace


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reuse an existing workplace and re-run Dockerfile synthesis only."
    )
    parser.add_argument("workplace", help="Path to an existing workplace directory.")
    parser.add_argument(
        "--model",
        default=DEFAULT_LLM_MODEL,
        help=f"LLM model to use for setup-log summary and recipe synthesis (default: {DEFAULT_LLM_MODEL})",
    )
    parser.add_argument(
        "--base-image",
        default=None,
        help="Optional explicit base image override. Defaults to image_selector_logs/summary.json or existing Dockerfile.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Optional explicit workdir override. Defaults to existing Dockerfile WORKDIR or /app.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = resynthesize_dockerfile_from_existing_workplace(
        workplace=args.workplace,
        model=args.model,
        base_image=args.base_image,
        workdir=args.workdir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("build_recipe_error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
