#!/usr/bin/env python3
"""
Installamatic evaluation script (W&B Weave)

Use Installamatic's Gather and Repair agents to generate Dockerfiles and set up environments.
"""

import argparse
import os
import sys

from eval.common.eval_runner import run_evaluation
from eval.models.installamatic_model import InstallamaticModel


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Batch-evaluate repositories with Installamatic + W&B Weave"
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
        "--num-turn", type=int, default=3, help="Max Installamatic repair iterations"
    )

    parser.add_argument(
        "--llm", type=str, default="deepseek-chat", help="LLM model to use"
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Per-repo timeout in seconds (default: 1200 = 20 minutes)",
    )

    parser.add_argument(
        "--weave-project",
        type=str,
        default="rat-installamatic-evaluation",
        help="W&B Weave project name",
    )

    parser.add_argument(
        "--language",
        type=str,
        default="python",
        choices=["python"],
        help="Repository language (currently only Python is supported)",
    )

    return parser.parse_args()


def main() -> int:
    """Main entrypoint."""
    args = parse_args()

    # Validate Installamatic directory exists
    installamatic_root = os.path.abspath("./installamatic")
    if not os.path.exists(installamatic_root):
        print(f"❌ Installamatic directory not found: {installamatic_root}")
        print("   Make sure Installamatic is installed under the project root")
        return 1

    # Build model parameters
    model_kwargs = {
        "root_path": args.root_path
        if os.path.isabs(args.root_path)
        else os.path.abspath(args.root_path),
        "llm": args.llm,
        "num_turn": args.num_turn,
        "timeout": args.timeout,
        "n_tries": args.num_turn,  # Use num_turn as retry count
    }

    # Run evaluation
    return run_evaluation(
        args=args,
        model_class=InstallamaticModel,
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
