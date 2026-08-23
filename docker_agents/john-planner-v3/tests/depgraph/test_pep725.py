from python_deps.depgraph import pep725
from python_deps.depgraph.pep725 import (
    needs_from_pyproject,
    parse_depurl,
    parse_external_table,
)
from python_deps.depgraph.os_resolver import capability_id

# PEP 725 §Examples — cryptography 39.0 [external] table (verbatim DepURLs).
CRYPTOGRAPHY = """
[build-system]
requires = ["setuptools"]

[external]
build-requires = [
  "dep:virtual/compiler/c",
  "dep:virtual/compiler/rust",
  "dep:generic/pkg-config",
]
host-requires = [
  "dep:generic/openssl",
  "dep:generic/libffi",
]
"""


def test_no_external_table_returns_empty():
    assert needs_from_pyproject("[project]\nname = 'x'\n") == []
    assert parse_external_table("[project]\nname = 'x'\n") == []


def test_malformed_toml_returns_empty():
    assert needs_from_pyproject("this is : not = toml [[[") == []
    assert parse_external_table("this is : not = toml [[[") == []


def test_cryptography_maps_generics_and_skips_virtual():
    needs = needs_from_pyproject(CRYPTOGRAPHY, source="cryptography")
    caps = {(n.kind, n.name) for n in needs}
    assert caps == {
        ("binary", "pkg-config"),
        ("header", "openssl/ssl.h"),
        ("header", "ffi.h"),
    }
    # virtual/compiler/* dropped (toolchain, not a single -dev lib)
    assert not any(n.name in {"c", "rust"} for n in needs)
    # shared-contract build-tier metadata + provenance
    assert all(n.context == "build" and n.strength == "curated" for n in needs)
    assert all(n.evidence == "pep725:cryptography" for n in needs)


def test_parse_depurl_strips_version_qualifiers_subpath_and_scheme():
    assert parse_depurl("dep:generic/openjpeg@>=2.0") == ("generic", "", "openjpeg")
    assert parse_depurl("dep:generic/openssl?x=1#sub") == ("generic", "", "openssl")
    assert parse_depurl("dep:virtual/compiler/c") == ("virtual", "compiler", "c")
    assert parse_depurl("pkg:generic/zlib") == ("generic", "", "zlib")   # PURL alias
    assert parse_depurl("not-a-depurl") is None
    assert parse_depurl("dep:generic") is None                          # too short


def test_unknown_generic_is_skipped_not_fabricated():
    doc = '[external]\nhost-requires = ["dep:generic/totally-unknown-lib"]\n'
    assert needs_from_pyproject(doc) == []


def test_non_generic_purl_types_are_skipped():
    doc = (
        "[external]\n"
        'build-requires = ["dep:cargo/ripgrep", "dep:pypi/numpy", '
        '"dep:golang/github.com/junegunn/fzf"]\n'
    )
    assert needs_from_pyproject(doc) == []


def test_marker_excluding_linux_drops_dep():
    doc = (
        "[external]\n"
        "host-requires = [\"dep:generic/openssl; platform_system=='Windows'\"]\n"
    )
    assert needs_from_pyproject(doc) == []


def test_marker_including_linux_keeps_dep():
    doc = (
        "[external]\n"
        "host-requires = [\"dep:generic/openssl; platform_system=='Linux'\"]\n"
    )
    needs = needs_from_pyproject(doc)
    assert [(n.kind, n.name) for n in needs] == [("header", "openssl/ssl.h")]


def test_dedup_by_capability_id():
    # generic/glib and generic/glib-2.0 map to the SAME capability (pkgconfig glib-2.0).
    doc = (
        "[external]\n"
        'build-requires = ["dep:generic/glib"]\n'
        'host-requires = ["dep:generic/glib-2.0"]\n'
    )
    needs = needs_from_pyproject(doc)
    assert [(n.kind, n.name) for n in needs] == [("pkgconfig", "glib-2.0")]
    assert len({capability_id(n) for n in needs}) == 1


import io
import tarfile
import zipfile

from python_deps.depgraph.pep725 import (
    fetch_sdist_pyproject,
    pep725_external,
    read_sdist_archive,
)

# PEP 725 §Examples — Pillow 10.1.0 [external] table.
PILLOW_EXTERNAL = """
[external]
build-requires = ["dep:virtual/compiler/c"]
host-requires = ["dep:generic/libjpeg", "dep:generic/zlib"]
"""


