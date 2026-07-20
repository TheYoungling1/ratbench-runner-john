"""Corpus curation harness (design §4): empirically vet candidate native packages
before they enter CORPUS. Each candidate carries a curated *maximal* apt set; the
harness installs it in a FRESH amd64 bookworm container and runs the real gate
(forced_sdist builds from source via --no-binary <name>). Only candidates that PASS
should be promoted into corpus.py; drops are logged. The maximal apt set is
curation-time scaffolding, NOT a PackageSpec field (PackageSpec stays clean)."""
from __future__ import annotations

from dataclasses import dataclass

from src.eval.package_installability.corpus import PackageSpec
from src.eval.package_installability.gate import run_gate

_BOTH = ("natural", "forced_sdist")
DEFAULT_IMAGE = "python:3.11-slim-bookworm"
DEFAULT_PLATFORM = "linux/amd64"


@dataclass(frozen=True)
class Candidate:
    spec: PackageSpec
    maximal_apt: tuple[str, ...]


@dataclass(frozen=True)
class VetResult:
    name: str
    version: str
    mode: str
    ok: bool
    detail: str


def pip_install_cmd(spec: PackageSpec, mode: str) -> str:
    nb = f"--no-binary {spec.name} " if mode == "forced_sdist" else ""
    return f"pip install {nb}{spec.name}=={spec.version}"


def _maximal_apt_for(cand: Candidate, mode: str) -> tuple[str, ...]:
    """The apt set to install for a vetting trial. For ``forced_sdist`` the base
    ``python:*-slim`` image has NO compiler, so ``build-essential`` is always
    required — mirroring what the real eval seeds via ``seed_wheel_oracle_prior``.
    A ``natural`` install that resolves to a wheel needs no compiler, so the base
    is left as the candidate's own maximal set."""
    apt = set(cand.maximal_apt)
    if mode == "forced_sdist":
        apt.add("build-essential")
    return tuple(sorted(apt))


def vet_candidate(executor, cand: Candidate, mode: str) -> VetResult:
    """Install maximal apt + the package in ``executor``, then run the gate.
    ok iff the gate passes. Never raises."""
    s = cand.spec
    try:
        apt = " ".join(_maximal_apt_for(cand, mode))
        if apt:
            r = executor.run(f"apt-get install -y {apt}", timeout=1800)
            if r.returncode != 0:
                return VetResult(s.name, s.version, mode, False, f"apt rc={r.returncode}")
        p = executor.run(pip_install_cmd(s, mode), timeout=2400)
        if p.returncode != 0:
            return VetResult(s.name, s.version, mode, False, f"pip rc={p.returncode}")
        g = run_gate(executor, s, timeout=600)
        ok = g.deep_ok and (g.tail_ok is not False)
        return VetResult(s.name, s.version, mode, ok,
                         "gate ok" if ok else f"gate deep_ok={g.deep_ok} tail_ok={g.tail_ok}")
    except Exception as exc:  # noqa: BLE001 — a vetting failure is a not-ok result
        return VetResult(s.name, s.version, mode, False, f"error: {exc}")


def _c(name, version, stratum, modes, import_name, apt, tail=None) -> Candidate:
    return Candidate(PackageSpec(name, version, stratum, modes, import_name, tail), tuple(apt))


