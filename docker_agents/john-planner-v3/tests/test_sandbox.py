import unittest
from types import SimpleNamespace
from unittest import mock

from src.sandbox import Sandbox
from src.synthesizer import Synthesizer


class FakeContainer:
    def __init__(self, results=None, status="running", short_id="fake123"):
        self.results = list(results or [])
        self.calls = []
        self.status = status
        self.short_id = short_id
        self.stopped = False
        self.stop_timeout = None
        self.removed = False

    def exec_run(self, command, workdir=None):
        self.calls.append({"command": command, "workdir": workdir})
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(exit_code=0, output=b"")

    def stop(self, timeout=None):
        self.stopped = True
        self.stop_timeout = timeout

    def remove(self):
        self.removed = True

    def reload(self):
        return None

    def commit(self):
        return SimpleNamespace(id="snapshot123456")


class CountingCommitContainer(FakeContainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commit_calls = 0

    def commit(self):
        self.commit_calls += 1
        return SimpleNamespace(id="should-not-happen")


class FakeContainerManager:
    def __init__(self, containers=None):
        self.containers_to_return = list(containers or [])
        self.run_calls = []

    def run(self, *args, **kwargs):
        self.run_calls.append({"args": args, "kwargs": kwargs})
        if not self.containers_to_return:
            raise AssertionError("No fake container available for run()")
        return self.containers_to_return.pop(0)


class FakeDockerClient:
    def __init__(self, containers=None):
        self.containers = FakeContainerManager(containers=containers)


class SandboxRuntimeReplayTests(unittest.TestCase):
    def _make_sandbox(self, replay_results=None):
        sandbox = Sandbox.__new__(Sandbox)
        sandbox.runtime_replay_commands = []
        sandbox.package_manager_broken_failure_streak = 0
        sandbox._command_classifier = Synthesizer()
        sandbox.command_timeout_seconds = None
        sandbox.workdir = "/app"
        sandbox.container = FakeContainer(results=replay_results)
        sandbox.client = FakeDockerClient()
        sandbox.last_success_image = "snapshot123"
        sandbox.snapshot_image_ids = set()
        sandbox.base_image = "ubuntu:22.04"
        sandbox.volumes = None
        sandbox.platform = None
        return sandbox

    def test_tracks_runtime_service_start_for_replay(self):
        sandbox = self._make_sandbox()

        sandbox._track_runtime_command("service postgresql start")

        self.assertEqual(
            sandbox.runtime_replay_commands,
            [{"key": "postgresql", "command": "service postgresql start"}],
        )

    def test_replaces_existing_runtime_command_for_same_service(self):
        sandbox = self._make_sandbox()

        sandbox._track_runtime_command("service postgresql start")
        sandbox._track_runtime_command("service postgresql restart")

        self.assertEqual(
            sandbox.runtime_replay_commands,
            [{"key": "postgresql", "command": "service postgresql restart"}],
        )

    def test_stop_command_removes_runtime_service_from_replay_list(self):
        sandbox = self._make_sandbox()

        sandbox._track_runtime_command("service rabbitmq-server start")
        sandbox._track_runtime_command("service rabbitmq-server stop")

        self.assertEqual(sandbox.runtime_replay_commands, [])

    def test_does_not_track_persistent_setup_chains_for_runtime_replay(self):
        sandbox = self._make_sandbox()

        sandbox._track_runtime_command(
            "apt-get update && apt-get install -y postgresql && service postgresql start"
        )

        self.assertEqual(sandbox.runtime_replay_commands, [])

    def test_replay_restores_successful_runtime_commands_and_drops_failed_ones(self):
        sandbox = self._make_sandbox(
            replay_results=[
                SimpleNamespace(exit_code=0, output=b"started"),
                SimpleNamespace(exit_code=1, output=b"failed"),
            ]
        )
        sandbox.runtime_replay_commands = [
            {"key": "postgresql", "command": "service postgresql start"},
            {"key": "rabbitmq-server", "command": "service rabbitmq-server start"},
        ]

        sandbox._replay_runtime_commands()

        self.assertEqual(len(sandbox.container.calls), 2)
        self.assertEqual(
            sandbox.container.calls[0]["command"],
            ["/bin/bash", "-c", "/bin/bash -o pipefail -lc 'service postgresql start'"],
        )
        self.assertEqual(sandbox.container.calls[0]["workdir"], "/app")
        self.assertEqual(
            sandbox.container.calls[1]["command"],
            ["/bin/bash", "-c", "/bin/bash -o pipefail -lc 'service rabbitmq-server start'"],
        )
        self.assertEqual(
            sandbox.runtime_replay_commands,
            [{"key": "postgresql", "command": "service postgresql start"}],
        )

    def test_failed_command_does_not_auto_rollback_when_container_is_healthy(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=1, output=b"plain failure")],
            status="running",
        )
        original_container = sandbox.container

        success, output = sandbox.execute("false")

        self.assertFalse(success)
        self.assertEqual(output, "plain failure")
        self.assertIs(sandbox.container, original_container)
        self.assertFalse(original_container.stopped)
        self.assertFalse(original_container.removed)

    def test_failed_mutating_command_adds_prefix_rerun_or_rollback_guidance(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=1, output=b"pytorch3d not found")],
            status="running",
        )

        success, output = sandbox.execute("pip3 install pytorch3d")

        self.assertFalse(success)
        self.assertIn("FAILED SETUP MUTATION", output)
        self.assertIn("partially installed packages", output)
        self.assertIn("rerun that prefix/sub-step as its own separate Action", output)
        self.assertIn("Action: __ROLLBACK__", output)
        self.assertIn("pytorch3d not found", output)

    def test_transient_pip_install_failure_is_retried_before_reporting(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[
                SimpleNamespace(
                    exit_code=1,
                    output=b"pip._vendor.urllib3.exceptions.ReadTimeoutError: timed out",
                ),
                SimpleNamespace(exit_code=0, output=b"Successfully installed robustbench"),
            ],
            status="running",
        )
        sandbox.last_success_image = None

        success, output = sandbox.execute("python -m pip install robustbench")

        self.assertTrue(success)
        self.assertIn("Transient pip install failure on attempt 1", output)
        self.assertIn("Successfully installed robustbench", output)
        self.assertEqual(len(sandbox.container.calls), 2)

    def test_failed_readonly_command_does_not_add_mutation_guidance(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=1, output=b"missing file")],
            status="running",
        )

        success, output = sandbox.execute("cat /app/missing-file")

        self.assertFalse(success)
        self.assertNotIn("FAILED SETUP MUTATION", output)
        self.assertEqual(output, "missing file")

    def test_explicit_rollback_restores_last_success_snapshot(self):
        sandbox = self._make_sandbox()
        original_container = FakeContainer()
        restored_container = FakeContainer()
        sandbox.container = original_container
        sandbox.client = FakeDockerClient(containers=[restored_container])

        success, output = sandbox.rollback()

        self.assertTrue(success)
        self.assertIn("Restored the container to the last successful snapshot", output)
        self.assertTrue(original_container.stopped)
        self.assertTrue(original_container.removed)
        self.assertIs(sandbox.container, restored_container)
        self.assertEqual(restored_container.calls[0]["command"], "mkdir -p /app")

    def test_failed_command_forces_recovery_when_container_is_unhealthy(self):
        sandbox = self._make_sandbox()
        original_container = FakeContainer(
            results=[SimpleNamespace(exit_code=1, output=b"broken state")],
            status="exited",
        )
        restored_container = FakeContainer()
        sandbox.container = original_container
        sandbox.client = FakeDockerClient(containers=[restored_container])

        success, output = sandbox.execute("failing-command")

        self.assertFalse(success)
        self.assertIn("automatically", output)
        self.assertTrue(original_container.stopped)
        self.assertTrue(original_container.removed)
        self.assertIs(sandbox.container, restored_container)

    def test_package_manager_broken_state_failure_adds_rollback_candidate_hint(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[
                SimpleNamespace(
                    exit_code=100,
                    output=(
                        b"You might want to run 'apt --fix-broken install' to correct these.\n"
                        b"The following packages have unmet dependencies:\n"
                        b"postgresql-16 : Depends: postgresql-common but it is not going to be installed\n"
                    ),
                )
            ],
            status="running",
        )

        success, output = sandbox.execute(
            "apt-get update && apt-get install -y postgresql-16 postgresql-client-16 --fix-missing"
        )

        self.assertFalse(success)
        self.assertIn("ROLLBACK CANDIDATE", output)
        self.assertIn("Action: __ROLLBACK__", output)
        self.assertEqual(sandbox.package_manager_broken_failure_streak, 1)

    def test_test_error_count_adds_no_excuses_failure_prefix(self):
        sandbox = self._make_sandbox()

        prefix = sandbox._get_test_failure_prefix(
            1,
            "collected 32 items\n==================== 32 passed, 1 error in 0.31s ====================",
        )

        self.assertIn("TEST FAILURE DETECTED", prefix)
        self.assertIn("Repo2Run-style pytest collection", prefix)
        self.assertIn("collection/import/config errors", prefix)

    def test_zero_failed_summary_does_not_add_no_excuses_failure_prefix(self):
        sandbox = self._make_sandbox()

        prefix = sandbox._get_test_failure_prefix(
            1,
            "Tests run: 10, Failures: 0, Errors: 0, Skipped: 0",
        )

        self.assertEqual(prefix, "")

    def test_zero_failed_ctest_summary_does_not_add_no_excuses_failure_prefix(self):
        sandbox = self._make_sandbox()

        prefix = sandbox._get_test_failure_prefix(
            1,
            "\n".join(
                [
                    "Passed:                           660",
                    "Failed:                             0",
                    "Unexpected successes:               0",
                    "[100%] Built target test",
                ]
            ),
        )

        self.assertEqual(prefix, "")

    def test_repeated_package_manager_recovery_failures_upgrade_hint_strength(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[
                SimpleNamespace(
                    exit_code=100,
                    output=(
                        b"Correcting dependencies... Done\n"
                        b"E: Unable to fetch some archives\n"
                    ),
                ),
                SimpleNamespace(
                    exit_code=100,
                    output=(
                        b"Correcting dependencies... Done\n"
                        b"The following packages have unmet dependencies:\n"
                        b"libpq5 : Depends: something but it is not going to be installed\n"
                    ),
                ),
            ],
            status="running",
        )

        first_success, first_output = sandbox.execute("apt --fix-broken install -y")
        second_success, second_output = sandbox.execute("apt --fix-broken install -y")

        self.assertFalse(first_success)
        self.assertIn("ROLLBACK CANDIDATE", first_output)
        self.assertFalse(second_success)
        self.assertIn("STRONG ROLLBACK CANDIDATE", second_output)
        self.assertEqual(sandbox.package_manager_broken_failure_streak, 2)

    def test_truncated_test_output_is_rejected_before_execution(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[
                SimpleNamespace(
                    exit_code=0,
                    output=b"collected 207 items\n207 passed in 47.25s\n",
                )
            ],
            status="running",
        )

        success, output = sandbox.execute(
            "cd /app && python -m pytest tests -v 2>&1 | head -100"
        )

        self.assertFalse(success)
        self.assertIn("COMMAND REJECTED BEFORE EXECUTION", output)
        self.assertIn("was NOT executed", output)
        self.assertEqual(sandbox.container.calls, [])

    def test_setup_output_filter_is_rejected_before_execution(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=0, output=b"installed")],
            status="running",
        )

        success, output = sandbox.execute("pip3 install pybullet 2>&1 | tail -20")

        self.assertFalse(success)
        self.assertIn("COMMAND REJECTED BEFORE EXECUTION", output)
        self.assertIn("must not pipe output through `head`, `tail`, or `grep`", output)
        self.assertEqual(sandbox.container.calls, [])

    def test_readonly_output_filter_is_allowed(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=0, output=b"setup.py\n")],
            status="running",
        )

        success, output = sandbox.execute("find /app -name setup.py 2>/dev/null | head -20")

        self.assertTrue(success)
        self.assertEqual(output, "setup.py\n")
        self.assertEqual(len(sandbox.container.calls), 1)

    def test_compound_setup_mutations_are_rejected_before_execution(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=0, output=b"installed")],
            status="running",
        )

        success, output = sandbox.execute("pip3 install pybullet && pip3 install pytorch3d")

        self.assertFalse(success)
        self.assertIn("multiple independent setup mutations", output)
        self.assertIn("environment was not changed", output)
        self.assertEqual(sandbox.container.calls, [])

    def test_mutation_plus_probe_is_rejected_before_execution(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=0, output=b"ok")],
            status="running",
        )

        success, output = sandbox.execute("pip3 install pybullet && python -c 'import pybullet'")

        self.assertFalse(success)
        self.assertIn("setup mutation with a verification, probe, or read-only check", output)
        self.assertEqual(sandbox.container.calls, [])

    def test_navigation_then_single_setup_mutation_is_allowed(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=0, output=b"installed")],
            status="running",
        )
        sandbox.last_success_image = None

        success, output = sandbox.execute("cd /app && pip3 install -e .")

        self.assertTrue(success)
        self.assertEqual(output, "installed")
        self.assertEqual(len(sandbox.container.calls), 1)

    def test_apt_update_install_chain_is_allowed(self):
        sandbox = self._make_sandbox()
        sandbox.container = FakeContainer(
            results=[SimpleNamespace(exit_code=0, output=b"installed cmake")],
            status="running",
        )
        sandbox.last_success_image = None

        success, output = sandbox.execute("apt-get update && apt-get install -y cmake")

        self.assertTrue(success)
        self.assertEqual(output, "installed cmake")
        self.assertEqual(len(sandbox.container.calls), 1)

    def test_execute_success_does_not_commit_per_command(self):
        sandbox = self._make_sandbox()
        sandbox.container = CountingCommitContainer(
            results=[SimpleNamespace(exit_code=0, output=b"ok")],
            status="running",
        )
        sandbox.last_success_image = "baseline-img"

        success, output = sandbox.execute("pip3 install foo")

        self.assertTrue(success)
        self.assertEqual(output, "ok")
        self.assertEqual(sandbox.container.commit_calls, 0)  # no per-command snapshot
        self.assertEqual(sandbox.last_success_image, "baseline-img")  # unchanged

    def test_should_commit_attribute_removed(self):
        self.assertFalse(hasattr(Sandbox, "_should_commit"))

    def test_command_wrapper_uses_pipefail_without_timeout(self):
        sandbox = self._make_sandbox()
        sandbox.command_timeout_seconds = None

        wrapped = sandbox._wrap_command_with_timeout("pip3 install missing-package | tail -20")

        self.assertIn("/bin/bash -o pipefail -lc", wrapped)

    def test_command_wrapper_uses_pipefail_with_timeout(self):
        sandbox = self._make_sandbox()
        sandbox.command_timeout_seconds = 42

        wrapped = sandbox._wrap_command_with_timeout("pip3 install missing-package | tail -20")

        self.assertIn("timeout --foreground --kill-after=30s 42s /bin/bash -o pipefail -lc", wrapped)


