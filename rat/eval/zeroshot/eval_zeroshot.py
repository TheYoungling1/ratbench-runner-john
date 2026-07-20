#!/usr/bin/env python3
"""
ZeroShot evaluation script (W&B Weave)

Use direct LLM-based Dockerfile generation without iterative refinement.
"""

import argparse
import os
import sys

from eval.common.eval_runner import run_evaluation
from eval.models.zeroshot_model import ZeroShotModel


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Batch-evaluate repositories with ZeroShot + W&B Weave"
    )

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
        "--llm", type=str, default="deepseek-chat", help="LLM model to use"
    )

    parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=8000,
        help="Maximum tokens for repository context (default: 8000)",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Per-repo timeout in seconds (default: 600 = 10 minutes)",
    )

    parser.add_argument(
        "--weave-project",
        type=str,
        default="rat-zeroshot-evaluation",
        help="W&B Weave project name",
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
    """Main entrypoint."""
    args = parse_args()

    # Build model parameters
    model_kwargs = {
        "root_path": args.root_path
        if os.path.isabs(args.root_path)
        else os.path.abspath(args.root_path),
        "llm": args.llm,
        "max_context_tokens": args.max_context_tokens,
        "timeout": args.timeout,
    }

    # Print configuration
    print("\n" + "=" * 60)
    print("🚀 ZeroShot evaluation config")
    print("=" * 60)
    print(f"LLM model: {args.llm}")
    print(f"Max context tokens: {args.max_context_tokens}")
    print(f"Timeout: {args.timeout}s ({args.timeout // 60} minutes)")
    print(f"Dataset language: {args.language}")
    print(f"Weave project: {args.weave_project}")
    print(f"Use eval split: {'yes' if args.use_eval else 'no'}")
    print("=" * 60 + "\n")

    # Run evaluation
    return run_evaluation(
        args=args,
        model_class=ZeroShotModel,
        model_kwargs=model_kwargs,
        weave_project=args.weave_project,
        language=args.language,
        use_eval_dataset=args.use_eval,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n❌ Unhandled exception: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
