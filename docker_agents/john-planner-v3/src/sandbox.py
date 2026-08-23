import io
import os
import re
import shlex
import tarfile
import docker
from dataclasses import dataclass
from src.synthesizer import Synthesizer

PIP_TRANSIENT_RETRY_ATTEMPTS = 3

_INSTALL_FAIL_MARKER = "__INSTALL_FAIL__"


@dataclass(frozen=True)
class InstallResult:
    rc: int
    failing_command: str | None   # $BASH_COMMAND captured by the ERR trap; None on success
    lineno: int | None
    stderr: str


def _wrap_with_err_trap(script: str) -> str:
    """Prepend an ERR trap so a `set -e` abort prints the failing command + line.

    render_build_script is reuse-only (its preamble already sets `set -Eeuo pipefail`);
    we add ONLY the trap line, immediately after the shebang/preamble is irrelevant —
    bash applies the most recent trap, and `-E` makes the trap inherit into functions.
    """
    trap = (
        "trap 'rc=$?; echo "
        f"\"{_INSTALL_FAIL_MARKER}:$BASH_COMMAND:$LINENO\" >&2; exit $rc' ERR\n"
    )
    return trap + script


def _parse_install_failure(output: str) -> tuple[str | None, int | None]:
    """Return (failing_command, lineno) from the FIRST install-fail marker, else (None, None)."""
    for line in (output or "").splitlines():
        if line.startswith(_INSTALL_FAIL_MARKER + ":"):
            rest = line[len(_INSTALL_FAIL_MARKER) + 1:]
            cmd, _, lineno_s = rest.rpartition(":")
            try:
                return (cmd or None), int(lineno_s)
            except ValueError:
                return (cmd or None), None
    return None, None


def _service_extra_hosts():
    if os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION") == "1":
        return {"postgres": "127.0.0.1"}
    return None