class SandboxAptBootstrapTests(unittest.TestCase):
    def _make_sandbox(self, mirror_url=None):
        sandbox = Sandbox.__new__(Sandbox)
        sandbox.workdir = "/app"
        sandbox.container = FakeContainer()
        sandbox.apt_mirror_url = mirror_url
        sandbox.apt_retries = 5
        sandbox.apt_http_timeout_seconds = 120
        sandbox.apt_https_timeout_seconds = 120
        return sandbox

    def test_build_apt_bootstrap_command_always_configures_retry_settings(self):
        sandbox = self._make_sandbox()

        command = sandbox._build_apt_bootstrap_command()

        self.assertIn('Acquire::Retries "5";', command)
        self.assertIn('Acquire::http::Timeout "120";', command)
        self.assertIn('Acquire::https::Timeout "120";', command)
        self.assertNotIn("apt-get update\n", command)

    def test_build_apt_bootstrap_command_rewrites_sources_when_mirror_is_configured(self):
        sandbox = self._make_sandbox("https://mirror.example.com/ubuntu")

        command = sandbox._build_apt_bootstrap_command()

        self.assertIn("APT_MIRROR_URL=https://mirror.example.com/ubuntu", command)
        self.assertIn('sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR_URL}|g"', command)
        self.assertIn('sed -i "s|http://security.ubuntu.com/ubuntu|${APT_MIRROR_URL}|g"', command)
        self.assertIn("apt-get update", command)

    def test_bootstrap_apt_executes_once_on_container(self):
        sandbox = self._make_sandbox("https://mirror.example.com/ubuntu")

        sandbox._bootstrap_apt_if_supported()

        self.assertEqual(len(sandbox.container.calls), 1)
        self.assertEqual(sandbox.container.calls[0]["command"][0:2], ["/bin/bash", "-lc"])
        self.assertIn("99jayint-retries", sandbox.container.calls[0]["command"][2])

    def test_resolve_apt_mirror_url_prefers_explicit_value(self):
        sandbox = Sandbox.__new__(Sandbox)

        resolved = sandbox._resolve_apt_mirror_url("https://mirror.example.com/ubuntu/")

        self.assertEqual(resolved, "https://mirror.example.com/ubuntu")

    @mock.patch("src.sandbox.docker.from_env")
    def test_init_uses_extended_docker_client_timeout(self, mock_from_env):
        fake_container = FakeContainer()
        fake_client = FakeDockerClient(containers=[fake_container])
        mock_from_env.return_value = fake_client

        sandbox = Sandbox(
            base_image="ubuntu:22.04",
            seed_dir=None,
            docker_client_timeout_seconds=600,
        )

        self.assertIs(sandbox.client, fake_client)
        mock_from_env.assert_called_once_with(timeout=600)

    def test_extra_hosts_added_when_arm_on(self):
        import os
        orig = os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION")
        os.environ["DOCKERAGENT_ENABLE_SERVICE_PROVISION"] = "1"
        try:
            captured = {}
            fake_container = FakeContainer()

            class _Containers:
                def run(self, image, **kwargs):
                    captured.update(kwargs)
                    return fake_container

            class _Client:
                containers = _Containers()

            import src.sandbox as sb
            s = sb.Sandbox.__new__(sb.Sandbox)
            s.client = _Client()
            s.current_image = "python:3.11-slim"
            s.platform = None
            s.workdir = "/app"
            s.volumes = {}
            s.seed_dir = None
            s._bootstrap_apt_if_supported = lambda: None
            s._register_snapshot = lambda *a: None
            s._setup_initial_container()
            self.assertEqual(captured.get("extra_hosts"), {"postgres": "127.0.0.1"})
        finally:
            if orig is None:
                os.environ.pop("DOCKERAGENT_ENABLE_SERVICE_PROVISION", None)
            else:
                os.environ["DOCKERAGENT_ENABLE_SERVICE_PROVISION"] = orig

    def test_no_extra_hosts_off_arm(self):
        import os
        orig = os.environ.get("DOCKERAGENT_ENABLE_SERVICE_PROVISION")
        os.environ.pop("DOCKERAGENT_ENABLE_SERVICE_PROVISION", None)
        try:
            captured = {}
            fake_container = FakeContainer()

            class _Containers:
                def run(self, image, **kwargs):
                    captured.update(kwargs)
                    return fake_container

            class _Client:
                containers = _Containers()

            import src.sandbox as sb
            s = sb.Sandbox.__new__(sb.Sandbox)
            s.client = _Client()
            s.current_image = "python:3.11-slim"
            s.platform = None
            s.workdir = "/app"
            s.volumes = {}
            s.seed_dir = None
            s._bootstrap_apt_if_supported = lambda: None
            s._register_snapshot = lambda *a: None
            s._setup_initial_container()
            self.assertNotIn("extra_hosts", captured)
        finally:
            if orig is None:
                os.environ.pop("DOCKERAGENT_ENABLE_SERVICE_PROVISION", None)
            else:
                os.environ["DOCKERAGENT_ENABLE_SERVICE_PROVISION"] = orig


