from src.eval.graph_repair_ablation.grade import grade_localization
from src.eval.graph_repair_ablation.oracle import PILOT_INJECTIONS

BY = {i.failure_class: i for i in PILOT_INJECTIONS}

def test_install_class_localized_at_1():
    inj = BY["SYSLIB_MISSING"]  # correct target apt:libgraphviz-dev
    trace = {"actions": ["apt-cache search libgraphviz-dev"], "patch": {"kind": "install", "target": "apt:libgraphviz-dev"}}
    s = grade_localization(trace, inj)
    assert s.localized_at_1 and s.first_correct_rank == 1 and not s.mislocalized

def test_install_class_wasted_actions_before_localizing():
    inj = BY["SYSLIB_MISSING"]
    trace = {"actions": ["pip show requests", "ls /tmp", "apt-cache search libgraphviz-dev"],
             "patch": {"kind": "install", "target": "apt:libgraphviz-dev"}}
    s = grade_localization(trace, inj)
    assert not s.localized_at_1 and s.localized_at_3
    assert s.first_correct_rank == 3
    assert abs(s.wasted_rate - 2/3) < 1e-6

def test_drop_class_install_attempt_is_mislocalization():
    inj = BY["OVERINCLUDE"]  # correct action: drop the optional pkg
    trace = {"actions": ["pip install this-optional-pkg-fails-to-build"],
             "patch": {"kind": "install", "target": "this-optional-pkg-fails-to-build"}}
    s = grade_localization(trace, inj)
    assert s.mislocalized and not s.localized_at_3

def test_drop_class_correct_when_patch_drops():
    inj = BY["OVERINCLUDE"]
    trace = {"actions": ["cat requirements.txt"], "patch": {"kind": "drop", "target": "this-optional-pkg-fails-to-build"}}
    s = grade_localization(trace, inj)
    assert s.localized_at_3 and not s.mislocalized

def test_repin_class():
    inj = BY["VERSION_CONFLICT"]  # target urllib3
    trace = {"actions": ["pip index versions urllib3"], "patch": {"kind": "repin", "target": "urllib3"}}
    s = grade_localization(trace, inj)
    assert s.localized_at_3 and not s.mislocalized
