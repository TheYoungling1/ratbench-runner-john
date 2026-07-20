#!/usr/bin/env python3
"""
pipreqs evaluation script (W&B Weave)
"""

import argparse
import os
import sys

from eval.common.eval_runner import run_evaluation
from eval.models.pipreqs_model import PipreqsModel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pipreqs evaluation script")

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
    parser.add_argument("--timeout", type=int, default=900, help="Per-repo timeout")
    parser.add_argument(
        "--weave-project",
        type=str,
        default="pipreqs-evaluation",
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
    }

    # Run evaluation
    return run_evaluation(
        args=args,
        model_class=PipreqsModel,
        model_kwargs=model_kwargs,
        weave_project=args.weave_project,
        language=args.language,
        use_eval_dataset=args.use_eval,
    )


if __name__ == "__main__":
    sys.exit(main())
