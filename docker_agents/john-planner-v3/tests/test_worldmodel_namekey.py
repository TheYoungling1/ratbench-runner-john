# tests/test_worldmodel_namekey.py
"""Name-key normalisation tests — superseded by tests/test_v1_maintainer.py.

The old parse_maintainer_proposal / apply_llm_proposal interface tested here
was removed in the v1 Maintainer rewrite.  These tests are kept as a
historical record but skipped unconditionally to avoid import errors.
"""
import unittest


class ObsoleteNameKeyNormalisationTests(unittest.TestCase):
    @unittest.skip("v0 parse_maintainer_proposal API removed; see tests/test_v1_maintainer.py")
    def test_v0_api_removed(self):
        pass
