#!/usr/bin/env python3
"""Run verified Multi-Docker-Eval regression cases one by one and capture rich JSON logs."""

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.constants import DEFAULT_LLM_MODEL, DEFAULT_MEMORY_EMBEDDING_MODEL


def absolute_path_preserving_symlinks(path: Path) -> Path:
    """Make a path absolute without resolving venv launcher symlinks."""
    return Path(os.path.abspath(path))


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """
    Load a dataset file that may be stored as:
    - standard JSONL (`{...}\n{...}\n`)
    - JSONL-like objects with trailing commas (`{...},\n{...},\n`)
    - a JSON array (`[{...}, {...}]`)
    """
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    if text.startswith("["):
        payload = json.loads(text)
        if not isinstance(payload, list):
            raise ValueError(f"Expected a JSON array in {path}")
        return payload

    decoder = json.JSONDecoder()
    instances: List[Dict[str, Any]] = []
    index = 0
    text_length = len(text)

    while index < text_length:
        while index < text_length and text[index] in " \t\r\n,":
            index += 1
        if index >= text_length:
            break
        item, next_index = decoder.raw_decode(text, index)
        if not isinstance(item, dict):
            raise ValueError(f"Expected JSON object in {path} at offset {index}")
        instances.append(item)
        index = next_index

    return instances


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def write_single_instance_jsonl(path: Path, instance: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(instance, ensure_ascii=False))
        handle.write("\n")


def sanitize_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def run_command(
    command: List[str],
    cwd: Path,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    started_at = datetime.now().astimezone()
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
    )
    finished_at = datetime.now().astimezone()
    return {
        "command": command,
        "command_shell": shlex.join(command),
        "cwd": str(cwd),
        "returncode": completed.returncode,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 3),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def is_infrastructure_failure_text(text: str) -> bool:
    lowered = (text or "").lower()
    indicators = [
        "connection reset",
        "recv failure",
        "could not resolve",
        "temporary failure resolving",
        "failed to fetch",
        "502 bad gateway",
        "503 service unavailable",
        "tls handshake",
        "operation timed out",
        "network is unreachable",
        "early eof",
        "the remote end hung up unexpectedly",
    ]
    if any(indicator in lowered for indicator in indicators):
        return True
    return "returned a non-zero code: 100" in lowered and "apt-get update" in lowered


def build_eval_command(
    python_executable: Path,
    dataset_path: Path,
    docker_res_path: Path,
    run_id: str,
    output_path: Path,
    max_workers: Optional[int],
    stability_runs: Optional[int],
) -> List[str]:
    command = [
        str(python_executable),
        str(Path("Multi-Docker-Eval/evaluation/main.py")),
        f"base.dataset={dataset_path}",
        f"base.docker_res={docker_res_path}",
        f"base.run_id={run_id}",
        f"base.output_path={output_path}",
    ]
    if max_workers is not None:
        command.append(f"run_time.max_workers={max_workers}")
    if stability_runs is not None:
        command.append(f"test.stability_runs={stability_runs}")
    return command


