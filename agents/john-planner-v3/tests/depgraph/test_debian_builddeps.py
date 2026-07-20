# tests/depgraph/test_debian_builddeps.py
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from conftest import FakeExecutor, make_result  # type: ignore  # noqa: E402

from python_deps.depgraph import debian_builddeps as m  # noqa: E402


def test_ensure_deb_src_patches_and_updates_when_not_enabled():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=1),          # not yet enabled
            "sed -i": make_result(returncode=0),
            "apt-get update": make_result(returncode=0),
        }
    )
    assert m.ensure_deb_src(ex) is True
    joined = "\n".join(ex.calls)
    assert "sed -i 's/^Types: deb$/Types: deb deb-src/'" in joined
    assert "/etc/apt/sources.list.d/debian.sources" in joined
    assert "apt-get update" in joined


def test_ensure_deb_src_idempotent_skips_sed_and_update_when_already_enabled():
    ex = FakeExecutor(responses={"grep -q": make_result(returncode=0)})
    assert m.ensure_deb_src(ex) is True
    # Fast path: only the grep probe ran — no re-patch, no re-update.
    assert len(ex.calls) == 1
    assert not any("sed -i" in c for c in ex.calls)
    assert not any("apt-get update" in c for c in ex.calls)


def test_ensure_deb_src_false_when_sed_fails_non_debian():
    ex = FakeExecutor(
        responses={"grep -q": make_result(returncode=1),
                   "sed -i": make_result(returncode=1)}  # no such file
    )
    assert m.ensure_deb_src(ex) is False


def test_ensure_deb_src_false_when_update_fails():
    ex = FakeExecutor(
        responses={"grep -q": make_result(returncode=1),
                   "sed -i": make_result(returncode=0),
                   "apt-get update": make_result(returncode=1)}
    )
    assert m.ensure_deb_src(ex) is False


_PSYCOPG2_SHOWSRC = """Package: psycopg2
Binary: python3-psycopg2
Architecture: any
Version: 2.9.5-1
Build-Depends: debhelper-compat (= 13), dh-python, python3-all-dev, libpq-dev, postgresql-server-dev-all
Format: 3.0 (quilt)
"""


def test_parse_build_depends_splits_and_takes_bare_names():
    assert m.parse_build_depends(_PSYCOPG2_SHOWSRC) == [
        "debhelper-compat", "dh-python", "python3-all-dev",
        "libpq-dev", "postgresql-server-dev-all",
    ]


def test_parse_build_depends_first_alternative_and_strips_qualifiers():
    text = (
        "Build-Depends: libssl-dev | libssl1.0-dev (>= 1.0), "
        "libfoo-dev [amd64], libbar-dev <!nocheck>, python3-sphinx <!nodoc>\n"
    )
    assert m.parse_build_depends(text) == [
        "libssl-dev", "libfoo-dev", "libbar-dev", "python3-sphinx",
    ]


def test_parse_build_depends_multiline_continuation():
    text = (
        "Build-Depends: debhelper-compat (= 13),\n"
        " libgeos-dev,\n"
        "\tlibgdal-dev\n"
        "Build-Depends-Indep: python3-sphinx\n"
    )
    assert m.parse_build_depends(text) == ["debhelper-compat", "libgeos-dev", "libgdal-dev"]


def test_parse_build_depends_absent_returns_empty():
    assert m.parse_build_depends("Package: foo\nArchitecture: any\n") == []


def test_is_machinery_drops_packaging_cruft():
    for token in ("debhelper-compat", "dh-python", "dh-sequence-python3",
                  "python3-all-dev", "python3-setuptools", "dpkg-dev",
                  "libjs-sphinxdoc", "libpython3-dev", "cython3"):
        assert m.is_machinery(token) is True


def test_is_system_lib_keeps_non_machinery():
    # system libs
    assert m.is_system_lib("libpq-dev") is True
    assert m.is_system_lib("portaudio19-dev") is True
    assert m.is_system_lib("libgeos-dev") is True
    # real build TOOLS the old -dev/lib* gate dropped
    assert m.is_system_lib("swig") is True
    assert m.is_system_lib("cargo") is True
    assert m.is_system_lib("rustc") is True
    assert m.is_system_lib("proj-bin") is True
    assert m.is_system_lib("pkg-config") is True
    assert m.is_system_lib("postgresql-server-dev-all") is True
    # packaging machinery still dropped
    assert m.is_system_lib("python3-all-dev") is False
    assert m.is_system_lib("libpython3-dev") is False
    assert m.is_system_lib("libjs-sphinxdoc") is False
    assert m.is_system_lib("debhelper-compat") is False
    # librust-* vendored crate shadows are noise, not system build-deps
    assert m.is_system_lib("librust-openssl-sys-dev") is False


def test_source_candidates_order_and_dedup():
    # normalized "pyaudio" starts with "py" -> bare stem "audio"; python-<x> variant.
    assert m.source_candidates("PyAudio") == ["pyaudio", "python-pyaudio", "audio"]


