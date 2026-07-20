import unittest

from src.envstate.extractor import (
    EXTRACTOR_COMMANDS,
    LIGHTWEIGHT_FIELDS,
    run_extractor,
)


class FakeExecutor:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, command):
        self.calls.append(command)
        return self.mapping.get(command, (1, ""))


class ExtractorTests(unittest.TestCase):
    def test_full_extractor_collects_known_fields(self):
        mapping = {
            EXTRACTOR_COMMANDS["python_version"]: (0, "Python 3.11.9\n"),
            EXTRACTOR_COMMANDS["pip_version"]: (0, "pip 24.0\n"),
            EXTRACTOR_COMMANDS["os_release"]: (0, 'ID=debian\nVERSION_CODENAME=bookworm\n'),
            EXTRACTOR_COMMANDS["arch"]: (0, "x86_64\n"),
        }
        result = run_extractor(FakeExecutor(mapping))
        self.assertEqual(result.fields["python_version"], "Python 3.11.9")
        self.assertEqual(result.fields["arch"], "x86_64")
        self.assertIn("debian", result.fields["os_release"])

    def test_lightweight_extractor_runs_subset(self):
        executor = FakeExecutor({cmd: (0, "ok") for cmd in EXTRACTOR_COMMANDS.values()})
        run_extractor(executor, fields=LIGHTWEIGHT_FIELDS)
        # only the lightweight field commands were executed
        self.assertEqual(len(executor.calls), len(LIGHTWEIGHT_FIELDS))

    def test_missing_command_is_recorded_but_not_fatal(self):
        result = run_extractor(FakeExecutor({}))  # everything returns (1, "")
        self.assertEqual(result.fields, {})
        self.assertTrue(all(rc == 1 for rc, _ in result.raw.values()))