class SandboxTeardownTimeoutTests(unittest.TestCase):
    """Throwaway containers (reset_to_base/_restore_last_success_container/close)
    are stopped and discarded, never reused — SIGKILL-immediately (timeout=0) is
    correct and saves the default 10s SIGTERM grace on each teardown."""

    def _make_sandbox(self, replacement_container=None):
        sandbox = Sandbox.__new__(Sandbox)
        sandbox.container = FakeContainer()
        sandbox.client = FakeDockerClient(containers=[replacement_container or FakeContainer()])
        sandbox.base_image = "ubuntu:22.04"
        sandbox.workdir = "/app"
        sandbox.volumes = None
        sandbox.platform = None
        sandbox.seed_dir = None
        sandbox.apt_mirror_url = None
        sandbox.apt_retries = 5
        sandbox.apt_http_timeout_seconds = 120
        sandbox.apt_https_timeout_seconds = 120
        sandbox.last_success_image = None
        sandbox.runtime_replay_commands = []
        sandbox.snapshot_image_ids = set()
        return sandbox

    def test_reset_to_base_stops_discarded_container_with_zero_timeout(self):
        sandbox = self._make_sandbox()
        original_container = sandbox.container

        sandbox.reset_to_base()

        self.assertTrue(original_container.stopped)
        self.assertEqual(original_container.stop_timeout, 0)
        self.assertTrue(original_container.removed)

    def test_restore_last_success_container_stops_discarded_container_with_zero_timeout(self):
        sandbox = self._make_sandbox()
        original_container = sandbox.container

        sandbox._restore_last_success_container()

        self.assertTrue(original_container.stopped)
        self.assertEqual(original_container.stop_timeout, 0)
        self.assertTrue(original_container.removed)

    def test_close_stops_container_with_zero_timeout(self):
        sandbox = self._make_sandbox()
        original_container = sandbox.container

        sandbox.close()

        self.assertTrue(original_container.stopped)
        self.assertEqual(original_container.stop_timeout, 0)
        self.assertTrue(original_container.removed)


