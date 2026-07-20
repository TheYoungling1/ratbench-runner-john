#!/usr/bin/env python3
"""
Repo2Run evaluation script (W&B Weave).
"""

import argparse
import os
import sys

from eval.common.eval_runner import run_evaluation
from eval.models.repo2run_model import Repo2RunModel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repo2Run evaluation script")

    parser.add_argument(
        "--root-path", type=str, default=os.getcwd(), help="Project root path"
    )

    parser.add_argument(
        "--use-eval",
        action="store_true",
        help="Use the eval split ({language}_eval) instead of the full dataset ({language}_all)",
    )
    parser.add_argument("--limit", type=int, help="Limit the number of repositories")
    parser.add_argument(
        "--offset", type=int, default=0, help="Skip the first N repositories"
    )

    parser.add_argument(
        "--llm",
        type=str,
        default="deepseek-chat",
        help="LLM model name (default: deepseek-chat)",
    )
    parser.add_argument(
        "--num-turn",
        type=int,
        default=15,
        help="Max turns for Repo2Run Configuration Agent (default: 15)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=2400,
        help="Per-repo timeout in seconds (default: 2400s = 40min)",
    )

    parser.add_argument(
        "--weave-project",
        type=str,
        default="repo2run-evaluation",
        help="Weave project name",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="python",
        choices=["python", "java", "nodejs", "rust"],
        help="Repository language (used to select scorers)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Build model parameters
    model_kwargs = {
        "root_path": args.root_path
        if os.path.isabs(args.root_path)
        else os.path.abspath(args.root_path),
        "timeout": args.timeout,
        "llm": args.llm,
        "num_turn": args.num_turn,
    }

    # Print configuration
    print("\n" + "=" * 60)
    print("🚀 Repo2Run evaluation config")
    print("=" * 60)
    print(f"LLM model: {args.llm}")
    print(f"Max turns: {args.num_turn}")
    print(f"Timeout: {args.timeout}s ({args.timeout // 60} minutes)")
    print(f"Dataset language: {args.language}")
    print(f"Weave project: {args.weave_project}")
    print("=" * 60 + "\n")

    # Run evaluation
    return run_evaluation(
        args=args,
        model_class=Repo2RunModel,
        model_kwargs=model_kwargs,
        weave_project=args.weave_project,
        language=args.language,
        use_eval_dataset=args.use_eval,
    )


if __name__ == "__main__":
    sys.exit(main())
