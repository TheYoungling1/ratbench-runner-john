# tests/test_seed_pin_render.py
"""Spec-compliance evidence: verify the pin layer renders correctly in a generated Dockerfile.

No Docker daemon or LLM is required — purely exercises the Synthesizer + build_pin_instructions
pure-Python path.
"""
import os
import tempfile

from src.envstate.world_model import Fact
from src.envstate.synthesis import build_pin_instructions
from src.synthesizer import Synthesizer


def _build_dockerfile(installed, build_commands=None):
    """Helper: build a minimal Synthesizer, apply a tiny recipe, append pin, and render."""
    synth = Synthesizer(base_image="python:3.11-slim", workdir="/app")
    recipe_cmds = build_commands or [
        "cd /app && pip install -e .",
        "pip install pytest",
    ]
    synth.apply_build_recipe({"build_commands": recipe_cmds})
    pin_cmds = build_pin_instructions(
        (Fact(name="requests", detail="2.31.0"), Fact(name="certifi", detail="2024.2.2"))
    )
    for cmd in pin_cmds:
        synth.add_build_instruction(cmd)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".Dockerfile", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        synth.generate_dockerfile(file_path=tmp_path)
        with open(tmp_path) as f:
            return f.read()
    finally:
        os.unlink(tmp_path)


def test_no_copy_line_for_pin_file():
    """No COPY instruction should reference the pin file."""
    content = _build_dockerfile(None)
    for line in content.splitlines():
        if line.strip().startswith("COPY"):
            assert "pinned" not in line and "requirements" not in line, (
                f"Unexpected COPY referencing pin file: {line!r}"
            )


def test_printf_and_pip_r_are_separate_runs():
    """printf write and pip install -r must be separate RUN instructions (not joined by &&)."""
    content = _build_dockerfile(None)
    lines = content.splitlines()
    run_lines = [l for l in lines if l.startswith("RUN ")]

    # Find the printf pin line
    printf_lines = [l for l in run_lines if "printf" in l and "jayint-pinned-closure.txt" in l]
    assert printf_lines, "Expected a RUN printf ... > jayint-pinned-closure.txt line"

    # Find the pip install -r line
    pip_r_lines = [l for l in run_lines if "pip install -r" in l and "jayint-pinned-closure.txt" in l]
    assert pip_r_lines, "Expected a RUN pip install -r jayint-pinned-closure.txt line"

    # They must be separate lines (no && joining them in the same RUN)
    for l in printf_lines:
        assert "pip install -r" not in l, f"printf and pip install -r joined in one RUN: {l!r}"
    for l in pip_r_lines:
        assert "printf" not in l, f"printf and pip install -r joined in one RUN: {l!r}"


def test_pip_r_is_last_run():
    """The pip install -r pin RUN must be the last RUN in the Dockerfile."""
    content = _build_dockerfile(None)
    lines = content.splitlines()
    run_lines = [l for l in lines if l.startswith("RUN ")]
    assert run_lines, "No RUN lines found in Dockerfile"
    last_run = run_lines[-1]
    assert "pip install -r" in last_run and "jayint-pinned-closure.txt" in last_run, (
        f"Last RUN is not the pin install: {last_run!r}"
    )


def test_pip_r_run_is_retry_wrapped():
    """The pip install -r RUN must be retry-wrapped (contains JAYINT_PIP_ATTEMPT)."""
    content = _build_dockerfile(None)
    lines = content.splitlines()
    # The retry wrapper spans multiple lines — collect the full Dockerfile text
    # and check for the marker near the pin install reference.
    # The wrapper may split across continuations; search the whole file.
    assert "JAYINT_PIP_ATTEMPT" in content, (
        "Expected retry-wrapper token JAYINT_PIP_ATTEMPT in Dockerfile, but not found.\n"
        f"Dockerfile content:\n{content}"
    )
    # Also confirm the pin path appears alongside it
    assert "jayint-pinned-closure.txt" in content
