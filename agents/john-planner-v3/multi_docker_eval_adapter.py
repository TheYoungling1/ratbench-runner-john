#!/usr/bin/env python3
"""multi_docker_eval_adapter.py — bridge the run_v3 graph-scheduler agent to the
RAT eval harness (``eval/models/dockeragent_model.py``).

Contract (see ``DockerAgentModel.predict``): the harness calls
``MultiDockerEvalAdapter(output_dir).process_single_instance(instance, ...)`` and
consumes ``result["dockerfile"]`` — a self-contained Dockerfile STRING that clones
the repo into ``/testbed`` and installs the environment — plus, optionally,
``result["setup_scripts"]`` (``{name: content}`` files the Dockerfile ``COPY``s).
The harness then builds the image, mounts RAT's pytest tools, runs them at
``/testbed`` and scores. This adapter does NOT run pytest itself.

The v3 agent's entrypoint is ``scripts/run_v3_e2e.py`` (``run_v3`` — the certified
graph-scheduler loop). This adapter runs it on a fresh local checkout to obtain a
certified install-only ``setup.sh`` + the base image ``run_v3`` selected, then bakes
both into a Dockerfile::

    FROM <base>
    RUN git clone <repo_url> /testbed
    WORKDIR /testbed
    COPY setup.sh ...            # the certified install-only script
    RUN bash setup.sh           # CWD=/testbed so `pip install -e .` resolves

This replaces the legacy DockerAgent adapter (which drove ``agent.DockerAgent`` +
``src.planner`` — both removed on the v3-core branch); it keeps the SAME
``process_single_instance`` entrypoint and the SAME result-dict contract, so
``dockeragent_model.py`` needs no change.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Tuple

# run_v3_e2e prints:  "[v3] base-image: <image> (py X) — <reason>". Capture <image>.
_BASE_IMAGE_RE = re.compile(r"\[v3\]\s*base-image:\s*(\S+)")
# run_v3 builds setup.sh for the Sandbox workdir /app; the eval image uses
# /testbed. setup.sh is install-only and path-relative in practice, but normalize
# any stray absolute /app defensively (mirrors the legacy adapter's behavior).
_APP_WORKDIR_RE = re.compile(r"(?<![\w/])/app(?![\w])")

# run_v3's entrypoint lives in THIS checkout (the agent root == this file's dir).
_AGENT_ROOT = Path(__file__).resolve().parent
_RUN_V3_E2E = _AGENT_ROOT / "scripts" / "run_v3_e2e.py"

# run_v3 may report giveup (exit 1) yet still have written a best-effort setup.sh;
# the benchmark scores whatever environment the agent produced, so a non-zero exit
# is NOT fatal here — only a missing artifact is.
_RUN_V3_TIMEOUT = int(os.environ.get("V3_ADAPTER_RUN_TIMEOUT", "5400"))   # 90 min
_CLONE_TIMEOUT = int(os.environ.get("V3_ADAPTER_CLONE_TIMEOUT", "1200"))


class MultiDockerEvalAdapter:
    """Bridge run_v3 to the RAT harness by emitting a Dockerfile from the certified
    setup.sh. Drop-in for the legacy DockerAgent adapter: same
    ``process_single_instance`` entrypoint, same ``dockerfile``/``setup_scripts``/
    ``base_image`` result contract."""

    def __init__(self, output_dir: str = "./multi_docker_eval_output"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def process_single_instance(
        self,
        instance: Dict[str, Any],
        base_image: str = "auto",
        model: str | None = None,
        max_steps: int = 30,
        enable_artifact_preflight: bool = False,
        **_ignored: Any,          # tolerate legacy kwargs the harness may still pass
    ) -> Dict[str, Any]:
        """Run run_v3 on a fresh checkout and return ``{instance_id: result}``.

        ``result`` carries ``dockerfile`` (str | None), ``setup_scripts``
        (``{"setup.sh": <content>}``), ``base_image`` (str | None) and ``logs``.
        Never raises — on any failure ``dockerfile`` stays None and ``logs["error"]``
        explains why (``dockeragent_model.py`` treats a None dockerfile as a clean
        ``no_dockerfile`` failure rather than a crash)."""
        instance_id = instance.get("instance_id", "unknown")
        repo_url = instance.get("repo_url") or ""
        result: Dict[str, Any] = {
            "dockerfile": None,
            "setup_scripts": {},
            "base_image": None,
            "logs": {},
        }
        try:
            src_dir = self._clone(repo_url)
            head_sha = self._head_sha(src_dir)
            setup_sh, resolved_base = self._run_v3(src_dir, base_image, model)
            setup_sh = _APP_WORKDIR_RE.sub("/testbed", setup_sh)
            result["base_image"] = resolved_base
            result["setup_scripts"] = {"setup.sh": setup_sh}
            result["dockerfile"] = self._render_dockerfile(resolved_base, repo_url)
            result["logs"] = {"head_sha": head_sha, "resolved_base": resolved_base}
        except subprocess.CalledProcessError as e:
            tail = (getattr(e, "stderr", "") or "")[-2000:]
            result["logs"] = {"error": f"{e} :: {tail}"}
        except Exception as e:  # never crash the harness — it reads result["dockerfile"]
            result["logs"] = {"error": repr(e)}
        return {instance_id: result}

    # ── steps (individually overridable/mockable) ─────────────────────────────
    def _clone(self, repo_url: str) -> Path:
        """Fresh shallow checkout for run_v3 to analyze. Idempotent per output_dir."""
        if not repo_url:
            raise ValueError("instance has no repo_url")
        dst = self.output_dir / "v3_src"
        if dst.exists():
            subprocess.run(["rm", "-rf", str(dst)], check=True, timeout=300)
        subprocess.run(
            ["git", "clone", "--depth=1", repo_url, str(dst)],
            check=True, capture_output=True, text=True, timeout=_CLONE_TIMEOUT,
        )
        return dst

    def _head_sha(self, src_dir: Path) -> str:
        out = subprocess.run(
            ["git", "-C", str(src_dir), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=60,
        )
        return out.stdout.strip()

    def _run_v3(self, src_dir: Path, base_image: str, model: str | None) -> Tuple[str, str]:
        """Run the run_v3 graph-scheduler loop on ``src_dir``; return
        ``(setup_sh_text, resolved_base_image)``.

        ``check=False``: run_v3 exits 1 on giveup/unresolved but still writes a
        best-effort setup.sh (run_v3_e2e.py:203) — the missing-artifact case is the
        only true failure, so we validate the file rather than the exit code."""
        setup_path = self.output_dir / "setup.sh"
        if setup_path.exists():
            setup_path.unlink()
        cmd = [sys.executable, str(_RUN_V3_E2E), str(src_dir),
               "--base-image", base_image or "auto",
               "--out", str(setup_path)]
        if model:
            cmd += ["--model", model]
        # First-pass-construction benchmark mode (V3_CONSTRUCTION_ONLY=1): render the
        # initial setup.sh from LLM-driven construction and skip the repair loop, so
        # the harness scores how well construction ALONE provisions the repo. The LLM
        # (base-image + service/config classify) stays on — only repair is skipped.
        if os.getenv("V3_CONSTRUCTION_ONLY") == "1":
            cmd.append("--construction-only")
        # Ensure `from src...` resolves when run_v3_e2e runs as a subprocess.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_AGENT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            cmd, cwd=str(_AGENT_ROOT), check=False, env=env,
            capture_output=True, text=True, timeout=_RUN_V3_TIMEOUT,
        )
        if not setup_path.exists():
            raise RuntimeError(
                "run_v3_e2e produced no setup.sh "
                f"(exit={proc.returncode}); stdout tail:\n{proc.stdout[-2000:]}\n"
                f"stderr tail:\n{proc.stderr[-2000:]}")
        setup_sh = setup_path.read_text()
        m = _BASE_IMAGE_RE.search(proc.stdout)
        resolved = m.group(1) if m else (
            base_image if base_image and base_image != "auto" else "python:3.11-slim")
        return setup_sh, resolved

    def _render_dockerfile(self, base_image: str, repo_url: str) -> str:
        """Self-contained Dockerfile: clone the repo into /testbed (same default
        HEAD run_v3 analyzed) and run the certified install-only setup.sh with
        CWD=/testbed so the trailing ``pip install -e .`` resolves."""
        return f"""FROM {base_image}
WORKDIR /testbed

# git for cloning (slim bases omit it); keep the layer lean.
RUN command -v git >/dev/null 2>&1 || (apt-get update \\
        && apt-get install -y --no-install-recommends git \\
        && rm -rf /var/lib/apt/lists/*)

# Fresh clone of the repo run_v3 analyzed.
RUN git clone --depth=1 {repo_url} /testbed

# The certified install-only script (system tier -> pinned pip closure ->
# editable install of the repo). CWD is /testbed so `pip install -e .` resolves.
COPY setup.sh /tmp/v3_setup.sh
RUN bash /tmp/v3_setup.sh
"""
