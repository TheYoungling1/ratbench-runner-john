import dataclasses

import pytest

from python_deps.depgraph.os_resolver import (
    ObservedNeed, ProviderCandidate, PROVIDER_TABLE, default_context,
)

def test_observed_need_defaults():
    need = ObservedNeed(kind="header", name="libpq-fe.h")
    assert need.context == ""
    assert need.strength == "strong"
    assert need.owner_node_id is None

def test_observed_need_rejects_invalid_kind():
    with pytest.raises(ValueError):
        ObservedNeed(kind="framework", name="x")

def test_observed_need_accepts_linker_lib():
    need = ObservedNeed(kind="linker_lib", name="ssl")
    assert need.kind == "linker_lib"

def test_provider_candidate_defaults():
    candidate = ProviderCandidate(manager="apt", package="libpq-dev", source="table")
    assert candidate.manager == "apt"
    assert candidate.package == "libpq-dev"
    assert candidate.source == "table"
    assert candidate.matched_path == ""
    assert candidate.check_command == ""
    assert candidate.score == 0

def test_observed_need_is_frozen():
    need = ObservedNeed(kind="header", name="libpq-fe.h")
    with pytest.raises(dataclasses.FrozenInstanceError):
        need.name = "other.h"

def test_provider_candidate_is_frozen():
    candidate = ProviderCandidate(manager="apt", package="libpq-dev", source="table")
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.package = "other-dev"

def test_default_context_from_kind():
    assert default_context("header") == "build"
    assert default_context("pkgconfig") == "build"
    assert default_context("soname") == "runtime"
    assert default_context("binary") == "unknown"  # binary needs explicit context

def test_provider_table_is_exactly_the_curated_set():
    expected = {
        # runtime sonames (migrated from NATIVE_LIB_TO_APT)
        ("soname", "libGL.so.1"): "libgl1",
        ("soname", "libgthread-2.0.so.0"): "libglib2.0-0",
        ("soname", "libglib-2.0.so.0"): "libglib2.0-0",
        ("soname", "libSM.so.6"): "libsm6",
        ("soname", "libXext.so.6"): "libxext6",
        ("soname", "libXrender.so.1"): "libxrender1",
        ("soname", "libxcb.so.1"): "libxcb1",
        ("soname", "libpq.so.5"): "libpq5",
        ("soname", "libgomp.so.1"): "libgomp1",
        ("soname", "libGLU.so.1"): "libglu1-mesa",
        # build-time tools/headers (migrated from TOOL_TO_APT)
        ("binary", "pg_config"): "libpq-dev",
        ("binary", "mysql_config"): "default-libmysqlclient-dev",
        ("binary", "gcc"): "build-essential",
        ("binary", "g++"): "build-essential",
        ("binary", "make"): "build-essential",
        ("binary", "cc"): "build-essential",
        ("header", "Python.h"): "python3-dev",
        # PoC-verified build-dep additions
        ("header", "libpq-fe.h"): "libpq-dev",
        ("pkgconfig", "cairo"): "libcairo2-dev",
        ("pkgconfig", "glib-2.0"): "libglib2.0-dev",
        ("header", "openssl/ssl.h"): "libssl-dev",
        ("header", "portaudio.h"): "portaudio19-dev",
        # re-added build-dep providers (container-verified bookworm+trixie 2026-07-03)
        ("header", "sql.h"): "unixodbc-dev",
        ("header", "ldap.h"): "libldap-dev",
        ("header", "sasl/sasl.h"): "libsasl2-dev",
        ("binary", "curl-config"): "libcurl4-openssl-dev",
        ("pkgconfig", "dbus-1"): "libdbus-1-dev",
        ("header", "snappy.h"): "libsnappy-dev",
        # the pkg-config binary itself (container-verified bookworm+trixie)
        ("binary", "pkg-config"): "pkgconf",
    }
    assert PROVIDER_TABLE == expected

from python_deps.depgraph.os_resolver import (
    parse_apt_file_rows, filter_by_kind, rank,
)

def test_parse_apt_file_rows():
    out = "libpq-dev: /usr/include/postgresql/libpq-fe.h\npostgresql: /usr/share/doc/x\n"
    assert parse_apt_file_rows(out) == [
        ("libpq-dev", "/usr/include/postgresql/libpq-fe.h"),
        ("postgresql", "/usr/share/doc/x"),
    ]

