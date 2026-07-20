from src.envstate.contracts import ids


def test_id_grammar():
    assert ids.contract_id("python_import", "cv2") == "contract:python_import:cv2"
    assert ids.contract_id("system_library", "libGL.so.1") == "contract:system_library:libgl-so-1"
    assert ids.goal_contract_id("repo_tests_pass") == "contract:goal:repo_tests_pass"
    assert ids.foundational_contract_id("python_version_compatible") == "contract:python_version_compatible"
    assert ids.blocker_id("ImportError: libGL.so.1") == "blocker:importerror-libgl-so-1"
    assert ids.attempt_id("install libgl1") == "attempt:install-libgl1"
