"""Pure wheel artifact reader for pre-install soname discovery (host-side).

Downloads a package's target wheel WITHOUT installing it and reads the shared
libraries its compiled extension ``.so`` files link against (``DT_NEEDED``),
walking transitively through the wheel's bundled ``.libs/`` libraries too, so
the graph can seed ``SystemLib`` priors BEFORE the final install. No graph /
apt / ``TargetEnv`` knowledge — strings and paths only. Reproducible by
construction: the caller pins the target platform tag, so the same wheel is
fetched and read on any host.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import sys
import zipfile

from elftools.elf.dynamic import DynamicSection
from elftools.elf.elffile import ELFFile

from python_deps.depgraph.executor import Executor

# Extension-module basenames: ``cpython-NN[N]*.so`` (NN = 2-digit for 3.0-3.9,
# NNN = 3-digit for 3.10+) or ``abi3.so``. Mirrors ldd_probe.EXT_SO_MAP_CMD.
_EXT_SO_RE = re.compile(r"\.cpython-\d{2,3}.*\.so\Z|\.abi3\.so\Z")
# Auditwheel vendors external libs into a top-level ``<distname>.libs/`` directory and
# relinks them to hash-suffixed names. We WALK those bundled libs (for their transitive
# DT_NEEDED) but subtract their own basenames from the result — they are satisfied
# inside the wheel, so they are not external system requirements.
_BUNDLED_DIR_RE = re.compile(r"(^|/)[A-Za-z0-9][A-Za-z0-9._+-]*\.libs/")
# Base-image / toolchain sonames every glibc target already provides. Reading
# ALL DT_NEEDED (unlike ldd's ``=> not found``) would otherwise seed a dozen
# trivially-satisfied nodes per package; filtering keeps the interesting
# external libs (libGL, libglib, libpq).
_BASE_IMAGE_SONAMES = frozenset(
    {
        "libc.so.6",
        "libm.so.6",
        "libdl.so.2",
        "libpthread.so.0",
        "librt.so.1",
        "libutil.so.1",
        "libnsl.so.1",
        "libresolv.so.2",
        "libstdc++.so.6",
        "libgcc_s.so.1",
        "libcrypt.so.1",
        "ld-linux-x86-64.so.2",
        "ld-linux-aarch64.so.1",
        # zlib: required by CPython's core `zlib` module, so it is guaranteed
        # present (shared lib, not just the Python module) in any CPython
        # base image regardless of what the target package needs it for.
        "libz.so.1",
    }
)


def download_target_wheel(
    name: str,
    version: str | None,
    *,
    platform_tag: str,
    py_version: str,
    abi: str,
    dest: str,
    executor: Executor,
) -> str | None:
    """Download the target wheel into ``dest`` (no install); return path or None.

    ``--only-binary=:all:`` makes sdist-only or incompatible packages fail fast
    (returns None -> caller falls through to build-essential seeding). The host
    interpreter is invoked via ``sys.executable`` because the dev host may have
    no bare ``python`` on PATH. Any executor failure / empty result returns None
    so the stage degrades to a no-op.
    """
    spec = f"{name}=={version}" if version else name
    cmd = (
        f"{shlex.quote(sys.executable)} -m pip download "
        "--no-deps --only-binary=:all: "
        f"--dest {shlex.quote(dest)} "
        f"--platform {shlex.quote(platform_tag)} "
        f"--python-version {shlex.quote(py_version)} "
        f"--implementation cp --abi {shlex.quote(abi)} "
        f"{shlex.quote(spec)}"
    )
    try:
        result = executor.run(cmd, timeout=300)
    except Exception:
        return None
    if not result.ok:
        return None
    try:
        wheels = [f for f in os.listdir(dest) if f.endswith(".whl")]
    except OSError:
        return None
    if not wheels:
        return None
    return os.path.join(dest, sorted(wheels)[0])


def inspect_wheel_sonames(wheel_path: str) -> set[str]:
    """External sonames a wheel needs at run time, incl. transitive-via-bundled.

    Reads ``DT_NEEDED`` from the wheel's extension module(s) AND its bundled
    ``<dist>.libs/`` shared libraries (mirroring what ``ldd`` resolves
    transitively post-install), then subtracts base-image/toolchain libs and the
    wheel's own bundled library names (satisfied internally). Empty set for
    pure-Python wheels or on any read error (caller no-ops).
    """
    needed: set[str] = set()
    provided: set[str] = set()  # sonames the wheel supplies internally
    scan: list = []             # zip members whose DT_NEEDED we read
    try:
        with zipfile.ZipFile(wheel_path) as zf:
            for info in zf.infolist():
                filename = info.filename
                base = os.path.basename(filename)
                if not (base.endswith(".so") or ".so." in base):
                    continue
                if _BUNDLED_DIR_RE.search(filename):
                    provided.add(base)  # a lib the wheel bundles
                    scan.append(info)   # ...but still walk it for transitive deps
                elif _EXT_SO_RE.search(base):
                    scan.append(info)   # the extension module itself
                # else: a top-level non-extension .so — ignore
            for info in scan:
                try:
                    data = zf.read(info)
                except Exception:
                    continue
                needed |= _dt_needed(io.BytesIO(data))
    except Exception:
        return set()
    return {s for s in needed if s not in _BASE_IMAGE_SONAMES and s not in provided}


def _dt_needed(fileobj: io.BytesIO) -> set[str]:
    """``DT_NEEDED`` sonames from one ELF via pyelftools; empty set on non-ELF."""
    out: set[str] = set()
    try:
        elf = ELFFile(fileobj)
    except Exception:
        return out
    for section in elf.iter_sections():
        if not isinstance(section, DynamicSection):
            continue
        for tag in section.iter_tags():
            if getattr(tag, "entry", None) is not None and tag.entry.d_tag == "DT_NEEDED":
                out.add(tag.needed)
    return out
