#!/usr/bin/env python3
"""Build a standalone Repo2Run benchmark dataset from Table 15 in docs/repo2run.pdf."""

import argparse
import json
from pathlib import Path

from src.repo2run_dataset import (
    build_repo2run_dataset_document,
    extract_repo2run_table15_entries,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse Repo2Run Table 15 from the local PDF and emit a standalone benchmark JSON file."
    )
    parser.add_argument(
        "--pdf",
        default="docs/repo2run.pdf",
        help="Path to the Repo2Run paper PDF. Defaults to docs/repo2run.pdf.",
    )
    parser.add_argument(
        "--output",
        default="datasets/repo2run_table15.json",
        help="Output JSON path. Defaults to datasets/repo2run_table15.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pdf_path = Path(args.pdf).resolve()
    output_path = Path(args.output).resolve()

    entries = extract_repo2run_table15_entries(pdf_path)
    document = build_repo2run_dataset_document(pdf_path, entries)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    yes_count = sum(1 for entry in entries if entry["paper_build_success"])
    print(
        f"Wrote {len(entries)} Repo2Run instances to {output_path} "
        f"(paper Yes={yes_count}, No={len(entries) - yes_count})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
