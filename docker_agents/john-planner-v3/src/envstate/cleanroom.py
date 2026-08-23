from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Callable, List

# InImageRunner: callable(image_ref, command) -> (rc, stdout)
InImageRunner = Callable[[str, str], tuple]


@dataclass(frozen=True)
class CleanroomResult:
    passed: bool
    reason: str
    failed_probes: tuple = ()
    failed_tests: tuple = ()


def ensure_repo_in_dockerfile(dockerfile_text: str, workdir: str) -> str:
    """Insert `COPY . <workdir>` right after the WORKDIR line so a clean-room
    rebuild image contains the repo. Idempotent."""
    workdir = workdir or "/app"
    copy_line = f"COPY . {workdir}"
    if copy_line in (dockerfile_text or ""):
        return dockerfile_text
    lines = (dockerfile_text or "").splitlines()
    out = []
    inserted = False
    for line in lines:
        out.append(line)
        if not inserted and line.strip().startswith("WORKDIR "):
            out.append(copy_line)
            inserted = True
    if not inserted:
        out.append(copy_line)
    return "\n".join(out)


def verify_cleanroom(
    docker_client,
    dockerfile_text: str,
    build_context_dir: str,
    probe_commands: List[str],
    test_commands: List[str],
    run_command: InImageRunner,
) -> CleanroomResult:
    """Build a fresh image from the Dockerfile + repo context, then re-run
    probe_commands (bare shell strings) and test_commands.

    probe_commands replaces the old List[ProbeSpec] parameter; callers now
    pass pre-built command strings directly.  The ProbeSpec type is removed
    along with probes.py.
    """
    dockerfile_name = "Dockerfile.envstate-cleanroom"
    try:
        with open(
            os.path.join(build_context_dir, dockerfile_name), "w", encoding="utf-8"
        ) as handle:
            handle.write(dockerfile_text)
        image, _logs = docker_client.images.build(
            path=build_context_dir, dockerfile=dockerfile_name, rm=True
        )
    except Exception as exc:
        return CleanroomResult(False, f"clean-room build failed: {exc}")

    image_ref = (
        image if isinstance(image, str) else getattr(image, "id", str(image))
    )

    if not probe_commands and not test_commands:
        return CleanroomResult(
            False,
            "clean-room had nothing to verify (no probe_commands or test_commands)",
        )

    failed_probes: list[str] = []
    for cmd in probe_commands:
        rc, _out = run_command(image_ref, cmd)
        if rc != 0:
            failed_probes.append(cmd)
    if failed_probes:
        return CleanroomResult(
            False,
            "probe command(s) regressed in clean image",
            failed_probes=tuple(failed_probes),
        )

    failed_tests: list[str] = []
    for command in test_commands:
        rc, _out = run_command(image_ref, command)
        if rc != 0:
            failed_tests.append(command)
    if failed_tests:
        return CleanroomResult(
            False,
            "test command(s) failed in clean image",
            failed_tests=tuple(failed_tests),
        )

    return CleanroomResult(True, "clean-room verification passed")