class Sandbox:
    def __init__(
        self,
        base_image="ubuntu:22.04",
        workdir="/app",
        volumes=None,
        platform=None,
        seed_dir=None,
        command_timeout_seconds=None,
        apt_mirror_url=None,
        apt_retries=5,
        apt_http_timeout_seconds=120,
        apt_https_timeout_seconds=120,
        docker_client_timeout_seconds=None,
        enable_cache_volume: bool = False,
    ):
        self.client = docker.from_env(timeout=docker_client_timeout_seconds)
        self.base_image = base_image
        self.workdir = workdir
        self.volumes = volumes  # Mapping of {local_path: {'bind': container_path, 'mode': 'rw'}}
        self.platform = platform  # Docker platform (e.g., "linux/amd64" for x86_64 emulation on ARM64)
        self.seed_dir = os.path.abspath(seed_dir) if seed_dir else None
        self.command_timeout_seconds = command_timeout_seconds
        self.docker_client_timeout_seconds = docker_client_timeout_seconds
        self.current_image = base_image
        self.container = None
        self.last_success_image = None  # 记录上一次成功状态的镜像
        self.snapshot_image_ids = set()
        self.runtime_replay_commands = []
        self.package_manager_broken_failure_streak = 0
        self._command_classifier = Synthesizer()
        self.apt_mirror_url = self._resolve_apt_mirror_url(apt_mirror_url)
        self.apt_retries = apt_retries
        self.apt_http_timeout_seconds = apt_http_timeout_seconds
        self.apt_https_timeout_seconds = apt_https_timeout_seconds
        if enable_cache_volume:
            cache = dict(self.volumes or {})
            cache.setdefault("jayint_pip_cache", {"bind": "/root/.cache/pip", "mode": "rw"})
            cache.setdefault("jayint_apt_cache", {"bind": "/var/cache/apt/archives", "mode": "rw"})
            self.volumes = cache
        self._setup_initial_container()

    def _setup_initial_container(self):
        """Initializes the container from the base image."""
        print(f"Initializing container from {self.current_image}...")
        if self.platform:
            print(f"[Platform] Using platform: {self.platform}")
            # Pull the image with the correct platform if different from cached version
            try:
                print(f"[Platform] Pulling {self.current_image} for platform {self.platform}...")
                self.client.images.pull(self.current_image, platform=self.platform)
            except Exception as e:
                print(f"[Platform] Pull failed (may already exist): {e}")
        _extra_hosts = _service_extra_hosts()
        self.container = self.client.containers.run(
            self.current_image,
            detach=True,
            tty=True,
            working_dir=self.workdir,
            command="/bin/bash",
            volumes=self.volumes,
            platform=self.platform,
            **({} if _extra_hosts is None else {"extra_hosts": _extra_hosts})
        )
        # Ensure workdir exists
        self.container.exec_run(f"mkdir -p {self.workdir}")
        self._bootstrap_apt_if_supported()
        if self.seed_dir:
            self._seed_workdir_from_host()
        # Always keep a baseline snapshot so the first failed command can roll back
        # to the initialized workspace rather than the raw base image.
        baseline_image = self.container.commit()
        self._register_snapshot(baseline_image.id)
        self.last_success_image = baseline_image.id
        print(f"[Baseline Snapshot] {self.last_success_image[:12]}")

    def _resolve_apt_mirror_url(self, apt_mirror_url):
        configured = (
            apt_mirror_url
            or os.environ.get("JAYINT_APT_MIRROR_URL")
            or os.environ.get("APT_MIRROR_URL")
        )
        if not configured:
            return None
        return configured.rstrip("/")

    def _bootstrap_apt_if_supported(self):
        if not self.container:
            return

        bootstrap_command = self._build_apt_bootstrap_command()
        exec_result = self.container.exec_run(
            ["/bin/bash", "-lc", bootstrap_command],
            workdir=self.workdir,
        )
        exit_code = exec_result.exit_code
        output = exec_result.output.decode("utf-8", errors="replace")
        if exit_code == 0:
            if self.apt_mirror_url:
                print(f"[Apt Bootstrap] Configured retries and mirror: {self.apt_mirror_url}")
            else:
                print("[Apt Bootstrap] Configured apt retries/timeouts.")
            return

        print(
            f"[Apt Bootstrap Warning] Failed to prepare apt mirror/retry settings (exit {exit_code})."
        )
        if output.strip():
            print(output)

    def _build_apt_bootstrap_command(self):
        mirror_block = ""
        if self.apt_mirror_url:
            mirror = shlex.quote(self.apt_mirror_url)
            mirror_block = (
                f"APT_MIRROR_URL={mirror}\n"
                "for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/ubuntu.sources; do\n"
                "  [ -f \"$file\" ] || continue\n"
                "  sed -i \"s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR_URL}|g\" \"$file\"\n"
                "  sed -i \"s|https://archive.ubuntu.com/ubuntu|${APT_MIRROR_URL}|g\" \"$file\"\n"
                "  sed -i \"s|http://security.ubuntu.com/ubuntu|${APT_MIRROR_URL}|g\" \"$file\"\n"
                "  sed -i \"s|https://security.ubuntu.com/ubuntu|${APT_MIRROR_URL}|g\" \"$file\"\n"
                "done\n"
                "apt-get update\n"
            )

        return (
            "set -e\n"
            "if ! command -v apt-get >/dev/null 2>&1; then\n"
            "  exit 0\n"
            "fi\n"
            "mkdir -p /etc/apt/apt.conf.d\n"
            "cat >/etc/apt/apt.conf.d/99jayint-retries <<'EOF'\n"
            f"Acquire::Retries \"{self.apt_retries}\";\n"
            f"Acquire::http::Timeout \"{self.apt_http_timeout_seconds}\";\n"
            f"Acquire::https::Timeout \"{self.apt_https_timeout_seconds}\";\n"
            "Acquire::http::Pipeline-Depth \"0\";\n"
            "EOF\n"
            f"{mirror_block}"
        )

    def _seed_workdir_from_host(self):
        """Copy the host workspace into the container so rollback includes repo state."""
        if not os.path.isdir(self.seed_dir):
            raise ValueError(f"seed_dir does not exist or is not a directory: {self.seed_dir}")

        self.container.exec_run(
            ["/bin/bash", "-lc", f"rm -rf {self.workdir}/* {self.workdir}/.[!.]* {self.workdir}/..?* 2>/dev/null || true"]
        )

        archive_stream = io.BytesIO()
        with tarfile.open(fileobj=archive_stream, mode="w") as tar:
            for entry in sorted(os.listdir(self.seed_dir)):
                entry_path = os.path.join(self.seed_dir, entry)
                tar.add(entry_path, arcname=entry, recursive=True)
        archive_stream.seek(0)

        if not self.container.put_archive(self.workdir, archive_stream.getvalue()):
            raise RuntimeError(f"Failed to copy workspace from {self.seed_dir} into container")

    def exec_readonly(self, command):
        """Run a read-only probe/extractor command with NO side effects.

        Returns (exit_code:int, output:str). Does not commit snapshots, does not
        run preflight rejection, does not retry, does not inject SYSTEM prefixes,
        and deliberately does NOT apply `set -o pipefail` (pipefail can flip the
        exit code of legitimate probe chains like `cmd | grep -q`, causing silent
        mis-certification). It wraps the command in a login shell so `&&` and `|`
        work, and returns the raw exit code untouched.
        Callers must only pass commands that do not mutate the environment.
        """
        result = self.container.exec_run(["/bin/sh", "-lc", command], workdir=self.workdir)
        output = result.output
        if isinstance(output, (bytes, bytearray)):
            output = output.decode("utf-8", errors="replace")
        exit_code = result.exit_code
        if exit_code is None:
            # Docker exec_run can yield None (streaming/detached); a probe that did not
            # produce a real exit code must never be read as success (rc==0).
            exit_code = -1
        return exit_code, output or ""

    def read_file_bytes(self, path, max_bytes):
        """Read a file's RAW bytes from the live container, capped at ``max_bytes``.

        Used by Tier-1 file capture (Fix 1) to capture the achieved file state
        losslessly. Deliberately uses a NON-login shell-free exec (``head -c`` via the
        argv form, no ``/bin/sh -lc``) so profile / MOTD / conda-init banners cannot
        pollute the bytes. Reads ``max_bytes + 1`` so the caller can detect oversize and
        fall back to replaying the edit instead of truncating. Returns ``(rc, bytes)``;
        rc != 0 means the file is unreadable (missing / permission) and the caller must
        replay the edit rather than drop it.
        """
        limit = int(max_bytes) + 1
        result = self.container.exec_run(
            ["head", "-c", str(limit), "--", path], workdir=self.workdir
        )
        output = result.output
        if output is None:
            output = b""
        elif not isinstance(output, (bytes, bytearray)):
            output = str(output).encode("utf-8", "surrogateescape")
        exit_code = result.exit_code
        if exit_code is None:
            exit_code = -1
        return exit_code, bytes(output)

    def execute(self, command):
        """
        Executes a bash command.
        Returns (success, output).
        """
        print(f"[Container ID: {self.container.short_id}]")
        print(f"Executing: {command}")

        preflight_rejection_prefix = self._get_preflight_rejection_prefix(command)
        if preflight_rejection_prefix:
            print("Command rejected before execution by sandbox preflight.")
            return False, preflight_rejection_prefix

        wrapped_command = ["/bin/bash", "-c", self._wrap_command_with_timeout(command)]
        try:
            exec_result = self.container.exec_run(
                wrapped_command,
                workdir=self.workdir
            )
        except docker.errors.DockerException as exc:
            print(f"[System] Command execution failed because the container is unusable: {exc}")
            recovery_message = self._restore_last_success_container(
                reason=f"container_exec_error: {exc}"
            )
            return False, (
                f"[SYSTEM] Command execution failed because the container became unusable: {exc}\n"
                f"{recovery_message}"
            )

        exit_code = exec_result.exit_code
        output = exec_result.output.decode('utf-8', errors='replace')
        retry_outputs = []
        attempt_index = 1
        while self._should_retry_transient_pip_failure(command, exit_code, output, attempt_index):
            retry_outputs.append(
                f"[SYSTEM] Transient pip install failure on attempt {attempt_index}; "
                f"retrying the same command.\n{output}"
            )
            attempt_index += 1
            print(
                f"[Sandbox Retry] Transient pip install failure; "
                f"retrying attempt {attempt_index}/{PIP_TRANSIENT_RETRY_ATTEMPTS}."
            )
            try:
                exec_result = self.container.exec_run(
                    wrapped_command,
                    workdir=self.workdir,
                )
            except docker.errors.DockerException as exc:
                print(f"[System] Command retry failed because the container is unusable: {exc}")
                recovery_message = self._restore_last_success_container(
                    reason=f"container_exec_retry_error: {exc}"
                )
                return False, (
                    f"[SYSTEM] Command retry failed because the container became unusable: {exc}\n"
                    f"{recovery_message}"
                )
            exit_code = exec_result.exit_code
            output = exec_result.output.decode('utf-8', errors='replace')

        if retry_outputs:
            output = "\n\n".join([*retry_outputs, output])

        if self._is_timeout_exit(exit_code):
            output = (
                f"[SYSTEM] Command timed out after {self.command_timeout_seconds} seconds.\n\n"
                f"{output}"
            )
        
        # 判断是否为"信息性退出"（非真正错误）
        is_informational_exit = self._is_informational_exit(exit_code, output)
        
        # 检测输出中是否有测试失败信号（用于 Observation 前缀注入）
        test_fail_prefix = self._get_test_failure_prefix(exit_code, output)
        truncated_test_prefix = self._get_truncated_test_output_prefix(command)
        
        if exit_code == 0 or is_informational_exit:
            # Success: 保存当前成功状态
            if is_informational_exit:
                print(f"Command exited with code {exit_code} (informational, not an error).")
            else:
                print("Command succeeded.")

            if truncated_test_prefix:
                output = truncated_test_prefix + output

            self.package_manager_broken_failure_streak = 0
            self._track_runtime_command(command)
            return True, output
        else:
            print(f"Command failed (exit {exit_code}). Preserving current state for agent decision.")

            failed_mutation_prefix = self._get_failed_mutating_command_prefix(
                command, exit_code
            )
            rollback_candidate_prefix = self._get_package_manager_rollback_prefix(
                command, exit_code, output
            )
            if truncated_test_prefix:
                output = truncated_test_prefix + output
            if test_fail_prefix:
                output = test_fail_prefix + output
            if failed_mutation_prefix:
                output = failed_mutation_prefix + output
            if rollback_candidate_prefix:
                output = rollback_candidate_prefix + output

            if not self._container_is_healthy():
                print("[System] Container became unhealthy after the failed command. Restoring last successful snapshot.")
                recovery_message = self._restore_last_success_container(
                    reason=f"container_unhealthy_after_exit_{exit_code}"
                )
                if output and not output.endswith("\n"):
                    output += "\n"
                output += (
                    "\n[SYSTEM] The container became unhealthy after the failed command, "
                    "so the system restored the last successful snapshot automatically.\n"
                    f"{recovery_message}"
                )

            return False, output

    def rollback(self, reason="agent_requested"):
        """Restore the container to the last successful snapshot on explicit agent request."""
        print(f"[System] Explicit rollback requested ({reason}).")
        try:
            message = self._restore_last_success_container(reason=reason)
        except docker.errors.DockerException as exc:
            return False, f"[SYSTEM] Rollback failed: {exc}"
        return True, message

    def reset_to_base(self) -> None:
        """Recreate the container fresh from base_image (NOT last_success_image).

        Distinct from rollback()/_restore_last_success_container, which restore the
        last good snapshot. Used by the Stage-2 binding-install gate so every install
        attempt runs from clean. Does NOT replay runtime services (install-only path).
        """
        if self.container is not None:
            try:
                self.container.stop(timeout=0)
            except docker.errors.DockerException:
                pass
            try:
                self.container.remove()
            except docker.errors.DockerException:
                pass
        _extra_hosts = _service_extra_hosts()
        self.container = self.client.containers.run(
            self.base_image, detach=True, tty=True, working_dir=self.workdir,
            command="/bin/bash", volumes=self.volumes, platform=self.platform,
            **({} if _extra_hosts is None else {"extra_hosts": _extra_hosts}),
        )
        self.container.exec_run(f"mkdir -p {self.workdir}")
        self._bootstrap_apt_if_supported()
        if self.seed_dir:
            self._seed_workdir_from_host()
        self.current_image = self.base_image

    def run_install_script(self, script: str) -> InstallResult:
        """Run an install-only setup.sh in the CURRENT container, bypassing execute()'s
        preflight (which rejects multi-step scripts). Returns InstallResult with the
        ERR-trap-localized failing command on rc!=0.

        Invariant: this does NOT commit a snapshot; the Stage-2 gate always calls
        reset_to_base() before this, so last_success_image is never relied upon here.
        """
        wrapped = _wrap_with_err_trap(script)
        result = self.container.exec_run(["/bin/bash", "-c", wrapped], workdir=self.workdir)
        output = result.output
        if isinstance(output, (bytes, bytearray)):
            output = output.decode("utf-8", errors="replace")
        rc = result.exit_code if result.exit_code is not None else -1
        failing_command, lineno = _parse_install_failure(output or "")
        return InstallResult(rc=rc, failing_command=failing_command, lineno=lineno,
                             stderr=output or "")

    def _register_snapshot(self, image_id):
        if image_id:
            self.snapshot_image_ids.add(image_id)

    def _remove_snapshot_image(self, image_id):
        if not image_id:
            return
        try:
            image = self.client.images.get(image_id)
            self.client.images.remove(image.id, force=True)
        except (docker.errors.ImageNotFound, docker.errors.APIError):
            return
        finally:
            self.snapshot_image_ids.discard(image_id)

    def _restore_last_success_container(self, reason="rollback"):
        rollback_image = self.last_success_image if self.last_success_image else self.base_image

        if self.container:
            try:
                self.container.stop(timeout=0)
            except docker.errors.DockerException:
                pass
            try:
                self.container.remove()
            except docker.errors.DockerException:
                pass

        _extra_hosts = _service_extra_hosts()
        self.container = self.client.containers.run(
            rollback_image,
            detach=True,
            tty=True,
            working_dir=self.workdir,
            command="/bin/bash",
            volumes=self.volumes,
            platform=self.platform,
            **({} if _extra_hosts is None else {"extra_hosts": _extra_hosts})
        )
        self.container.exec_run(f"mkdir -p {self.workdir}")
        self._replay_runtime_commands()
        return (
            "[SYSTEM] Restored the container to the last successful snapshot "
            f"because of {reason}.\n"
            "[SYSTEM] Ephemeral runtime services were replayed when possible."
        )

    def _wrap_command_with_timeout(self, command):
        """Enforce a per-command timeout when GNU `timeout` is available in the container."""
        pipefail_command = self._wrap_command_with_pipefail(command)
        if not self.command_timeout_seconds:
            return pipefail_command

        timeout_seconds = int(self.command_timeout_seconds)
        return (
            "if command -v timeout >/dev/null 2>&1; then "
            f"timeout --foreground --kill-after=30s {timeout_seconds}s {pipefail_command}; "
            "else "
            f"{pipefail_command}; "
            "fi"
        )

    def _wrap_command_with_pipefail(self, command):
        quoted_command = shlex.quote(command)
        return f"/bin/bash -o pipefail -lc {quoted_command}"

    def _is_timeout_exit(self, exit_code):
        if not self.command_timeout_seconds:
            return False
        return exit_code in {124, 137}

    def _should_retry_transient_pip_failure(self, command, exit_code, output, attempt_index):
        if attempt_index >= PIP_TRANSIENT_RETRY_ATTEMPTS:
            return False
        if exit_code == 0:
            return False
        if not self._looks_like_pip_install_command(command):
            return False

        normalized_output = (output or "").lower()
        transient_markers = (
            "readtimeouterror",
            "connection reset",
            "connection aborted",
            "connection broken",
            "remote disconnected",
            "temporary failure in name resolution",
            "temporarily unavailable",
            "timed out",
            "timeout",
            "sslerror",
            "max retries exceeded",
            "too many 502 error responses",
            "too many 503 error responses",
            "too many 504 error responses",
            "network is unreachable",
        )
        return any(marker in normalized_output for marker in transient_markers)

    def _looks_like_pip_install_command(self, command):
        for raw_segment, _ in self._command_classifier._split_shell_chain(command or ""):
            normalized = self._command_classifier._normalize_command_segment(raw_segment)
            if self._command_classifier._is_navigation_only_segment(normalized):
                continue
            if re.match(r"^(?:pip|pip2|pip3)\s+install\b", normalized):
                return True
            if re.match(r"^(?:python|python2|python3)\s+-m\s+pip\s+install\b", normalized):
                return True
            if re.match(r"^uv\s+pip\s+install\b", normalized):
                return True
        return False

    def _container_is_healthy(self):
        if not self.container:
            return False

        reload_fn = getattr(self.container, "reload", None)
        if callable(reload_fn):
            try:
                reload_fn()
            except docker.errors.DockerException:
                return False

        status = getattr(self.container, "status", None)
        if status is None:
            return True
        return status == "running"
    
    def _track_runtime_command(self, command):
        """Remember pure runtime service commands so they can be replayed after rollback."""
        normalized_command = (command or "").strip()
        if not normalized_command:
            return

        if not self._command_classifier.is_runtime_service_command(normalized_command):
            return

        runtime_key = self._runtime_service_key(normalized_command)
        runtime_action = self._runtime_service_action(normalized_command)

        if runtime_key and runtime_action == "stop":
            self.runtime_replay_commands = [
                entry for entry in self.runtime_replay_commands if entry["key"] != runtime_key
            ]
            return

        if self._command_classifier.is_persistent_setup_command(normalized_command):
            return

        if runtime_key:
            self.runtime_replay_commands = [
                entry for entry in self.runtime_replay_commands if entry["key"] != runtime_key
            ]

        self.runtime_replay_commands.append(
            {
                "key": runtime_key or normalized_command,
                "command": normalized_command,
            }
        )

    def _replay_runtime_commands(self):
        """Restore ephemeral runtime services after container rollback."""
        if not self.runtime_replay_commands:
            return

        print(
            f"[Runtime Replay] Re-running {len(self.runtime_replay_commands)} runtime command(s) after rollback."
        )
        restored_commands = []
        for entry in self.runtime_replay_commands:
            command = entry["command"]
            exec_result = self.container.exec_run(
                ["/bin/bash", "-c", self._wrap_command_with_timeout(command)],
                workdir=self.workdir,
            )
            exit_code = exec_result.exit_code
            output = exec_result.output.decode("utf-8", errors="replace")
            if exit_code == 0 or self._is_informational_exit(exit_code, output):
                print(f"[Runtime Replay] Restored: {command}")
                restored_commands.append(entry)
                continue

            print(
                f"[Runtime Replay Warning] Failed to restore runtime command (exit {exit_code}): {command}"
            )

        self.runtime_replay_commands = restored_commands

    def _runtime_service_key(self, command):
        normalized = command.strip().lower()
        service_match = re.match(r"^service\s+(\S+)\s+(?:start|restart|reload|stop)\b", normalized)
        if service_match:
            return service_match.group(1)

        executable = normalized.split()[0] if normalized else ""
        executable = executable.strip("\"'`")
        if executable:
            return executable.rsplit("/", 1)[-1]
        return None

    def _runtime_service_action(self, command):
        normalized = command.strip().lower()
        service_match = re.match(r"^service\s+\S+\s+(start|restart|reload|stop)\b", normalized)
        if service_match:
            return service_match.group(1)
        if " --daemonize " in f" {normalized} " or " -detached" in normalized or " --fork" in normalized:
            return "start"
        return "start"
    
    def _is_informational_exit(self, exit_code, output):
        """
        判断是否为信息性退出（如显示帮助信息），而非真正的错误。
        测试命令的失败（如测试未通过）不应被视为信息性退出。
        """
        # Exit code 1-2 通常是参数错误或显示帮助
        if exit_code not in [1, 2]:
            return False
        
        # 检查输出中是否包含帮助信息的特征
        help_indicators = [
            'Usage:',
            'usage:',
            '--help',
            'Options:',
            'Commands:',
            'positional arguments:',
            'optional arguments:'
        ]
        
        # 测试失败的特征（不应被误判为信息性退出）
        test_failure_indicators = [
            'failures:',
            'errors:',
            'FAILED',
            'Failed:',        # run_all / TAP 格式：Failed: 3
            'not ok',         # TAP 协议失败行
            'Test failed',
            'assertion failed',
            'expected',
            'actual',
            'diff:',
            'Traceback (most recent call last):',
            'NameError',
            'ImportError',
            'ModuleNotFoundError',
            'LoadError',
            'Gem::LoadError',
            'bundler: command not found'
        ]
        
        output_lower = output.lower()
        
        # 如果包含测试失败特征，则不是信息性退出
        if any(indicator.lower() in output_lower for indicator in test_failure_indicators):
            return False
        
        return any(indicator.lower() in output_lower for indicator in help_indicators)

    def _get_test_failure_prefix(self, exit_code, output):
        """
        检测命令输出是否包含测试失败信号。
        若是，返回注入到 Observation 头部的强制警告；否则返回空字符串。
        目的：阻止 LLM 以"核心功能通过"为由自我合理化，绕过 No Excuses Rule。
        """
        if exit_code == 0:
            return ""

        # TAP 格式失败：run_all 输出的 "Failed: N"
        tap_fail = re.search(r'Failed:\s+([1-9]\d*)', output)
        if tap_fail:
            failed_count = tap_fail.group(1)
            return (
                f"[SYSTEM] ⚠️  TEST FAILURE DETECTED: {failed_count} test(s) FAILED.\n"
                f"[SYSTEM] Per the No Excuses Rule, you CANNOT output 'Final Answer: Success' "
                f"from this failed command. Repo2Run success requires a real "
                f"`pytest --collect-only -q --disable-warnings` verification, or the Poetry "
                f"equivalent, that proves tests are collectable. Full test failures do not need "
                f"to pass if collection succeeds.\n\n"
            )

        # pytest / unittest 格式失败
        pytest_fail = re.search(r'([1-9]\d*) failed', output, re.IGNORECASE)
        if pytest_fail:
            failed_count = pytest_fail.group(1)
            return (
                f"[SYSTEM] ⚠️  TEST FAILURE DETECTED: {failed_count} test(s) FAILED.\n"
                f"[SYSTEM] Per the No Excuses Rule, you CANNOT output 'Final Answer: Success' "
                f"from this failed command. If full test execution fails, rerun the Repo2Run "
                f"collection command and use it as final proof only if collection succeeds.\n\n"
            )

        pytest_error = re.search(r'([1-9]\d*) errors?', output, re.IGNORECASE)
        if pytest_error:
            error_count = pytest_error.group(1)
            return (
                f"[SYSTEM] ⚠️  TEST FAILURE DETECTED: {error_count} test error(s) reported.\n"
                f"[SYSTEM] Per the No Excuses Rule, you CANNOT output 'Final Answer: Success' "
                f"until Repo2Run-style pytest collection succeeds without collection/import/config "
                f"errors.\n\n"
            )

        if self._command_classifier.observation_has_test_failure_signal(output):
            return (
                "[SYSTEM] ⚠️  TEST FAILURE DETECTED in command output.\n"
                "[SYSTEM] Per the No Excuses Rule, you CANNOT output 'Final Answer: Success' "
                "from this failed command. Final proof must be a successful Repo2Run-style "
                "pytest collection command, not a failed full test run.\n\n"
            )

        # 通用失败关键词。只匹配测试用例行，避免把 "Failed: 0" 当成失败。
        if (
            re.search(r'^\s*(?:FAILED|ERROR)\s+\S+', output, re.MULTILINE)
            or re.search(r'^\s*not ok\b', output, re.IGNORECASE | re.MULTILINE)
        ):
            return (
                "[SYSTEM] ⚠️  TEST FAILURE DETECTED in command output.\n"
                "[SYSTEM] Per the No Excuses Rule, you CANNOT output 'Final Answer: Success' "
                "from this failed command. Final proof must be a successful Repo2Run-style "
                "pytest collection command.\n\n"
            )

        return ""

    def _get_truncated_test_output_prefix(self, command):
        """Warn when a test command is piped through a lossy output filter."""
        if not self._command_classifier.is_truncated_test_output_command(command or ""):
            return ""

        return (
            "[SYSTEM] ⚠️  TRUNCATED TEST OUTPUT: this command pipes a test run through "
            "`head`, `tail`, or `grep`, so the Observation may omit failures, passes, or the final "
            "test summary.\n"
            "[SYSTEM] Do NOT treat this as complete verification. For final verification, "
            "run the full project test command without output-limiting pipes; long output "
            "will be handled by observation compression.\n\n"
        )

    def _get_preflight_rejection_prefix(self, command):
        """Reject commands that are too ambiguous to record/replay safely."""
        return (
            self._get_invalid_output_filter_prefix(command)
            or self._get_invalid_compound_setup_prefix(command)
        )

    def _get_invalid_output_filter_prefix(self, command):
        """Reject setup/test commands piped through lossy output filters."""
        if not self._command_pipes_setup_or_test_through_output_filter(command or ""):
            return ""

        return (
            "[SYSTEM] COMMAND REJECTED BEFORE EXECUTION: setup or test commands "
            "must not pipe output through `head`, `tail`, or `grep` because those "
            "filters can hide failures and mask the real exit status.\n"
            "[SYSTEM] The command was NOT executed and the environment was not "
            "changed. Rerun the full command without output filtering. Long output "
            "will be handled by observation compression.\n\n"
        )

    def _command_pipes_setup_or_test_through_output_filter(self, command):
        for raw_segment, _ in self._command_classifier._split_shell_chain(command or ""):
            pipeline_components = self._command_classifier._split_pipeline(raw_segment)
            if len(pipeline_components) < 2:
                continue

            saw_setup_or_test = False
            for component in pipeline_components:
                segment_type = self._classify_preflight_segment(component)
                if segment_type == "output_filter" and saw_setup_or_test:
                    return True
                if segment_type in {"setup", "test", "probe"}:
                    saw_setup_or_test = True
        return False

    def _get_invalid_compound_setup_prefix(self, command):
        """Reject multi-action setup chains that should be split into separate Actions."""
        segments = self._classified_preflight_segments(command)
        if len(segments) <= 1:
            return ""

        if self._is_allowed_apt_update_install_chain(segments):
            return ""

        setup_segments = [segment for segment in segments if segment["type"] == "setup"]
        if len(setup_segments) >= 2:
            return self._build_invalid_compound_setup_message(
                "this Action combines multiple independent setup mutations",
            )

        if len(setup_segments) == 1:
            mixed_segments = [
                segment
                for segment in segments
                if segment["type"] in {"test", "probe", "readonly"}
            ]
            if mixed_segments:
                return self._build_invalid_compound_setup_message(
                    "this Action combines a setup mutation with a verification, probe, or read-only check",
                )

        return ""

    def _classified_preflight_segments(self, command):
        segments = []
        for raw_segment, _ in self._command_classifier._split_shell_chain(command or ""):
            segment_type = self._classify_preflight_segment(raw_segment)
            if segment_type == "empty":
                continue
            segments.append({"raw": raw_segment.strip(), "type": segment_type})
        return segments

    def _classify_preflight_segment(self, raw_segment):
        cleaned_segment = self._command_classifier._strip_trailing_redirections(
            raw_segment or ""
        )
        normalized = self._command_classifier._normalize_command_segment(cleaned_segment)
        if not normalized:
            return "empty"
        if self._command_classifier._is_output_truncation_component(normalized):
            return "output_filter"
        if self._command_classifier._is_navigation_only_segment(normalized):
            return "navigation"
        if self._is_setup_preparation_segment(normalized):
            return "preparation"
        if self._command_classifier._is_test_like_segment(normalized):
            return "test"
        if self._is_probe_segment(normalized):
            return "probe"
        if self._command_classifier._is_readonly_command(cleaned_segment):
            return "readonly"
        if self._command_classifier._segment_has_meaningful_setup_activity(normalized):
            return "setup"
        return "other"

    def _is_setup_preparation_segment(self, normalized_segment):
        return (
            normalized_segment.startswith("mkdir -p ")
            or normalized_segment.startswith("mkdir --parents ")
            or normalized_segment.startswith("install -d ")
        )

    def _is_probe_segment(self, normalized_segment):
        probe_patterns = (
            r"^(?:python|python2|python3)\s+-c\b",
            r"^(?:node)\s+-e\b",
            r"^(?:ruby)\s+-e\b",
            r"^(?:php)\s+-r\b",
            r"^(?:pip|pip2|pip3|python\s+-m\s+pip|python3\s+-m\s+pip)\s+(?:check|show|list|freeze)\b",
            r"^(?:npm|yarn|pnpm)\s+(?:list|ls)\b",
        )
        return any(re.search(pattern, normalized_segment) for pattern in probe_patterns)

    def _is_allowed_apt_update_install_chain(self, segments):
        meaningful_segments = [
            segment
            for segment in segments
            if segment["type"] not in {"navigation", "preparation"}
        ]
        if len(meaningful_segments) != 2:
            return False

        first = self._command_classifier._normalize_command_segment(
            meaningful_segments[0]["raw"]
        )
        second = self._command_classifier._normalize_command_segment(
            meaningful_segments[1]["raw"]
        )
        return (
            re.match(r"^(?:apt-get|apt)\s+update\b", first) is not None
            and re.match(r"^(?:apt-get|apt)\s+install\b", second) is not None
        )

    def _build_invalid_compound_setup_message(self, reason):
        return (
            f"[SYSTEM] COMMAND REJECTED BEFORE EXECUTION: {reason}.\n"
            "[SYSTEM] The command was NOT executed and the environment was not changed. "
            "Run each setup mutation, verification, or probe as a separate Action so each "
            "state-changing step can be confirmed independently.\n\n"
        )

    def _get_failed_mutating_command_prefix(self, command, exit_code):
        """Warn when a failed setup command may have left partial environment changes."""
        if exit_code == 0:
            return ""

        if not self._command_classifier.command_mutates_environment(command or ""):
            return ""

        return (
            "[SYSTEM] FAILED SETUP MUTATION: this setup command failed after attempting "
            "to change the environment.\n"
            "[SYSTEM] It may have partially installed packages, modified files, or changed "
            "services. Do not assume useful parts of this failed command are reliably "
            "available for later steps.\n"
            "[SYSTEM] If any prefix or sub-step appears useful, rerun that prefix/sub-step "
            "as its own separate Action so it is confirmed successful. If the partial "
            "changes may have polluted the environment, use `Action: __ROLLBACK__` to "
            "restore the previous snapshot.\n\n"
        )

    def _get_package_manager_rollback_prefix(self, command, exit_code, output):
        """Warn the agent when package-manager failures strongly suggest a dirty dependency state."""
        if exit_code == 0:
            return ""

        normalized_command = (command or "").strip().lower()
        output_lower = (output or "").lower()

        if not self._looks_like_package_manager_command(normalized_command):
            self.package_manager_broken_failure_streak = 0
            return ""

        broken_state_markers = (
            "unmet dependencies",
            "fix-broken install",
            "is not going to be installed",
            "depends:",
            "correcting dependencies... done",
        )
        if not any(marker in output_lower for marker in broken_state_markers):
            self.package_manager_broken_failure_streak = 0
            return ""

        self.package_manager_broken_failure_streak += 1

        if self.package_manager_broken_failure_streak >= 2:
            return (
                "[SYSTEM] STRONG ROLLBACK CANDIDATE: package-manager recovery is still failing "
                "after repeated attempts, and the container likely remains in a broken dependency state.\n"
                "[SYSTEM] Unless you have a concrete repair step that will cleanly resolve the package state, "
                "seriously consider `Action: __ROLLBACK__` before continuing.\n\n"
            )

        return (
            "[SYSTEM] ROLLBACK CANDIDATE: this package-manager failure indicates broken dependencies "
            "or partial package state.\n"
            "[SYSTEM] Consider `Action: __ROLLBACK__` if you believe the failed install left the environment "
            "in an uncertain state, especially before trying unrelated setup work.\n\n"
        )

    def _looks_like_package_manager_command(self, normalized_command):
        package_manager_prefixes = (
            "apt ",
            "apt-get ",
            "yum ",
            "dnf ",
            "apk ",
            "pacman ",
            "zypper ",
        )
        return normalized_command.startswith(package_manager_prefixes)

    def close(self, keep_alive=False):
        """关闭容器，可选择保持容器运行以供验证"""
        if self.container:
            if keep_alive:
                print(f"\n[Container Kept Alive] ID: {self.container.short_id}")
                print(f"To inspect: docker exec -it {self.container.short_id} /bin/bash")
                print(f"To stop later: docker stop {self.container.short_id}")
            else:
                try:
                    self.container.stop(timeout=0)
                    self.container.remove()
                    print("\n[Container Cleaned Up]")
                except docker.errors.DockerException:
                    pass

        for snapshot_id in list(self.snapshot_image_ids):
            try:
                self._remove_snapshot_image(snapshot_id)
                print("[Snapshot Image Cleaned]")
            except docker.errors.DockerException:
                pass
