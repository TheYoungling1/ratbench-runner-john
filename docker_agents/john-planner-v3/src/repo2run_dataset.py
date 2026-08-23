from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pypdf import PdfReader


REPO2RUN_TABLE15_PAGES = (34, 35, 36, 37)
REPO2RUN_BENCHMARK_SIZE = 420
REPO2RUN_PAPER_SUCCESS_RATE = 0.86
TABLE15_FIRST_RECORD = "271374667/VideoFusion"


def _clean_table15_page_text(text: str) -> str:
    cleaned = text or ""
    cleaned = re.sub(r"^H Repository building success status\n", "", cleaned)
    cleaned = re.sub(
        r"^As shown in Table 15, we list the success status of all the packages in our constructed benchmark\.\n",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"^Table 15: Success status of each package in the benchmark\.\n",
        "",
        cleaned,
    )
    cleaned = re.sub(r"^Table 15 . continued from previous page\n", "", cleaned)
    cleaned = re.sub(r"^full_name sha success full_name sha success\n", "", cleaned)
    cleaned = re.sub(r"Continued on next page\n\d+\n?$", "", cleaned.strip())
    cleaned = re.sub(r"\n\d+\n?$", "", cleaned.strip())
    return cleaned


def extract_repo2run_table15_entries(pdf_path: str | Path) -> list[dict[str, Any]]:
    reader = PdfReader(str(pdf_path))
    page_count = len(reader.pages)
    table_chunks: list[str] = []

    for page_number in REPO2RUN_TABLE15_PAGES:
        if page_number > page_count:
            raise ValueError(
                f"Repo2Run Table 15 page {page_number} not found in {pdf_path}; "
                f"PDF only has {page_count} pages."
            )
        raw_text = reader.pages[page_number - 1].extract_text() or ""
        table_chunks.append(_clean_table15_page_text(raw_text))

    token_stream = "\n".join(table_chunks).split()
    entries: list[dict[str, Any]] = []

    index = 0
    while index < len(token_stream) and TABLE15_FIRST_RECORD not in token_stream[index]:
        index += 1

    while index < len(token_stream):
        repo_name_parts: list[str] = []
        sha = None

        while index < len(token_stream):
            token = token_stream[index]

            if token == "full_name":
                index += 1
                continue

            if re.fullmatch(r"[0-9a-f]{6}", token):
                sha = token
                index += 1
                break

            fused_match = re.fullmatch(r"(.+?)([0-9a-f]{6})", token)
            if fused_match and repo_name_parts:
                repo_name_parts.append(fused_match.group(1))
                sha = fused_match.group(2)
                index += 1
                break

            repo_name_parts.append(token)
            index += 1

        if sha is None:
            break

        if index >= len(token_stream):
            raise ValueError(f"Missing success marker after {' '.join(repo_name_parts)} {sha}")

        status_token = token_stream[index]
        if status_token not in {"Yes", "No"}:
            raise ValueError(
                f"Unexpected success marker {status_token!r} after {' '.join(repo_name_parts)} {sha}"
            )
        index += 1

        full_name = "".join(repo_name_parts)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", full_name):
            raise ValueError(f"Invalid GitHub repository name parsed from Table 15: {full_name}")

        entries.append(
            {
                "instance_id": full_name.replace("/", "__"),
                "full_name": full_name,
                "repo_url": f"https://github.com/{full_name}.git",
                "sha": sha,
                "base_commit": sha,
                "paper_build_success": status_token == "Yes",
                "paper_build_success_label": status_token,
                "language": "python",
                "source": {
                    "paper": "Repo2Run: Automated Building Executable Environment for Code Repository at Scale",
                    "table": 15,
                    "pdf_path": str(Path(pdf_path)),
                },
            }
        )

    yes_count = sum(1 for entry in entries if entry["paper_build_success"])
    no_count = len(entries) - yes_count

    if len(entries) != REPO2RUN_BENCHMARK_SIZE:
        raise ValueError(
            f"Expected {REPO2RUN_BENCHMARK_SIZE} Table 15 entries, parsed {len(entries)}."
        )
    if yes_count != 361 or no_count != 59:
        raise ValueError(
            f"Unexpected Repo2Run success counts: yes={yes_count}, no={no_count}."
        )

    return entries


def build_repo2run_dataset_document(
    pdf_path: str | Path, entries: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "benchmark_name": "Repo2Run Table 15",
        "source_paper": "Repo2Run: Automated Building Executable Environment for Code Repository at Scale",
        "source_table": 15,
        "source_pdf": str(Path(pdf_path)),
        "paper_benchmark_size": REPO2RUN_BENCHMARK_SIZE,
        "paper_reported_success_rate": REPO2RUN_PAPER_SUCCESS_RATE,
        "instances": entries,
    }


def load_repo2run_dataset(dataset_path: str | Path) -> dict[str, Any]:
    path = Path(dataset_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return {
            "benchmark_name": path.stem,
            "instances": payload,
        }
    if not isinstance(payload, dict):
        raise ValueError(f"Unsupported dataset format in {path}")
    if "instances" not in payload or not isinstance(payload["instances"], list):
        raise ValueError(f"Expected an 'instances' list in {path}")
    return payload
