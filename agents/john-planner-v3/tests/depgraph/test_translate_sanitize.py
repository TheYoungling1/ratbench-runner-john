"""Pure unit tests for translate_sanitize (arch/env sanitizers)."""

from python_deps.depgraph.translate_sanitize import apply_arch, apply_env

_ARM = {"dpkg": "arm64", "uname": "aarch64"}
_AMD = {"dpkg": "amd64", "uname": "x86_64"}


def test_apply_arch_substitutes_tokens():
    out = apply_arch({"install": ["curl x/{ARCH_DPKG}/y"]}, _ARM)
    assert "arm64" in out["install"][0]
    assert "{ARCH_DPKG}" not in out["install"][0]


def test_apply_arch_substitutes_uname_token():
    out = apply_arch({"start": "run --arch {ARCH_UNAME}"}, _ARM)
    assert out["start"] == "run --arch aarch64"


def test_apply_arch_rewrites_hardcoded_amd64_in_url():
    out = apply_arch(
        {"install": ["wget https://h/app-linux-amd64.tar.gz"]}, _ARM
    )
    assert "linux-arm64.tar.gz" in out["install"][0]
    assert "amd64" not in out["install"][0]


def test_apply_arch_rewrites_x86_64_in_url():
    out = apply_arch(
        {"install": ["wget https://h/app-x86_64.bin"]}, _ARM
    )
    assert "aarch64" in out["install"][0]


def test_apply_arch_amd64_target_no_rewrite():
    out = apply_arch(
        {"install": ["wget https://h/app-linux-amd64.tar.gz"]}, _AMD
    )
    assert "linux-amd64.tar.gz" in out["install"][0]


def test_apply_arch_no_rewrite_outside_urls():
    # A bare (non-URL) string keeps a hardcoded amd64 even on arm targets.
    out = apply_arch({"post": ["dpkg --add-architecture amd64"]}, _ARM)
    assert out["post"][0] == "dpkg --add-architecture amd64"


def test_apply_arch_does_not_mutate_input():
    plan = {"install": ["curl x/{ARCH_DPKG}/y"], "start": "s {ARCH_UNAME}"}
    original_list = plan["install"]
    apply_arch(plan, _ARM)
    # The input dict, its list, and its strings are all untouched.
    assert plan["install"] is original_list
    assert plan["install"][0] == "curl x/{ARCH_DPKG}/y"
    assert plan["start"] == "s {ARCH_UNAME}"


def test_apply_arch_leaves_other_keys_untouched():
    out = apply_arch({"feasible": True, "note": "amd64 note"}, _ARM)
    assert out["feasible"] is True
    assert out["note"] == "amd64 note"


def test_apply_env_strips_leading_sudo():
    out = apply_env({"install": ["sudo apt-get install -y x"]})
    assert out["install"][0] == "apt-get install -y x"


def test_apply_env_strips_chained_sudo():
    out = apply_env({"install": ["a && sudo b"]})
    assert out["install"][0] == "a && b"


def test_apply_env_strips_semicolon_chained_sudo():
    out = apply_env({"start": "a; sudo b"})
    assert out["start"] == "a; b"


def test_apply_env_does_not_mutate_input():
    plan = {"install": ["sudo apt-get install -y x"]}
    original_list = plan["install"]
    apply_env(plan)
    assert plan["install"] is original_list
    assert plan["install"][0] == "sudo apt-get install -y x"


def test_apply_env_leaves_non_sudo_untouched():
    out = apply_env({"install": ["apt-get install -y sudo"]})
    # 'sudo' as an install target (no leading/chained "sudo ") is left alone.
    assert out["install"][0] == "apt-get install -y sudo"