def test_source_candidates_alias_is_first():
    # mysqlclient's Debian source is python-mysqldb (neither normalized nor python-<x>).
    cands = m.source_candidates("mysqlclient")
    assert cands[0] == "python-mysqldb"
    assert "mysqlclient" in cands and "python-mysqlclient" in cands


def test_source_candidates_no_py_prefix_no_bare_stem_dup():
    # "psycopg2" has no python-/py prefix -> bare stem == normalized -> deduped out.
    assert m.source_candidates("psycopg2") == ["psycopg2", "python-psycopg2"]


_LISP_CFFI = (
    "Package: cffi\n"
    "Binary: cl-cffi, cl-cffi-doc\n"
    "Build-Depends: debhelper-compat (= 13), sbcl, texlive, python3-sphinx\n"
)
_PY_CFFI = (
    "Package: python-cffi\n"
    "Binary: python3-cffi, python3-cffi-backend\n"
    "Build-Depends: debhelper-compat (= 13), dh-python, python3-all-dev, libffi-dev\n"
)


def test_builds_python3_binary_checks_binary_field_not_build_depends():
    assert m._builds_python3_binary(_PY_CFFI) is True
    # Lisp cffi: Binary is cl-cffi (no python3-*), even though Build-Depends has
    # python3-sphinx — must be False (proves it reads Binary:, not Build-Depends:).
    assert m._builds_python3_binary(_LISP_CFFI) is False
    assert m._builds_python3_binary("Package: x\nBuild-Depends: libfoo-dev\n") is False


def test_resolve_source_skips_non_python_source(monkeypatch):
    ex = FakeExecutor(responses={
        "grep -q": make_result(),                              # ensure_deb_src -> True
        "apt-cache showsrc cffi": make_result(stdout=_LISP_CFFI),
        "apt-cache showsrc python-cffi": make_result(stdout=_PY_CFFI),
    })
    resolved = m._resolve_source("cffi", ex)
    assert resolved is not None
    assert resolved[0] == "python-cffi"          # skipped the Lisp cffi match
    assert "libffi-dev" in resolved[1]


def _showsrc_stanza(package: str, build_depends: str) -> str:
    return (
        f"Package: {package}\n"
        f"Binary: python3-{package}\n"
        f"Architecture: any\n"
        f"Build-Depends: {build_depends}\n"
    )


def test_pypi_to_debian_source_resolves_via_normalized_name():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),  # deb-src already enabled
            "showsrc psycopg2": make_result(
                stdout=_showsrc_stanza("psycopg2", "libpq-dev")),
        }
    )
    assert m.pypi_to_debian_source("psycopg2", ex) == "psycopg2"


def test_pypi_to_debian_source_resolves_via_python_prefix():
    # normalized "pyaudio" misses (default 127); python-pyaudio has Build-Depends.
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc python-pyaudio": make_result(
                stdout=_showsrc_stanza("python-pyaudio", "portaudio19-dev")),
        }
    )
    assert m.pypi_to_debian_source("pyaudio", ex) == "python-pyaudio"


def test_pypi_to_debian_source_alias_precedence():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc python-mysqldb": make_result(
                stdout=_showsrc_stanza("python-mysqldb", "default-libmysqlclient-dev")),
        }
    )
    assert m.pypi_to_debian_source("mysqlclient", ex) == "python-mysqldb"


def test_pypi_to_debian_source_none_when_all_candidates_miss():
    # deb-src enabled, but every showsrc misses (default 127 -> not ok).
    ex = FakeExecutor(responses={"grep -q": make_result(returncode=0)})
    assert m.pypi_to_debian_source("nonexistentpkg", ex) is None


def test_pypi_to_debian_source_none_when_deb_src_cannot_enable():
    ex = FakeExecutor(
        responses={"grep -q": make_result(returncode=1),
                   "sed -i": make_result(returncode=1)}  # non-Debian
    )
    assert m.pypi_to_debian_source("psycopg2", ex) is None


def test_pypi_to_debian_source_ignores_source_without_build_depends():
    # showsrc returns rc0 but the stanza has NO Build-Depends -> not a match.
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc psycopg2": make_result(stdout="Package: psycopg2\nArchitecture: any\n"),
        }
    )
    assert m.pypi_to_debian_source("psycopg2", ex) is None


_UWSGI_BDEPS = (
    "debhelper-compat (= 13), libpcre2-dev, libz-dev, libcap-dev, "
    "libjansson-dev, libyaml-dev, libsqlite3-dev, python3-dev, libxml2-dev, "
    "libgeoip-dev, libldap2-dev, libpq-dev"
)


def test_debian_build_deps_returns_apt_names_dropping_machinery():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc psycopg2": make_result(
                stdout=_showsrc_stanza(
                    "psycopg2",
                    "debhelper-compat (= 13), dh-python, python3-all-dev, "
                    "libpq-dev, postgresql-server-dev-all")),
        }
    )
    # machinery (debhelper-compat, dh-python, python3-all-dev) dropped;
    # libpq-dev and postgresql-server-dev-all (a real build tool, not machinery) kept.
    assert m.debian_build_deps("psycopg2", ex) == ["libpq-dev", "postgresql-server-dev-all"]


