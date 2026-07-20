"""Curated, stratified corpus of native PyPI packages (design §8).

Pure-wheel packages carry almost no signal for system-dep detection; the meat is
native/sdist packages that genuinely need a `-dev`. `import_name` is explicit
because dist name != import name (mysqlclient->MySQLdb); "" means no importable
module (uWSGI builds a binary — gated by its tail_snippet instead).
"""
from __future__ import annotations

from dataclasses import dataclass

STRATA: frozenset[str] = frozenset({"S1", "S2", "S3", "S4", "S5", "S6", "branch"})


@dataclass(frozen=True)
class PackageSpec:
    name: str
    version: str
    stratum: str
    modes: tuple[str, ...]
    import_name: str            # "" => no importable module (binary-only, e.g. uWSGI)
    tail_snippet: str | None = None  # shell cmd run in-container; exit 0 == ok


_BOTH = ("natural", "forced_sdist")

CORPUS: tuple[PackageSpec, ...] = (
    # S1 — one clear required -dev
    PackageSpec("psycopg2", "2.9.9", "S1", _BOTH, "psycopg2"),
    PackageSpec("mysqlclient", "2.2.4", "S1", _BOTH, "MySQLdb"),
    PackageSpec("pyodbc", "5.1.0", "S1", _BOTH, "pyodbc"),
    PackageSpec("pyaudio", "0.2.14", "S1", _BOTH, "pyaudio"),
    PackageSpec("python-ldap", "3.4.4", "S1", _BOTH, "ldap"),
    PackageSpec("pygraphviz", "1.12", "S1", _BOTH, "pygraphviz"),
    PackageSpec("pycairo", "1.26.0", "S1", _BOTH, "cairo"),
    PackageSpec("pyzmq", "26.0.3", "S1", _BOTH, "zmq"),
    PackageSpec("shapely", "2.0.4", "S1", _BOTH, "shapely"),
    PackageSpec("pyproj", "3.6.1", "S1", _BOTH, "pyproj"),
    # S2 — multi-backend / flavor
    PackageSpec("pycurl", "7.45.3", "S2", _BOTH, "pycurl"),
    PackageSpec("cryptography", "42.0.8", "S2", _BOTH, "cryptography"),
    # S3 — broad / kitchen-sink (binary, no import; gated by --version)
    PackageSpec("uWSGI", "2.0.25.1", "S3", ("forced_sdist",), "",
                tail_snippet="uwsgi --version"),
    # S4 — dlopen tail (snippet exercises the native path)
    PackageSpec("GDAL", "3.8.4", "S4", ("forced_sdist",), "osgeo",
                tail_snippet='python3 -c "from osgeo import gdal; gdal.GetDriverByName(\'GTiff\')"'),
    PackageSpec("h5py", "3.11.0", "S4", _BOTH, "h5py",
                tail_snippet='python3 -c "import h5py, io; h5py.File(io.BytesIO(), \'w\')"'),
    # S5 — sdist but ZERO syslib (must NOT over-predict)
    PackageSpec("regex", "2024.5.15", "S5", ("forced_sdist",), "regex"),
    PackageSpec("ujson", "5.10.0", "S5", ("forced_sdist",), "ujson"),
    # S6 — pure-wheel negative controls (S* == {} both modes)
    PackageSpec("requests", "2.32.3", "S6", _BOTH, "requests"),
    PackageSpec("flask", "3.0.3", "S6", _BOTH, "flask"),
    PackageSpec("click", "8.1.7", "S6", _BOTH, "click"),
    # branch-oracle controls — wheel in natural, syslib under forced_sdist
    PackageSpec("Pillow", "10.3.0", "branch", _BOTH, "PIL"),
    PackageSpec("lxml", "5.2.2", "branch", _BOTH, "lxml"),
    # === v2 vetted additions (2026-07-05, python:3.11-slim-bookworm / linux-amd64).
    # Each was docker-vetted (curation.py): installs + gates with its maximal apt
    # set (build-essential seeded for forced_sdist). Modes reflect what PASSED —
    # a mode dropped here failed the from-source build on bookworm (version skew,
    # not a detection gap): pymssql/confluent-kafka are natural-only (their
    # forced_sdist build failed), and rasterio/netCDF4 were dropped entirely
    # (GDAL/HDF5 from-source skew; GDAL/h5py already cover those patterns). ===
    # S1 — one clear / multi -dev
    PackageSpec('cffi', '1.16.0', 'S1', _BOTH, 'cffi'),
    PackageSpec('gmpy2', '2.1.5', 'S1', ('forced_sdist',), 'gmpy2'),
    PackageSpec('python-snappy', '0.7.1', 'S1', ('forced_sdist',), 'snappy'),
    PackageSpec('python-lzo', '1.15', 'S1', ('forced_sdist',), 'lzo'),
    PackageSpec('pymssql', '2.3.0', 'S1', ('natural',), 'pymssql'),
    PackageSpec('confluent-kafka', '2.4.0', 'S1', ('natural',), 'confluent_kafka'),
    PackageSpec('pycups', '2.0.4', 'S1', ('forced_sdist',), 'cups'),
    PackageSpec('PyICU', '2.13.1', 'S1', ('forced_sdist',), 'icu'),
    PackageSpec('dbus-python', '1.3.2', 'S1', ('forced_sdist',), 'dbus'),
    # S2 — multi-backend / flavor / tool
    PackageSpec('M2Crypto', '0.41.0', 'S2', ('forced_sdist',), 'M2Crypto'),
    # S4 — runtime dlopen tail
    PackageSpec('python-magic', '0.4.27', 'S4', ('natural',), 'magic',
                tail_snippet='python3 -c "import magic; magic.Magic()"'),
    PackageSpec('soundfile', '0.12.1', 'S4', ('natural',), 'soundfile',
                tail_snippet='python3 -c "import soundfile; soundfile.available_formats()"'),
    # S5 — sdist, zero syslib (must NOT over-predict)
    PackageSpec('msgpack', '1.0.8', 'S5', ('forced_sdist',), 'msgpack'),
    PackageSpec('simplejson', '3.19.2', 'S5', ('forced_sdist',), 'simplejson'),
    PackageSpec('xxhash', '3.4.1', 'S5', ('forced_sdist',), 'xxhash'),
    PackageSpec('wrapt', '1.16.0', 'S5', ('forced_sdist',), 'wrapt'),
    PackageSpec('psutil', '5.9.8', 'S5', ('forced_sdist',), 'psutil'),
    PackageSpec('MarkupSafe', '2.1.5', 'S5', ('forced_sdist',), 'markupsafe'),
    # S6 — pure-wheel controls
    PackageSpec('jinja2', '3.1.4', 'S6', _BOTH, 'jinja2'),
    PackageSpec('rich', '13.7.1', 'S6', _BOTH, 'rich'),
    PackageSpec('python-dateutil', '2.9.0.post0', 'S6', _BOTH, 'dateutil'),
    # branch — big wheel natural / builds sdist
    PackageSpec('PyYAML', '6.0.1', 'branch', _BOTH, 'yaml'),
    PackageSpec('numpy', '1.26.4', 'branch', ('natural',), 'numpy'),
    PackageSpec('pandas', '2.2.2', 'branch', ('natural',), 'pandas'),
    PackageSpec('scipy', '1.13.1', 'branch', ('natural',), 'scipy'),
)


def select_corpus(only: frozenset[str] = frozenset(),
                  strata: frozenset[str] = frozenset()) -> list[PackageSpec]:
    """Filter CORPUS by package name (--only) and/or stratum (--stratum).

    Empty sets = no filter on that axis. Raises ValueError on an unknown stratum
    (fail-fast on a typo) or an --only name absent from the corpus.
    """
    if strata - STRATA:
        raise ValueError(f"unknown stratum(s): {sorted(strata - STRATA)}; valid={sorted(STRATA)}")
    names = {s.name for s in CORPUS}
    if only - names:
        raise ValueError(f"unknown --only package(s): {sorted(only - names)}")
    return [s for s in CORPUS
            if (not only or s.name in only) and (not strata or s.stratum in strata)]