def compute_status(
    adapter_instance_result: Optional[Dict[str, Any]],
    adapter_run: Dict[str, Any],
    evaluation_run: Optional[Dict[str, Any]],
    combined_report: Optional[Dict[str, Any]],
) -> str:
    adapter_logs = (adapter_instance_result or {}).get("logs", {})
    if adapter_logs.get("infrastructure_failure"):
        return "infrastructure_failure"
    if adapter_run["returncode"] != 0:
        if is_infrastructure_failure_text(
            f"{adapter_run.get('stdout') or ''}\n{adapter_run.get('stderr') or ''}"
        ):
            return "infrastructure_failure"
        return "adapter_command_failed"
    if not adapter_instance_result:
        return "adapter_result_missing"
    if adapter_logs.get("skip_evaluation"):
        return "adapter_skipped"
    if evaluation_run is None:
        return "evaluation_not_run"
    if evaluation_run.get("skipped"):
        return "adapter_not_evaluable"
    if evaluation_run["returncode"] != 0:
        if is_infrastructure_failure_text(
            f"{evaluation_run.get('stdout') or ''}\n{evaluation_run.get('stderr') or ''}"
        ):
            return "evaluation_infrastructure_failure"
        return "evaluation_command_failed"
    if not combined_report:
        return "evaluation_report_missing"
    if combined_report.get("resolved"):
        return "passed"
    return "failed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run verified Multi-Docker-Eval regression cases one by one with per-instance JSON logs."
    )
    parser.add_argument(
        "--dataset",
        default="verified.jsonl",
        help="Regression dataset JSONL path. Defaults to verified.jsonl.",
    )
    parser.add_argument(
        "--output-root",
        default="outputs/verified_regression",
        help="Directory where regression artifacts and per-instance JSON logs will be stored.",
    )
    parser.add_argument(
        "--python",
        default=".venv/bin/python",
        help="Python executable used for both adapter and evaluation commands.",
    )
    parser.add_argument(
        "--base-image",
        default="auto",
        help="Base image forwarded to multi_docker_eval_adapter.py.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_LLM_MODEL,
        help="Model forwarded to multi_docker_eval_adapter.py.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="Maximum agent steps per instance.",
    )
    parser.add_argument(
        "--enable-observation-compression",
        action="store_true",
        help="Enable AgentDiet-style observation compression during adapter runs.",
    )
    parser.add_argument(
        "--enable-long-term-memory",
        action="store_true",
        help="Enable failure-triggered long-term memory during adapter runs.",
    )
    parser.add_argument(
        "--memory-path",
        default=None,
        help="Path to the JSONL long-term memory store.",
    )
    parser.add_argument(
        "--memory-embedding-model",
        default=DEFAULT_MEMORY_EMBEDDING_MODEL,
        help=f"Embedding model for long-term memory (default: {DEFAULT_MEMORY_EMBEDDING_MODEL}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only run the first N instances from the dataset.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        help="Optional override for Multi-Docker-Eval run_time.max_workers.",
    )
    parser.add_argument(
        "--stability-runs",
        type=int,
        default=1,
        help="Multi-Docker-Eval test.stability_runs. Defaults to 1.",
    )
    parser.add_argument(
        "--run-id-prefix",
        default="VerifiedRegression",
        help="Prefix used to generate per-instance evaluation run_id values.",
    )
    parser.add_argument(
        "--disable-artifact-preflight",
        action="store_true",
        help="Forwarded to adapter: disable adapter-side artifact preflight and recipe repair.",
    )
    parser.add_argument(
        "--artifact-repair-rounds",
        type=int,
        default=1,
        help="Forwarded to adapter: maximum LLM recipe repair rounds after preflight failure.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parent
    dataset_path = (repo_root / args.dataset).resolve()
    output_root = (repo_root / args.output_root).resolve()
    python_arg = Path(args.python)
    if not python_arg.is_absolute():
        python_arg = repo_root / python_arg
    python_executable = absolute_path_preserving_symlinks(python_arg)

    if not dataset_path.exists():
        print(f"Dataset not found: {dataset_path}", file=sys.stderr)
        return 1
    if not python_executable.exists():
        print(f"Python executable not found: {python_executable}", file=sys.stderr)
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root = output_root / timestamp
    datasets_dir = run_root / "datasets"
    adapter_root = run_root / "adapter_output"
    eval_root = run_root / "eval_output"
    results_dir = run_root / "results"
    run_root.mkdir(parents=True, exist_ok=True)

    instances = load_jsonl(dataset_path)
    if args.limit is not None:
        instances = instances[: args.limit]

    summary: Dict[str, Any] = {
        "dataset": str(dataset_path),
        "python_executable": str(python_executable),
        "started_at": datetime.now().astimezone().isoformat(),
        "max_steps": args.max_steps,
        "model": args.model,
        "base_image": args.base_image,
        "enable_observation_compression": args.enable_observation_compression,
        "enable_long_term_memory": args.enable_long_term_memory,
        "memory_path": args.memory_path,
        "memory_embedding_model": args.memory_embedding_model,
        "artifact_preflight_enabled": not args.disable_artifact_preflight,
        "artifact_repair_rounds": args.artifact_repair_rounds,
        "run_root": str(run_root),
        "instance_count": len(instances),
        "instances": [],
    }

    base_eval_env = os.environ.copy()
    current_pythonpath = base_eval_env.get("PYTHONPATH")
    multi_docker_eval_path = str((repo_root / "Multi-Docker-Eval").resolve())
    base_eval_env["PYTHONPATH"] = (
        f"{multi_docker_eval_path}:{current_pythonpath}"
        if current_pythonpath
        else multi_docker_eval_path
    )

    print(f"Running regression dataset: {dataset_path}")
    print(f"Artifacts will be written to: {run_root}")

    for index, instance in enumerate(instances, start=1):
        instance_id = instance["instance_id"]
        print(f"\n{'#' * 70}")
        print(f"[{index}/{len(instances)}] {instance_id}")
        print(f"{'#' * 70}")

        safe_instance_id = sanitize_name(instance_id)
        dataset_file = datasets_dir / f"{safe_instance_id}.jsonl"
        adapter_output_dir = adapter_root / safe_instance_id
        result_file = results_dir / f"{safe_instance_id}.json"
        eval_run_id = f"{args.run_id_prefix}-{safe_instance_id}"

        write_single_instance_jsonl(dataset_file, instance)

        adapter_command = [
            str(python_executable),
            str((repo_root / "multi_docker_eval_adapter.py").resolve()),
            str(dataset_file),
            "--output-dir",
            str(adapter_output_dir),
            "--base-image",
            args.base_image,
            "--model",
            args.model,
            "--max-steps",
            str(args.max_steps),
        ]
        if args.enable_observation_compression:
            adapter_command.append("--enable-observation-compression")
        if args.enable_long_term_memory:
            adapter_command.append("--enable-long-term-memory")
        if args.memory_path:
            adapter_command.extend(["--memory-path", args.memory_path])
        if args.memory_embedding_model:
            adapter_command.extend(["--memory-embedding-model", args.memory_embedding_model])
        if args.disable_artifact_preflight:
            adapter_command.append("--disable-artifact-preflight")
        adapter_command.extend(["--artifact-repair-rounds", str(args.artifact_repair_rounds)])

        adapter_run = run_command(adapter_command, cwd=repo_root)
        adapter_instance_result = load_json(adapter_output_dir / f"{instance_id}.json")
        docker_res = load_json(adapter_output_dir / "docker_res.json")
        docker_res_entry = None
        if isinstance(docker_res, dict):
            docker_res_entry = docker_res.get(instance_id)

        evaluation_run: Optional[Dict[str, Any]] = None
        if docker_res_entry:
            eval_command = build_eval_command(
                python_executable=python_executable,
                dataset_path=dataset_file,
                docker_res_path=adapter_output_dir / "docker_res.json",
                run_id=eval_run_id,
                output_path=eval_root,
                max_workers=args.max_workers,
                stability_runs=args.stability_runs,
            )
            evaluation_run = run_command(eval_command, cwd=repo_root, env=base_eval_env)
        else:
            evaluation_run = {
                "command": None,
                "command_shell": None,
                "cwd": str(repo_root),
                "returncode": None,
                "started_at": None,
                "finished_at": None,
                "duration_seconds": 0,
                "stdout": "",
                "stderr": "",
                "skipped": True,
                "reason": "adapter_did_not_produce_evaluable_docker_res",
            }

        instance_eval_dir = eval_root / eval_run_id / instance_id
        combined_report = load_json(instance_eval_dir / "combined_report.json")
        final_report = load_json(eval_root / eval_run_id / "final_report.json")

        per_instance_payload: Dict[str, Any] = {
            "instance_id": instance_id,
            "dataset_entry": instance,
            "paths": {
                "single_instance_dataset": str(dataset_file),
                "adapter_output_dir": str(adapter_output_dir),
                "adapter_instance_result": str(adapter_output_dir / f"{instance_id}.json"),
                "docker_res": str(adapter_output_dir / "docker_res.json"),
                "evaluation_output_root": str(eval_root / eval_run_id),
                "evaluation_instance_dir": str(instance_eval_dir),
                "combined_report": str(instance_eval_dir / "combined_report.json"),
                "final_report": str(eval_root / eval_run_id / "final_report.json"),
            },
            "adapter": {
                "run": adapter_run,
                "instance_result": adapter_instance_result,
                "docker_res_entry": docker_res_entry,
            },
            "evaluation": {
                "run": evaluation_run,
                "combined_report": combined_report,
                "final_report": final_report,
            },
        }
        per_instance_payload["status"] = compute_status(
            adapter_instance_result=adapter_instance_result,
            adapter_run=adapter_run,
            evaluation_run=evaluation_run,
            combined_report=combined_report,
        )
        per_instance_payload["resolved"] = bool(combined_report and combined_report.get("resolved"))
        per_instance_payload["stable"] = bool(combined_report and combined_report.get("stable"))

        write_json(result_file, per_instance_payload)

        summary["instances"].append(
            {
                "instance_id": instance_id,
                "status": per_instance_payload["status"],
                "resolved": per_instance_payload["resolved"],
                "stable": per_instance_payload["stable"],
                "result_json": str(result_file),
            }
        )

    summary["finished_at"] = datetime.now().astimezone().isoformat()
    summary["status_counts"] = {}
    for item in summary["instances"]:
        status = item["status"]
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
    summary["resolved_count"] = sum(1 for item in summary["instances"] if item["resolved"])
    summary["stable_count"] = sum(1 for item in summary["instances"] if item["stable"])

    summary_path = run_root / "summary.json"
    write_json(summary_path, summary)

    print(f"\nSummary written to: {summary_path}")
    print(json.dumps(summary["status_counts"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
