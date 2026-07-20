#!/usr/bin/env python3
"""Common evaluation runner for all evaluation scripts."""

import asyncio
import os
import sys
from typing import Type
from datasets import load_dataset

import weave
from weave import Evaluation, Model

from libkit.command import stop_and_remove_container
from libkit.config import config

from .scorers import get_scorers_for_language


def run_evaluation(
    args,
    model_class: Type[Model],
    model_kwargs: dict,
    weave_project: str,
    language: str = "python",
    use_eval_dataset: bool = False,
) -> int:
    """
    Shared evaluation runner.

    Args:
        args: Parsed CLI args.
        model_class: Model class (e.g., RATModel, SWEAgentModel, PipreqsModel).
        model_kwargs: Model initialization kwargs.
        weave_project: W&B Weave project name.
        language: Repository language.
        use_eval_dataset: Whether to use the _eval split (default: False uses _all).

    Returns:
        Exit code: 0=success, 1=error, 130=interrupted
    """
    # Normalize root_path
    root_path = args.root_path
    if not os.path.isabs(root_path):
        root_path = os.path.abspath(root_path)

    # Set WANDB / WEAVE env vars
    if config.WANDB_API_KEY:
        os.environ["WANDB_API_KEY"] = config.WANDB_API_KEY
    os.environ["WANDB_HTTP_TIMEOUT"] = str(config.WANDB_HTTP_TIMEOUT)
    os.environ["WEAVE_PARALLELISM"] = str(config.WEAVE_PARALLELISM)
    if config.WEAVE_USE_SERVER_CACHE:
        os.environ["WEAVE_USE_SERVER_CACHE"] = "true"
    os.environ["WEAVE_SERVER_CACHE_SIZE_LIMIT"] = str(
        config.WEAVE_SERVER_CACHE_SIZE_LIMIT
    )
    if config.WEAVE_SERVER_CACHE_DIR:
        os.environ["WEAVE_SERVER_CACHE_DIR"] = config.WEAVE_SERVER_CACHE_DIR

    # Init Weave
    print(f"🔗 Initializing W&B Weave project: {weave_project}")
    weave.init(weave_project)

    # Load dataset
    dataset_suffix = "_eval" if use_eval_dataset else "_all"
    split_name = f"{language}{dataset_suffix}"

    print(f"📂 Loading repo list ({split_name})...")
    offset = max(0, args.offset)
    limit_str = args.limit or ""

    dataset = load_dataset(
        "<omitted>",
        split=f"{split_name}[{offset}:{limit_str}]",
        token=os.getenv("HF_TOKEN"),
    )
    weave_ds = weave.Dataset.from_hf(dataset)  # type: ignore
    weave.publish(weave_ds, split_name)
    dataset_ref = weave.ref(f"{split_name}:latest").get()

    if not dataset:
        print("⚠️  No repositories to process")
        return 0

    # Select scorers for the requested language
    scorers = get_scorers_for_language(language)

    # Print config
    print("\n" + "=" * 60)
    print("🚀 Starting batch evaluation")
    print("=" * 60)
    print(f"Dataset: <omitted>/RAT-bench ({split_name})")
    print(f"Root path: {root_path}")
    print(f"LLM model: {getattr(args, 'llm', 'N/A')}")
    print(f"Max turns: {getattr(args, 'num_turn', 'N/A')}")
    print(f"Timeout: {args.timeout}s ({args.timeout // 60} minutes)")
    print(f"To process: {len(dataset)}")  # type: ignore
    print(f"Dataset language: {language}")
    print(f"Use eval split: {'yes' if use_eval_dataset else 'no'}")
    print(f"Scorers: {[s.__name__ for s in scorers]}")
    print(f"Weave project: {weave_project}")
    print(f"\n--- Weave/WandB config ---")
    print(f"WANDB_HTTP_TIMEOUT: {config.WANDB_HTTP_TIMEOUT}s")
    print(f"WEAVE_PARALLELISM: {config.WEAVE_PARALLELISM}")
    print(f"WEAVE_USE_SERVER_CACHE: {config.WEAVE_USE_SERVER_CACHE}")
    print(
        f"WEAVE_SERVER_CACHE_SIZE_LIMIT: {config.WEAVE_SERVER_CACHE_SIZE_LIMIT} bytes"
    )
    if config.WEAVE_SERVER_CACHE_DIR:
        print(f"WEAVE_SERVER_CACHE_DIR: {config.WEAVE_SERVER_CACHE_DIR}")
    print("=" * 60 + "\n")

    # Create model
    model = model_class(**model_kwargs)

    # Create evaluation
    evaluation = Evaluation(
        dataset=dataset_ref,
        scorers=scorers,
    )

    # Run evaluation
    try:
        print("🎯 Running Weave evaluation...")
        result = asyncio.run(evaluation.evaluate(model))

        print("\n" + "=" * 60)
        print("✅ Evaluation completed")
        print("=" * 60)
        print("View details in the W&B Weave UI")
        print("=" * 60 + "\n")

        return 0

    except KeyboardInterrupt:
        print("\n\n⚠️  Evaluation interrupted by user")
        return 130

    except Exception as e:
        print(f"\n❌ Evaluation failed: {e}")
        import traceback

        traceback.print_exc()
        return 1

    finally:
        print("🧹 Cleaning up dangling containers...")
        stop_and_remove_container()
