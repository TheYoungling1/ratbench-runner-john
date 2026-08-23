"""Pure failure-signature extraction: stderr -> ObservedNeed list.

Table-independence is the whole point: names NOT in PROVIDER_TABLE still extract.
The false-positive galleries below are regression fixtures — every "must NOT
match" line is an assertion.
"""
from __future__ import annotations

from python_deps.depgraph.failure_signatures import extract_needs


def _kn(needs):
    """(kind, name) pairs, order-preserving — the extractor's observable output."""
    return [(n.kind, n.name) for n in needs]


# ── header ────────────────────────────────────────────────────────────────────
def test_header_gcc_bare():
    needs = extract_needs("fatal error: portaudio.h: No such file or directory")
    assert _kn(needs) == [("header", "portaudio.h")]
    assert needs[0].context == "build"
    assert "portaudio.h" in needs[0].evidence


def test_header_gcc_legacy_no_fatal_prefix():
    assert _kn(extract_needs("sqlite3.h: No such file or directory")) == [("header", "sqlite3.h")]


def test_header_clang_quoted():
    assert _kn(extract_needs("fatal error: 'openssl/ssl.h' file not found")) == [
        ("header", "openssl/ssl.h")
    ]


def test_header_clang_driver_no_fatal():
    assert _kn(extract_needs("'zlib.h' file not found")) == [("header", "zlib.h")]


def test_header_slashed_kept_as_printed():
    # Slashed form preserved (matches check_command_for's -path branch); NOT basenamed.
    assert _kn(extract_needs("fatal error: openssl/ssl.h: No such file or directory")) == [
        ("header", "openssl/ssl.h")
    ]


def test_header_absolute_path_is_basenamed():
    assert _kn(extract_needs("/build/tmp/foo.h: No such file or directory")) == [
        ("header", "foo.h")
    ]


def test_header_extension_gate_rejects_source_and_data_enoent():
    # ENOENT on .c / .so / data files is NOT a header gap.
    for line in (
        "main.c: No such file or directory",
        "libGL.so: No such file or directory",
        "config.yaml: No such file or directory",
        "Makefile: No such file or directory",
    ):
        assert extract_needs(line) == [], line


def test_header_include_trace_is_not_a_gap():
    # -H header-trace lines and #include echoes lack the not-found phrase.
    assert extract_needs(". /usr/include/stdio.h") == []
    assert extract_needs('#include "foo.h"') == []


def test_header_unknown_name_still_extracted():
    # Table-independence: hiredis/hiredis.h is not in PROVIDER_TABLE.
    assert _kn(extract_needs("fatal error: hiredis/hiredis.h: No such file or directory")) == [
        ("header", "hiredis/hiredis.h")
    ]


# ── binary ────────────────────────────────────────────────────────────────────
def test_binary_command_not_found():
    assert _kn(extract_needs("bash: swig: command not found")) == [("binary", "swig")]


def test_binary_dash_numbered_not_found():
    assert _kn(extract_needs("/bin/sh: 1: pg_config: not found")) == [("binary", "pg_config")]


def test_binary_setuptools_executable_not_found():
    assert _kn(extract_needs("Error: pg_config executable not found.")) == [
        ("binary", "pg_config")
    ]


def test_binary_meson_quoted_executable():
    assert _kn(extract_needs("The 'cmake' executable was not found")) == [("binary", "cmake")]


def test_binary_autoconf_cannot_find():
    assert _kn(extract_needs("configure: error: Cannot find swig")) == [("binary", "swig")]


def test_binary_autoconf_x_not_found():
    assert _kn(extract_needs("configure: error: pkg-config not found")) == [
        ("binary", "pkg-config")
    ]


def test_binary_autoconf_probe_no():
    assert _kn(extract_needs("checking for gdal-config... no")) == [("binary", "gdal-config")]


def test_binary_which_no():
    assert _kn(extract_needs("which: no llvm-config in (/usr/bin:/bin)")) == [
        ("binary", "llvm-config")
    ]


def test_binary_distutils_errno2_only():
    assert _kn(
        extract_needs("error: command 'gcc' failed: No such file or directory")
    ) == [("binary", "gcc")]


def test_binary_config_script_name_needs_no_special_casing():
    assert _kn(extract_needs("bash: curl-config: command not found")) == [
        ("binary", "curl-config")
    ]


def test_binary_context_hint_flows_through():
    assert extract_needs("bash: swig: command not found", context_hint="runtime")[0].context == "runtime"
    assert extract_needs("bash: swig: command not found", context_hint="build")[0].context == "build"


def test_binary_h_probe_routes_to_header():
    # "checking for X.h... no" is a header probe, not a binary.
    assert _kn(extract_needs("checking for zlib.h... no")) == [("header", "zlib.h")]


# ── binary false positives (load-bearing negatives) ──────────────────────────
def test_binary_probe_success_is_not_a_gap():
    assert extract_needs("checking for gcc... yes") == []


def test_binary_nonzero_exit_is_not_a_missing_tool():
    # A real compile failure, NOT a missing tool (no errno=2 'No such file' tail).
    assert extract_needs("error: command 'gcc' failed with exit status 1") == []


def test_binary_bare_mention_is_not_a_gap():
    # The exact false positive the old _tool_gaps produced.
    assert extract_needs("Using pg_config at /usr/bin/pg_config") == []
    assert extract_needs("running build_ext with gcc") == []


