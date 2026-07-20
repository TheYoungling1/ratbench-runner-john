"""Tier-1 Fix 3: validate/repair Dockerfile so no RUN body holds a bare directive."""
import pytest

from src.envstate.dockerfile_validate import (
    MalformedDockerfileError,
    find_malformed_runs,
    validate_and_repair,
)


def test_clean_dockerfile_unchanged():
    df = "FROM python:3.11\nWORKDIR /app\nRUN pip install requests\nRUN echo done\n"
    assert find_malformed_runs(df) == []
    assert validate_and_repair(df) == df


def test_detects_run_inside_run_ampersand():
    df = "FROM x\nRUN echo a && RUN echo b\n"
    assert find_malformed_runs(df)


def test_detects_run_inside_run_semicolon():
    df = "FROM x\nRUN echo a; RUN echo b\n"
    assert find_malformed_runs(df)


def test_repairs_run_concatenation():
    df = "FROM x\nRUN echo a; RUN echo b\n"
    out = validate_and_repair(df)
    stripped = [l.strip() for l in out.splitlines()]
    assert "RUN echo a" in stripped
    assert "RUN echo b" in stripped
    assert find_malformed_runs(out) == []


def test_heredoc_body_with_run_word_not_flagged():
    df = (
        "# syntax=docker/dockerfile:1\n"
        "FROM x\n"
        "RUN cat > /app/note.txt <<'EOF'\n"
        "RUN this is just text inside a heredoc body\n"
        "FROM also-text\n"
        "EOF\n"
    )
    assert find_malformed_runs(df) == []
    assert validate_and_repair(df) == df


def test_quoted_run_word_not_flagged():
    df = "FROM x\nRUN echo 'please RUN this later'\n"
    assert find_malformed_runs(df) == []


def test_pip_and_pin_runs_not_flagged():
    df = (
        "FROM x\n"
        "RUN printf '%s\\n' 'requests==2.31.0' > /tmp/jayint-pinned-closure.txt\n"
        "RUN pip install -r /tmp/jayint-pinned-closure.txt\n"
        "RUN printf '%s' 'YmFzZTY0' | base64 -d > conf.py\n"
    )
    assert find_malformed_runs(df) == []


def test_from_inside_run_is_unrepairable_and_raises():
    df = "FROM x\nRUN echo a && FROM y\n"
    with pytest.raises(MalformedDockerfileError):
        validate_and_repair(df)
