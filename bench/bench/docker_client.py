# bench/docker_client.py
from __future__ import annotations

import subprocess


class SubprocessDocker:
    """DockerClient over the docker CLI (the shape measure() expects)."""

    def build(self, tag: str, ctx: str, timeout: int | None = None) -> tuple[int, str]:
        try:
            p = subprocess.run(["docker", "build", "-t", tag, ctx],
                               capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            return 124, f"docker build timed out after {timeout}s"

    def image_size_mb(self, tag: str) -> float | None:
        p = subprocess.run(["docker", "image", "inspect", tag, "--format", "{{.Size}}"],
                           capture_output=True, text=True)
        try:
            return round(int(p.stdout.strip()) / (1024 * 1024), 1)
        except (ValueError, AttributeError):
            return None

    def run_detached(self, tag: str, name: str, workdir: str) -> None:
        subprocess.run(f"docker rm -f {name} >/dev/null 2>&1", shell=True)
        subprocess.run(["docker", "run", "-d", "--name", name, "-w", workdir, tag,
                        "tail", "-f", "/dev/null"], check=True, capture_output=True)

    def exec(self, name: str, argv: list, timeout: int | None = None) -> tuple[int, str, bool]:
        try:
            p = subprocess.run(["docker", "exec", name, *argv], capture_output=True, text=True,
                               timeout=timeout)
            return p.returncode, (p.stdout + p.stderr), False
        except subprocess.TimeoutExpired:
            return 124, "", True

    def rm(self, name: str, tag: str) -> None:
        subprocess.run(f"docker rm -f {name} >/dev/null 2>&1", shell=True)
        subprocess.run(f"docker rmi {tag} >/dev/null 2>&1", shell=True)
