# bench/docker_client.py
from __future__ import annotations

import subprocess


# Docker's output is NOT guaranteed to be valid UTF-8. BuildKit truncates long log lines, and a
# cut through a multi-byte character leaves an invalid continuation byte; pytest output carries
# whatever a repo's tests print. `text=True` alone decodes STRICTLY, so one bad byte raises
# UnicodeDecodeError out of subprocess.run -- which run_one catches and records as measure_error,
# fabricating a failure for a build that was proceeding fine. Observed on
# insistence/RuoYi-Vue3-FastAPI (2026-09-17), whose build log carries Chinese commit text.
# Decoding with errors="replace" keeps the log readable and can never fail. It is applied to every
# text-mode call, not just build(): exec() decodes the pytest run, where non-ASCII is routine, and
# a crash there would take down a measurement that had already paid for its build.
_TEXT = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


class SubprocessDocker:
    """DockerClient over the docker CLI (the shape measure() expects)."""

    def build(self, tag: str, ctx: str, timeout: int | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(["docker", "build", "-t", tag, ctx], **_TEXT, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            return 124, f"docker build timed out after {timeout}s"

    def image_size_mb(self, tag: str) -> float | None:
        p = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Size}}"], **_TEXT)
        try:
            return round(int(p.stdout.strip()) / (1024 * 1024), 1)
        except (ValueError, AttributeError):
            return None

    def image_digest(self, tag: str) -> str | None:
        """Resolved digest of `tag` (design item 4b), for the reproducibility record — never
        gates measurement. Prefers the pulled RepoDigest; falls back to the image ID for a
        locally-built image with no registry digest. Best-effort: any failure is None."""
        try:
            p = subprocess.run(
                ["docker", "inspect", "--format",
                 "{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}", tag],
                **_TEXT)
            out = (p.stdout or "").strip()
            return out or None
        except Exception:                                # noqa: BLE001 — provenance, never fatal
            return None

    def run_detached(self, tag: str, name: str, workdir: str, env: dict | None = None) -> None:
        # `env` becomes container-level environment, so every later `docker exec` inherits it --
        # the same as Graph2Env's evaluator passing its runtime handoff as `docker run --env`.
        env_args = [a for k, v in sorted((env or {}).items()) for a in ("-e", f"{k}={v}")]
        subprocess.run(f"docker rm -f {name} >/dev/null 2>&1", shell=True)
        subprocess.run(["docker", "run", "-d", "--name", name, "-w", workdir, *env_args, tag,
                        "tail", "-f", "/dev/null"], check=True, capture_output=True)

    def exec(self, name: str, argv: list, timeout: int | None = None) -> tuple[int, str, bool]:
        try:
            p = subprocess.run(["docker", "exec", name, *argv], **_TEXT, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr), False
        except subprocess.TimeoutExpired:
            return 124, "", True

    def rm(self, name: str, tag: str) -> None:
        subprocess.run(f"docker rm -f {name} >/dev/null 2>&1", shell=True)
        subprocess.run(f"docker rmi {tag} >/dev/null 2>&1", shell=True)
