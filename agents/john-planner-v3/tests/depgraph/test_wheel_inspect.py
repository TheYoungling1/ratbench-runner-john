import zipfile
from pathlib import Path

from python_deps.depgraph.executor import CommandResult
from python_deps.depgraph.wheel_inspect import (
    download_target_wheel,
    inspect_wheel_sonames,
)

FIXTURE_SO = Path(__file__).parent / "fixtures" / "mod.cpython-311-x86_64-linux-gnu.so"


def _wheel_with(tmp_path, arcname):
    whl = tmp_path / "pkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.write(FIXTURE_SO, arcname)
    return str(whl)


class _Exec:
    """Minimal Executor: canned rc, records calls."""

    def __init__(self, rc):
        self.rc = rc
        self.calls = []

    def run(self, command, *, timeout=300):
        self.calls.append(command)
        return CommandResult(command=command, returncode=self.rc, stdout="", stderr="")


def test_inspect_returns_external_soname_filtering_base_libs(tmp_path):
    wheel = _wheel_with(tmp_path, "mypkg/mod.cpython-311-x86_64-linux-gnu.so")
    # libc.so.6 is a base-image lib -> filtered; libGL.so.1 is the real need.
    assert inspect_wheel_sonames(wheel) == {"libGL.so.1"}


def test_inspect_pure_python_wheel_returns_empty(tmp_path):
    whl = tmp_path / "pure-1.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.writestr("pure/__init__.py", "x = 1\n")
    assert inspect_wheel_sonames(str(whl)) == set()


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_inspect_walks_bundled_libs_transitively_and_excludes_their_names(tmp_path):
    """A soname needed only via a bundled lib surfaces (transitive walk); the
    bundled lib's own versioned/renamed name does NOT leak as a system need."""
    whl = tmp_path / "pkg-1.0-cp311-cp311-manylinux_2_28_x86_64.whl"
    with zipfile.ZipFile(whl, "w") as zf:
        zf.write(
            FIXTURE_DIR / "ext_bundled.cpython-311-x86_64-linux-gnu.so",
            "pkg/ext_bundled.cpython-311-x86_64-linux-gnu.so",
        )
        zf.write(
            FIXTURE_DIR / "libbundled-deadbeef.so.1.2.3",
            "pkg.libs/libbundled-deadbeef.so.1.2.3",
        )
    result = inspect_wheel_sonames(str(whl))
    # libGL.so.1 is reachable ONLY through the bundled lib -> proves transitive walk (Bug 2).
    # libbundled-deadbeef.so.1.2.3 is bundled -> must be subtracted (Bug 1, versioned suffix).
    # libc.so.6 is a base-image lib -> filtered.
    assert result == {"libGL.so.1"}
    assert "libbundled-deadbeef.so.1.2.3" not in result


def test_inspect_bad_wheel_returns_empty(tmp_path):
    bad = tmp_path / "bad.whl"
    bad.write_text("not a zip")
    assert inspect_wheel_sonames(str(bad)) == set()


def test_download_returns_path_when_pip_succeeds(tmp_path):
    (tmp_path / "opencv_python-1-cp311-cp311-manylinux_2_28_x86_64.whl").write_bytes(b"x")
    got = download_target_wheel(
        "opencv-python", "4.9.0.80",
        platform_tag="manylinux_2_28_x86_64", py_version="3.11", abi="cp311",
        dest=str(tmp_path), executor=_Exec(rc=0),
    )
    assert got is not None and got.endswith(".whl")


def test_download_returns_none_when_pip_fails(tmp_path):
    got = download_target_wheel(
        "psycopg2", "2.9.12",
        platform_tag="manylinux_2_28_x86_64", py_version="3.11", abi="cp311",
        dest=str(tmp_path), executor=_Exec(rc=1),
    )
    assert got is None


def test_download_returns_none_when_no_wheel_present(tmp_path):
    # pip "succeeds" but writes nothing (e.g. already-satisfied edge case).
    got = download_target_wheel(
        "empty", "1.0",
        platform_tag="manylinux_2_28_x86_64", py_version="3.11", abi="cp311",
        dest=str(tmp_path), executor=_Exec(rc=0),
    )
    assert got is None


def test_base_image_sonames_includes_cpython_guaranteed_libs():
    from python_deps.depgraph.wheel_inspect import _BASE_IMAGE_SONAMES
    assert "libz.so.1" in _BASE_IMAGE_SONAMES  # zlib: required by CPython, present in any base image