def test_header_filter_keeps_usr_include_only():
    rows = [
        ("libpq-dev", "/usr/include/postgresql/libpq-fe.h"),
        ("weird", "/usr/share/libpq-fe.h"),
    ]
    need = ObservedNeed(kind="header", name="libpq-fe.h", context="build")
    assert filter_by_kind(rows, need) == [("libpq-dev", "/usr/include/postgresql/libpq-fe.h")]

def test_bare_header_prefers_canonical_over_vendor_copy():
    # sqlite3.h PoC failure: libtbox-dev owns a nested private copy.
    rows = [
        ("libtbox-dev", "/usr/include/tbox/database/impl/sqlite3.h"),
        ("libsqlite3-dev", "/usr/include/sqlite3.h"),
    ]
    need = ObservedNeed(kind="header", name="sqlite3.h", context="build")
    assert rank(filter_by_kind(rows, need), need)[0] == (
        "libsqlite3-dev", "/usr/include/sqlite3.h",
    )

def test_soname_prefers_runtime_over_dev():
    rows = [
        ("libgl1", "/usr/lib/x86_64-linux-gnu/libGL.so.1"),
        ("libgl-dev", "/usr/lib/x86_64-linux-gnu/libGL.so.1"),
    ]
    need = ObservedNeed(kind="soname", name="libGL.so.1", context="runtime")
    assert rank(filter_by_kind(rows, need), need)[0][0] == "libgl1"

def test_build_binary_prefers_dev():
    rows = [
        ("postgresql-common", "/usr/bin/pg_config"),
        ("libpq-dev", "/usr/bin/pg_config"),
    ]
    need = ObservedNeed(kind="binary", name="pg_config", context="build")
    assert rank(filter_by_kind(rows, need), need)[0][0] == "libpq-dev"

from python_deps.depgraph.os_resolver import check_command_for, _candidate

def test_check_command_per_kind():
    assert check_command_for(ObservedNeed("soname", "libGL.so.1")) == "ldconfig -p | grep libGL.so.1"
    assert check_command_for(ObservedNeed("binary", "pg_config", context="build")) == "command -v pg_config"
    assert check_command_for(ObservedNeed("pkgconfig", "cairo")) == "pkg-config --exists cairo"

def test_header_check_prefers_matched_path():
    need = ObservedNeed("header", "libpq-fe.h", context="build")
    assert check_command_for(need, "/usr/include/postgresql/libpq-fe.h") == \
        "test -f /usr/include/postgresql/libpq-fe.h"

def test_header_check_without_path_searches_include_roots():
    need = ObservedNeed("header", "Python.h", context="build")
    cmd = check_command_for(need)
    assert "find" in cmd
    assert "-name" in cmd
    assert "Python.h" in cmd
    assert "sysconfig" not in cmd

def test_header_check_without_path_bare_name_uses_find_name():
    need = ObservedNeed("header", "libpq-fe.h", context="build")
    cmd = check_command_for(need)
    assert "find" in cmd
    assert "-name" in cmd
    assert "libpq-fe.h" in cmd
    assert "sysconfig" not in cmd

def test_header_check_without_path_slashed_name_uses_find_path():
    need = ObservedNeed("header", "openssl/ssl.h", context="build")
    cmd = check_command_for(need)
    assert "find" in cmd
    assert "-path" in cmd
    assert "*/openssl/ssl.h" in cmd

def test_candidate_carries_apt_and_check():
    need = ObservedNeed("binary", "pg_config", context="build")
    cand = _candidate(need, "libpq-dev", source="table")
    assert cand.manager == "apt"
    assert cand.package == "libpq-dev"
    assert cand.source == "table"
    assert cand.check_command == "command -v pg_config"

from python_deps.depgraph.os_resolver import resolve
from python_deps.depgraph.executor import CommandResult

class _Fake:
    """Minimal Executor: returns canned CommandResult by substring match."""
    def __init__(self, responses):
        self.responses = responses
    def run(self, command, timeout=None):
        for needle, result in self.responses.items():
            if needle in command:
                return result
        return CommandResult(returncode=0, stdout="", stderr="", command=command)

def _ok(stdout=""):
    return CommandResult(returncode=0, stdout=stdout, stderr="", command="")