class SandboxCacheVolumeTests(unittest.TestCase):
    """enable_cache_volume should add named-volume binds for pip/apt caches so
    repeated installs across cycles/runs reuse a warm cache instead of
    re-downloading every time; default (False) must not add them."""

    @mock.patch("src.sandbox.docker.from_env")
    def test_enable_cache_volume_true_adds_pip_and_apt_cache_binds(self, mock_from_env):
        fake_container = FakeContainer()
        fake_client = FakeDockerClient(containers=[fake_container])
        mock_from_env.return_value = fake_client

        sandbox = Sandbox(base_image="ubuntu:22.04", seed_dir=None, enable_cache_volume=True)

        self.assertEqual(
            sandbox.volumes["jayint_pip_cache"],
            {"bind": "/root/.cache/pip", "mode": "rw"},
        )
        self.assertEqual(
            sandbox.volumes["jayint_apt_cache"],
            {"bind": "/var/cache/apt/archives", "mode": "rw"},
        )

    @mock.patch("src.sandbox.docker.from_env")
    def test_enable_cache_volume_default_false_omits_cache_binds(self, mock_from_env):
        fake_container = FakeContainer()
        fake_client = FakeDockerClient(containers=[fake_container])
        mock_from_env.return_value = fake_client

        sandbox = Sandbox(base_image="ubuntu:22.04", seed_dir=None)

        self.assertIsNone(sandbox.volumes)


if __name__ == "__main__":
    unittest.main()
