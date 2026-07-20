#!/usr/bin/env python3
"""RAT evaluation script (W&B Weave) running the RAT model."""

import argparse
import os
import sys

from eval.common.eval_runner import run_evaluation
from eval.models.rat_model import RATModel


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Batch-evaluate repositories with W&B Weave (RAT Model)"
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

    parser.add_argument("--num-turn", type=int, default=30, help="Max CodeAgent turns")

    parser.add_argument(
        "--llm", type=str, default="deepseek-chat", help="LLM model to use"
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=2400,
        help="Per-repo timeout in seconds (default: 2400 = 40 minutes)",
    )

    parser.add_argument(
        "--save-mode",
        type=str,
        default="none",
        choices=["none", "dockerfile", "image"],
        help="Docker save mode",
    )

    parser.add_argument(
        "--weave-project",
        type=str,
        default="rat-evaluation",
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
        "num_turn": args.num_turn,
        "timeout": args.timeout,
        "save_mode": args.save_mode,
    }

    # Run evaluation
    return run_evaluation(
        args=args,
        model_class=RATModel,
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