def test_resolve_table_hit_needs_no_executor():
    need = ObservedNeed("binary", "pg_config", context="build")
    cands = resolve(need, executor=None)
    assert [c.package for c in cands] == ["libpq-dev"]
    assert cands[0].source == "table"

def test_resolve_predict_miss_returns_empty_without_executor():
    need = ObservedNeed("header", "unknown-thing.h", context="build")
    assert resolve(need, executor=None) == []

def test_resolve_apt_file_fallback_on_table_miss():
    need = ObservedNeed("header", "vips/vips8.h", context="build")
    fake = _Fake({
        "command -v apt-file": _ok("/usr/bin/apt-file"),
        "apt-file search": _ok("libvips-dev: /usr/include/vips/vips8.h\n"),
    })
    cands = resolve(need, executor=fake)
    assert cands[0].package == "libvips-dev"
    assert cands[0].source == "apt-file"
    assert cands[0].check_command == "test -f /usr/include/vips/vips8.h"

def test_resolve_apt_file_pkgconfig_searches_pc_file(fake_executor, make_result_fixture):
    # Per spec Type-Specific Search: a pkgconfig need searches for the .pc file,
    # not the bare module name.
    need = ObservedNeed("pkgconfig", "foo", context="build")
    fake_executor.responses = {
        "command -v apt-file": make_result_fixture(stdout="/usr/bin/apt-file"),
        "apt-file search foo.pc": make_result_fixture(
            stdout="libfoo-dev: /usr/lib/x86_64-linux-gnu/pkgconfig/foo.pc\n"
        ),
    }
    cands = resolve(need, executor=fake_executor)
    assert "apt-file search foo.pc" in fake_executor.calls
    assert cands[0].package == "libfoo-dev"
    assert cands[0].source == "apt-file"
    assert cands[0].check_command == "pkg-config --exists foo"

def test_resolve_no_candidate_returns_empty():
    need = ObservedNeed("header", "nope.h", context="build")
    fake = _Fake({
        "command -v apt-file": _ok("/usr/bin/apt-file"),
        "apt-file search": _ok(""),
    })
    assert resolve(need, executor=fake) == []

def test_resolve_table_hit_does_not_touch_executor():
    class _Boom:
        def run(self, command, timeout=None):
            raise AssertionError(f"executor must not be called on a table hit: {command!r}")
    need = ObservedNeed("binary", "pg_config", context="build")
    cands = resolve(need, executor=_Boom())
    assert [c.package for c in cands] == ["libpq-dev"]
    assert cands[0].source == "table"

def test_resolve_apt_file_search_failure_returns_empty():
    need = ObservedNeed("header", "vips/vips8.h", context="build")
    fake = _Fake({
        "command -v apt-file": _ok("/usr/bin/apt-file"),
        "apt-file search": CommandResult(returncode=1, stdout="", stderr="apt-file: command failed", command=""),
    })
    assert resolve(need, executor=fake) == []

def test_default_context_linker_lib_is_build():
    from python_deps.depgraph.os_resolver import default_context
    assert default_context("linker_lib") == "build"

def test_resolve_linker_lib_prefers_dev_via_unversioned_so(fake_executor, make_result_fixture):
    from python_deps.depgraph.os_resolver import ObservedNeed, resolve
    need = ObservedNeed(kind="linker_lib", name="ssl", context="build")
    fake_executor.responses = {
        "command -v apt-file": make_result_fixture(stdout="/usr/bin/apt-file"),
        "apt-file search libssl.so": make_result_fixture(
            stdout=(
                "libssl-dev: /usr/lib/x86_64-linux-gnu/libssl.so\n"
                "libssl3: /usr/lib/x86_64-linux-gnu/libssl.so.3\n"
            )
        ),
    }
    cands = resolve(need, fake_executor)
    assert cands, "linker_lib must resolve via the unversioned .so"
    assert cands[0].package == "libssl-dev"  # -dev preferred; libssl.so.3 filtered out

def test_check_command_for_linker_lib_is_find_not_ldconfig():
    from python_deps.depgraph.os_resolver import ObservedNeed, check_command_for
    cmd = check_command_for(ObservedNeed(kind="linker_lib", name="ssl", context="build"))
    assert "find" in cmd and "libssl.so" in cmd
    assert "ldconfig" not in cmd  # dev symlinks lack a SONAME; not in the linker cache