def test_binary_unknown_name_still_extracted():
    # Table-independence: 'gdal' is not in PROVIDER_TABLE.
    assert _kn(extract_needs("bash: gdal: command not found")) == [("binary", "gdal")]


# ── pkgconfig ────────────────────────────────────────────────────────────────
def test_pkgconfig_no_package_quoted():
    assert _kn(extract_needs("No package 'cairo' found")) == [("pkgconfig", "cairo")]


def test_pkgconfig_was_not_found_unquoted_tail():
    assert _kn(
        extract_needs("Package glib-2.0 was not found in the pkg-config search path")
    ) == [("pkgconfig", "glib-2.0")]


def test_pkgconfig_transitive_requires():
    assert _kn(
        extract_needs("Package 'gio-2.0', required by 'gtk+-3.0', not found")
    ) == [("pkgconfig", "gio-2.0")]


def test_pkgconfig_meson_pkgconfig_gated():
    assert _kn(
        extract_needs('Dependency "libudev" not found, tried pkgconfig and cmake')
    ) == [("pkgconfig", "libudev")]


def test_pkgconfig_meson_simple_fallback():
    assert _kn(extract_needs("Dependency 'zlib' not found")) == [("pkgconfig", "zlib")]


def test_pkgconfig_cmake_pkg_check_modules_echo():
    assert _kn(extract_needs("--   No package 'gobject-2.0' found")) == [
        ("pkgconfig", "gobject-2.0")
    ]


def test_pkgconfig_none_of_required_splits_on_semicolon():
    assert _kn(extract_needs("None of the required 'glib-2.0;gobject-2.0' found")) == [
        ("pkgconfig", "glib-2.0"),
        ("pkgconfig", "gobject-2.0"),
    ]


# ── pkgconfig false positives ────────────────────────────────────────────────
def test_pkgconfig_prose_no_package_manager_is_not_a_gap():
    # The reported false positive: optional quotes matched 'manager'. Quotes now required.
    assert extract_needs("No package manager found on this system") == []


def test_pkgconfig_meson_cmake_only_not_emitted():
    # tried-list without pkgconfig -> the name is not a .pc module; skip it.
    assert extract_needs('Dependency "Qt5" not found, tried cmake') == []


def test_pkgconfig_unknown_name_still_extracted():
    assert _kn(extract_needs("No package 'libcamera' found")) == [("pkgconfig", "libcamera")]


# ── soname ───────────────────────────────────────────────────────────────────
def test_soname_import_error():
    assert _kn(
        extract_needs("ImportError: libGL.so.1: cannot open shared object file")
    ) == [("soname", "libGL.so.1")]
    assert extract_needs("ImportError: libGL.so.1: cannot open shared object file")[0].context == "runtime"


def test_soname_all_occurrences_not_just_first():
    text = (
        "ImportError: libGL.so.1: cannot open shared object file\n"
        "ImportError: libSM.so.6: cannot open shared object file\n"
    )
    assert _kn(extract_needs(text)) == [("soname", "libGL.so.1"), ("soname", "libSM.so.6")]


def test_soname_no_prefix_ctypes():
    assert _kn(
        extract_needs("OSError: foobar.so.2: cannot open shared object file")
    ) == [("soname", "foobar.so.2")]


# ── linker_lib ───────────────────────────────────────────────────────────────
def test_linker_lib_gnu_ld():
    assert [(n.kind, n.name) for n in extract_needs("/usr/bin/ld: cannot find -lssl")] == [
        ("linker_lib", "ssl")
    ]


def test_linker_lib_variant_binaries_and_multiple():
    text = (
        "ld.gold: cannot find -ljpeg\n"
        "/usr/bin/ld.bfd: cannot find -lz\n"
    )
    assert [(n.kind, n.name) for n in extract_needs(text)] == [
        ("linker_lib", "jpeg"),
        ("linker_lib", "z"),
    ]
    assert extract_needs("/usr/bin/ld: cannot find -lssl")[0].context == "build"


def test_undefined_reference_is_not_a_linker_lib():
    # A symbol, not a library — detectable-but-unresolvable, must NOT be emitted.
    assert extract_needs("main.o: undefined reference to `SSL_new'") == []


# ── GLIBC / symbol-version mismatch (ABI, not an absent package) ────────────
def test_glibc_version_mismatch_emits_no_soname():
    from python_deps.depgraph.failure_signatures import extract_needs
    text = ("ImportError: /lib/x86_64-linux-gnu/libm.so.6: "
            "version `GLIBC_2.34' not found (required by foo.so)")
    assert extract_needs(text) == []


def test_symbol_lookup_error_emits_nothing():
    from python_deps.depgraph.failure_signatures import extract_needs
    assert extract_needs("symbol lookup error: /app/x.so: undefined symbol: foo") == []


# ── cross-kind ordering + dedup ──────────────────────────────────────────────
def test_dedup_by_kind_and_name_first_occurrence_text_order():
    text = (
        "fatal error: portaudio.h: No such file or directory\n"
        "bash: pg_config: command not found\n"
        "fatal error: portaudio.h: No such file or directory\n"  # dup
    )
    assert _kn(extract_needs(text)) == [
        ("header", "portaudio.h"),
        ("binary", "pg_config"),
    ]
