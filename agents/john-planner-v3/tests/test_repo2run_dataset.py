from pathlib import Path

from src.repo2run_dataset import extract_repo2run_table15_entries


def test_extract_repo2run_table15_entries_parses_expected_size_and_counts():
    entries = extract_repo2run_table15_entries(
        Path(__file__).resolve().parents[1] / "docs" / "repo2run.pdf"
    )

    assert len(entries) == 420
    assert sum(1 for entry in entries if entry["paper_build_success"]) == 361
    assert sum(1 for entry in entries if not entry["paper_build_success"]) == 59


def test_extract_repo2run_table15_entries_repairs_split_repository_names():
    entries = extract_repo2run_table15_entries(
        Path(__file__).resolve().parents[1] / "docs" / "repo2run.pdf"
    )
    by_name = {entry["full_name"]: entry for entry in entries}

    assert by_name["Benexl/FastAnime"]["sha"] == "677f46"
    assert by_name["Azure-Samples/rag-postgres-openai-python"]["sha"] == "61bde7"
    assert by_name["hngprojects/hng_boilerplate_python_fastapi_web"]["sha"] == "bc9740"
    assert by_name["Open-Wine-Components/umu-launcher"]["sha"] == "b0c0d4"
    assert by_name["TheAiSingularity/graphrag-local-ollama"]["sha"] == "bcb98d"