CANDIDATES: tuple[Candidate, ...] = (
    # S1 — single / multi -dev (build-time)
    _c("cffi", "1.16.0", "S1", _BOTH, "cffi", ("libffi-dev",)),
    _c("gmpy2", "2.1.5", "S1", ("forced_sdist",), "gmpy2", ("libgmp-dev", "libmpfr-dev", "libmpc-dev")),
    _c("python-snappy", "0.7.1", "S1", ("forced_sdist",), "snappy", ("libsnappy-dev",)),
    _c("python-lzo", "1.15", "S1", ("forced_sdist",), "lzo", ("liblzo2-dev",)),
    _c("pymssql", "2.3.0", "S1", _BOTH, "pymssql", ("freetds-dev",)),
    _c("confluent-kafka", "2.4.0", "S1", _BOTH, "confluent_kafka", ("librdkafka-dev",)),
    _c("pycups", "2.0.4", "S1", ("forced_sdist",), "cups", ("libcups2-dev",)),
    # S1 — pkg-config / multi -dev
    _c("PyICU", "2.13.1", "S1", ("forced_sdist",), "icu", ("libicu-dev", "pkg-config")),
    _c("dbus-python", "1.3.2", "S1", ("forced_sdist",), "dbus",
       ("libdbus-1-dev", "libglib2.0-dev", "pkg-config")),
    _c("rasterio", "1.3.10", "S1", ("forced_sdist",), "rasterio", ("libgdal-dev",)),
    # S2 — flavor / tool (swig)
    _c("M2Crypto", "0.41.0", "S2", ("forced_sdist",), "M2Crypto", ("libssl-dev", "swig", "pkg-config")),
    # S4 — runtime dlopen (tail exercises the native path)
    _c("python-magic", "0.4.27", "S4", ("natural",), "magic", ("libmagic1",),
       tail='python3 -c "import magic; magic.Magic()"'),
    _c("soundfile", "0.12.1", "S4", ("natural",), "soundfile", ("libsndfile1",),
       tail='python3 -c "import soundfile; soundfile.available_formats()"'),
    _c("netCDF4", "1.6.5", "S4", ("forced_sdist",), "netCDF4", ("libhdf5-dev", "libnetcdf-dev"),
       tail='python3 -c "import netCDF4"'),
    # S5 — sdist, ZERO syslib (must NOT over-predict)
    _c("msgpack", "1.0.8", "S5", ("forced_sdist",), "msgpack", ()),
    _c("simplejson", "3.19.2", "S5", ("forced_sdist",), "simplejson", ()),
    _c("xxhash", "3.4.1", "S5", ("forced_sdist",), "xxhash", ()),
    _c("wrapt", "1.16.0", "S5", ("forced_sdist",), "wrapt", ()),
    _c("psutil", "5.9.8", "S5", ("forced_sdist",), "psutil", ()),
    _c("MarkupSafe", "2.1.5", "S5", ("forced_sdist",), "markupsafe", ()),
    # S6 — pure-wheel negative controls
    _c("jinja2", "3.1.4", "S6", _BOTH, "jinja2", ()),
    _c("rich", "13.7.1", "S6", _BOTH, "rich", ()),
    _c("python-dateutil", "2.9.0.post0", "S6", _BOTH, "dateutil", ()),
    # branch controls — big wheel in natural
    _c("PyYAML", "6.0.1", "branch", _BOTH, "yaml", ()),
    _c("numpy", "1.26.4", "branch", ("natural",), "numpy", ()),
    _c("pandas", "2.2.2", "branch", ("natural",), "pandas", ()),
    _c("scipy", "1.13.1", "branch", ("natural",), "scipy", ()),
)


def run_vetting(image: str = DEFAULT_IMAGE, platform: str = DEFAULT_PLATFORM,
                candidates=CANDIDATES) -> list[VetResult]:
    """Vet each (candidate, mode) in a FRESH container. Docker; lazy import so unit
    tests never need docker."""
    from python_deps.depgraph.executor import DockerExecutor

    results: list[VetResult] = []
    for cand in candidates:
        for mode in cand.spec.modes:
            try:
                with DockerExecutor(image, platform=platform) as ex:
                    if ex.run("apt-get update", timeout=600).returncode != 0:
                        results.append(VetResult(cand.spec.name, cand.spec.version, mode,
                                                 False, "apt-get update failed"))
                        continue
                    results.append(vet_candidate(ex, cand, mode))
            except Exception as exc:  # noqa: BLE001
                results.append(VetResult(cand.spec.name, cand.spec.version, mode,
                                         False, f"container: {exc}"))
    return results