def _write_targz(path, arcname, text):
    with tarfile.open(path, "w:gz") as tf:
        data = text.encode("utf-8")
        info = tarfile.TarInfo(arcname)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))


def _write_zip(path, arcname, text):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(arcname, text)


def test_read_sdist_archive_targz(tmp_path):
    p = tmp_path / "Pillow-10.1.0.tar.gz"
    _write_targz(str(p), "Pillow-10.1.0/pyproject.toml", PILLOW_EXTERNAL)
    assert "host-requires" in read_sdist_archive(str(p))


def test_read_sdist_archive_zip(tmp_path):
    p = tmp_path / "pkg-1.0.zip"
    _write_zip(str(p), "pkg-1.0/pyproject.toml", PILLOW_EXTERNAL)
    assert "libjpeg" in read_sdist_archive(str(p))


def test_read_sdist_archive_no_pyproject_returns_none(tmp_path):
    p = tmp_path / "legacy-1.0.tar.gz"
    _write_targz(str(p), "legacy-1.0/setup.py", "from setuptools import setup")
    assert read_sdist_archive(str(p)) is None


def test_read_sdist_archive_corrupt_returns_none(tmp_path):
    p = tmp_path / "broken.tar.gz"
    p.write_bytes(b"not a real archive")
    assert read_sdist_archive(str(p)) is None


def test_fetch_uses_no_binary_sdist_flags(fake_executor, make_result_fixture):
    fake_executor.responses = {"pip download": make_result_fixture(stdout="ok")}
    # No real file lands in the throwaway temp dir -> returns None, but the pip
    # command shape is asserted (sdist-forcing flags + pinned spec).
    assert fetch_sdist_pyproject("cryptography", "42.0.0", fake_executor) is None
    cmd = fake_executor.calls[0]
    assert "--no-deps" in cmd
    assert "--no-binary :all:" in cmd
    assert "cryptography==42.0.0" in cmd


def test_fetch_download_failure_returns_none(fake_executor, make_result_fixture):
    fake_executor.responses = {"pip download": make_result_fixture(returncode=1)}
    assert fetch_sdist_pyproject("x", None, fake_executor) is None


def test_fetch_executor_exception_returns_none():
    class Boom:
        def run(self, command, *, timeout=300):
            raise RuntimeError("no network")

    assert fetch_sdist_pyproject("x", "1.0", Boom()) is None


def test_pep725_external_end_to_end(monkeypatch):
    monkeypatch.setattr(pep725, "fetch_sdist_pyproject", lambda *a, **k: PILLOW_EXTERNAL)
    needs = pep725_external("Pillow", "10.1.0", object())
    assert {(n.kind, n.name) for n in needs} == {
        ("header", "jpeglib.h"),
        ("header", "zlib.h"),
    }
    assert all(n.context == "build" and n.strength == "curated" for n in needs)
    assert all(n.evidence == "pep725:pillow" for n in needs)  # normalized source


def test_pep725_external_no_external_returns_empty(monkeypatch):
    monkeypatch.setattr(
        pep725, "fetch_sdist_pyproject", lambda *a, **k: "[project]\nname = 'x'\n"
    )
    assert pep725_external("x", None, object()) == []


def test_pep725_external_fetch_none_returns_empty(monkeypatch):
    monkeypatch.setattr(pep725, "fetch_sdist_pyproject", lambda *a, **k: None)
    assert pep725_external("x", None, object()) == []


def test_pep725_external_logs_present(monkeypatch, caplog):
    import logging as _log
    monkeypatch.setattr(pep725, "fetch_sdist_pyproject", lambda *a, **k: PILLOW_EXTERNAL)
    with caplog.at_level(_log.INFO, logger="python_deps.depgraph.pep725"):
        pep725_external("Pillow", "10.1.0", object())
    line = next(r.getMessage() for r in caplog.records if "pep725: pillow" in r.getMessage())
    assert "external=present" in line and "needs=2" in line


def test_pep725_external_absent_no_info(monkeypatch, caplog):
    import logging as _log
    monkeypatch.setattr(pep725, "fetch_sdist_pyproject", lambda *a, **k: "[project]\nname = 'x'\n")
    with caplog.at_level(_log.INFO, logger="python_deps.depgraph.pep725"):
        pep725_external("x", None, object())
    assert not any("external=present" in r.getMessage() for r in caplog.records)
