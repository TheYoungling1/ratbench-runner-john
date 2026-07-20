from python_deps.depgraph.wheel_oracle import (
    _artifact_filename,
    _wheel_matches_platform,
    risk_from_packages,
)

LINUX_X86 = "x86_64-manylinux_2_28"


def test_artifact_filename_prefers_explicit_filename():
    assert _artifact_filename({"filename": "foo-1.0.tar.gz"}) == "foo-1.0.tar.gz"


def test_artifact_filename_derives_from_url():
    assert _artifact_filename({"url": "https://x/foo-1.0-py3-none-any.whl"}) == "foo-1.0-py3-none-any.whl"


def test_artifact_filename_none_for_non_dict():
    assert _artifact_filename(None) is None


def test_wheel_matches_platform_universal_wheel():
    assert _wheel_matches_platform("foo-1.0-py3-none-any.whl", LINUX_X86) is True


def test_wheel_matches_platform_arch_mismatch():
    assert _wheel_matches_platform("foo-1.0-cp311-cp311-macosx_11_0_arm64.whl", LINUX_X86) is False


def test_wheel_matches_platform_sdist_never_matches():
    assert _wheel_matches_platform("foo-1.0.tar.gz", LINUX_X86) is False


def test_risk_from_packages_build_from_source_when_no_matching_wheel():
    raw = [{
        "name": "psycopg2",
        "sdist": {"filename": "psycopg2-2.9.9.tar.gz", "hash": "sha256:abc"},
        "wheels": [{"filename": "psycopg2-2.9.9-cp311-cp311-macosx_11_0_arm64.whl"}],
    }]
    risk = risk_from_packages(raw, LINUX_X86)
    assert risk["psycopg2"]["build_from_source"] is True
    assert risk["psycopg2"]["artifact"] == "psycopg2-2.9.9.tar.gz"
    assert risk["psycopg2"]["hash"] == "sha256:abc"


def test_risk_from_packages_prefers_matching_wheel():
    raw = [{
        "name": "requests",
        "sdist": {"filename": "requests-2.31.0.tar.gz"},
        "wheels": [{"filename": "requests-2.31.0-py3-none-any.whl", "hash": "sha256:xyz"}],
    }]
    risk = risk_from_packages(raw, LINUX_X86)
    assert risk["requests"]["build_from_source"] is False
    assert risk["requests"]["artifact"] == "requests-2.31.0-py3-none-any.whl"


def test_risk_from_packages_skips_unnamed_entries():
    assert risk_from_packages([{"source": {}}], LINUX_X86) == {}