def test_debian_build_deps_broad_returns_all_system_libs():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc uwsgi": make_result(stdout=_showsrc_stanza("uwsgi", _UWSGI_BDEPS)),
        }
    )
    names = m.debian_build_deps("uwsgi", ex)
    # machinery (debhelper-compat, python3-dev) dropped; 10 system libs kept, in order.
    assert "python3-dev" not in names and "debhelper-compat" not in names
    assert names == [
        "libpcre2-dev", "libz-dev", "libcap-dev", "libjansson-dev", "libyaml-dev",
        "libsqlite3-dev", "libxml2-dev", "libgeoip-dev", "libldap2-dev", "libpq-dev",
    ]


def test_debian_build_deps_empty_when_no_source():
    ex = FakeExecutor(responses={"grep -q": make_result(returncode=0)})
    assert m.debian_build_deps("nonexistentpkg", ex) == []


def test_debian_build_deps_empty_when_only_machinery_kept():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc widgetlib": make_result(
                stdout=_showsrc_stanza("widgetlib", "debhelper-compat (= 13), dh-python")),
        }
    )
    assert m.debian_build_deps("widgetlib", ex) == []


def test_debian_build_deps_dedupes_repeated_tokens():
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "showsrc dupe": make_result(
                stdout=_showsrc_stanza("dupe", "libpq-dev, libpq-dev, libssl-dev")),
        }
    )
    assert m.debian_build_deps("dupe", ex) == ["libpq-dev", "libssl-dev"]


def test_debian_build_deps_logs_source_and_deps(caplog):
    import logging as _log
    ex = FakeExecutor(responses={
        "grep -q": make_result(returncode=0),
        "showsrc psycopg2": make_result(stdout=_showsrc_stanza(
            "psycopg2", "debhelper-compat (= 13), dh-python, python3-all-dev, "
            "libpq-dev, postgresql-server-dev-all")),
    })
    with caplog.at_level(_log.INFO, logger="python_deps.depgraph.debian_builddeps"):
        m.debian_build_deps("psycopg2", ex)
    line = next(r.getMessage() for r in caplog.records if "debian: psycopg2 ->" in r.getMessage())
    assert "source=psycopg2" in line and "libpq-dev" in line


def test_debian_build_deps_logs_miss(caplog):
    import logging as _log
    ex = FakeExecutor(responses={"grep -q": make_result(returncode=0)})
    with caplog.at_level(_log.INFO, logger="python_deps.depgraph.debian_builddeps"):
        m.debian_build_deps("nonexistentpkg", ex)
    assert any("debian: nonexistentpkg -> MISS" in r.getMessage() for r in caplog.records)


_REPOLOGY_JSON = (
    '[{"repo":"debian_12","srcname":"confluent-kafka-python","visiblename":"x"},'
    ' {"repo":"pypi","srcname":"confluent-kafka"},'
    ' {"repo":"debian_unstable","srcname":"confluent-kafka-python"}]'
)


def test_repology_debian_sources_extracts_debian_srcnames():
    ex = FakeExecutor(responses={"repology.org": make_result(stdout=_REPOLOGY_JSON)})
    assert m.repology_debian_sources("confluent-kafka", ex) == ["confluent-kafka-python"]


def test_repology_debian_sources_empty_on_curl_failure():
    ex = FakeExecutor(responses={"repology.org": make_result(returncode=7)})  # curl fail
    assert m.repology_debian_sources("whatever", ex) == []


def test_repology_debian_sources_empty_on_garbage_json():
    ex = FakeExecutor(responses={"repology.org": make_result(stdout="not json")})
    assert m.repology_debian_sources("whatever", ex) == []


def test_pypi_to_debian_source_falls_back_to_repology():
    # All heuristic candidates miss (default 127); Repology yields the real source,
    # which then verifies via showsrc.
    ex = FakeExecutor(
        responses={
            "grep -q": make_result(returncode=0),
            "repology.org": make_result(stdout=_REPOLOGY_JSON),
            "showsrc confluent-kafka-python": make_result(
                stdout=_showsrc_stanza("confluent-kafka-python", "librdkafka-dev")),
        }
    )
    assert m.pypi_to_debian_source("confluent-kafka", ex) == "confluent-kafka-python"


def test_debian_build_deps_fetches_showsrc_once():
    # The winning source's stanza is fetched once (via _resolve_source) and reused
    # by debian_build_deps — no second apt-cache round-trip.
    ex = FakeExecutor(responses={
        "grep -q": make_result(returncode=0),
        "showsrc psycopg2": make_result(stdout=_showsrc_stanza(
            "psycopg2", "debhelper-compat (= 13), libpq-dev")),
    })
    assert m.debian_build_deps("psycopg2", ex) == ["libpq-dev"]
    showsrc_calls = [c for c in ex.calls if "apt-cache showsrc psycopg2" in c]
    assert len(showsrc_calls) == 1  # was 2 before the _resolve_source refactor
