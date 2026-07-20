# tests/test_envstate_maintainer.py
"""v0 Maintainer tests — superseded by tests/test_v1_maintainer.py.

The old Maintainer.interpret / build_maintainer_input / parse_maintainer_proposal
interface was removed in the v1 rewrite.  These tests are kept as a historical
record but skipped unconditionally to avoid import errors.
"""
import unittest


class ObsoleteMaintainerV0Tests(unittest.TestCase):
    @unittest.skip("v0 Maintainer API removed; see tests/test_v1_maintainer.py")
    def test_v0_api_removed(self):
        pass
