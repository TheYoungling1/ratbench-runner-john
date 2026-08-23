import unittest

from src.envstate.ledger import ActionEvent, ActionLedger


class ActionLedgerTests(unittest.TestCase):
    def test_append_is_ordered_and_immutable_view(self):
        ledger = ActionLedger()
        ledger.append(ActionEvent(
            step=1, task_id=None, cmd="apt-get install -y libpq-dev", rc=0,
            stdout_path=None, stderr_path=None,
            env_revision_before=7, env_revision_after=8,
            mutation_class="system_package_install", container_id="abc123",
            summary="Installed libpq-dev successfully",
        ))
        events = ledger.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].env_revision_after, 8)
        # events() returns an immutable snapshot tuple
        self.assertIsInstance(events, tuple)

    def test_to_list_emits_design_shape(self):
        ledger = ActionLedger()
        ledger.append(ActionEvent(
            step=17, task_id="task-004", cmd="pip install psycopg2==2.8.6", rc=1,
            stdout_path="logs/action_017.stdout", stderr_path="logs/action_017.stderr",
            env_revision_before=7, env_revision_after=7,
            mutation_class=None, container_id="abc123",
            summary="pg_config executable not found",
        ))
        row = ledger.to_list()[0]
        self.assertEqual(row["step"], 17)
        self.assertEqual(row["task_id"], "task-004")
        self.assertEqual(row["rc"], 1)
