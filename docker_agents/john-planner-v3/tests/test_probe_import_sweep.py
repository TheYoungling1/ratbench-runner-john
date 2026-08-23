from src.envstate.world_model import apply_deterministic, initial_map, merge_map, Fact


def _base():
    return initial_map(base_image="b", workdir="/r", language="python 3.11",
                       build_system="pip", repo_layout=("tests/",), required=(Fact("opencv-python", ""),))


def test_apply_deterministic_folds_import_results():
    snap = type("S", (), {"env": {"python_version": "3.11"}, "installed": (Fact("opencv-python", ""),),
                          "system_installed": (), "import_results": (("cv2", False),)})()
    man = type("M", (), {"required": (Fact("opencv-python", ""),), "build_system": "pip"})()
    m = apply_deterministic(_base(), snap, man)
    assert m.import_results == (("cv2", False),)


def test_apply_deterministic_preserves_import_results_on_empty_snap():
    """When probe_env() returns empty import_results (the typical case), any
    previously-stored import_results in the map must be preserved — not erased.

    This guards against the bug where apply_deterministic unconditionally
    overwrites import_results with snap.import_results even when it is ().
    """
    prior = merge_map(_base(), import_results=(("cv2", True),))
    # probe_env() always returns import_results=() — simulate that
    snap = type("S", (), {"env": {"python_version": "3.11"}, "installed": (Fact("opencv-python", ""),),
                          "system_installed": (), "import_results": ()})()
    man = type("M", (), {"required": (Fact("opencv-python", ""),), "build_system": "pip"})()
    m = apply_deterministic(prior, snap, man)
    assert m.import_results == (("cv2", True),), (
        "import_results must be preserved when snap.import_results is empty"
    )
