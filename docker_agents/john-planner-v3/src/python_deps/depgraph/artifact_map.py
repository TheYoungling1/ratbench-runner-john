"""§1 — wheel/sdist branch oracle read from the REAL resolver.

``resolve_artifact_map`` classifies every requirement as ``"wheel"`` or
``"sdist"`` by asking pip what it would actually do, instead of a per-package
``--platform`` tag-match probe (unsound: pip may pick a newer sdist over an
older wheel; ``--no-binary``/constraints/``pip.conf`` are invisible; opaque
``--platform`` string matching false-negatives on forward-compatible manylinux
and ``abi3`` wheels; resolver backtracking/yanks make the chosen version
unknowable per-package).

Three tiers, highest fidelity first:

1. PRIMARY (native, in the target container): ONE
   ``pip install --dry-run --ignore-installed --report`` run over the whole
   requirement set; classify each ``install[i].download_info.url``
   (``.whl`` -> wheel, ``.tar.gz``/``.zip`` -> sdist). Ground truth for what
   pip will actually do.
2. HOST-SIDE FALLBACK (cross-target, no native interpreter — Task 2):
   per-package ``pip download --only-binary=:all:`` against the FULL expanded
   compatible-tag set. Resolves -> wheel; "no matching distribution" -> sdist.
3. LOWEST-FIDELITY FALLBACK (Task 3): the package's PyPI file list matched
   against the target tag set.

Pure w.r.t. the graph: returns a plain dict; the caller (``build.py``) stamps
``Node.build_from_source`` from it. Degrades to ``{}`` (caller then keeps the
resolver's own tag-match heuristic) on any failure.
"""

from __future__ import annotations

import json
import logging
import re
import shlex

from python_deps.depgraph.executor import Executor
from python_deps.depgraph.target_env import TargetEnv
from python_deps.import_mapping import normalize_package_name

_WHEEL = "wheel"
_SDIST = "sdist"

logger = logging.getLogger(__name__)

# In-container scratch paths for the one dry-run report.
_REPORT_PATH = "/tmp/depgraph-artifact-report.json"
_REQS_PATH = "/tmp/depgraph-artifact-reqs.txt"
_REQS_HEREDOC = "DEPGRAPH_ARTIFACT_REQS"

# glibc manylinux policy ladder (§4 scope: Debian/Ubuntu on glibc). Each rung is
# passed as its own `--platform` so pip need not do forward-compat matching on a
# single opaque tag; legacy perennial aliases are appended too.
_GLIBC_MANYLINUX = ((2, 34), (2, 31), (2, 28), (2, 24), (2, 17), (2, 12), (2, 5))
_LEGACY_MANYLINUX = ("manylinux2014", "manylinux2010", "manylinux1")
_DL_DIR = "/tmp/depgraph-artifact-dl"

# Leading distribution name of a requirement line (`foo==1.2` -> `foo`).
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
# pip's genuine "no compatible distribution" signature (the ONLY rc!=0 that
# means sdist; every other failure is left unclassified).
_NO_DIST_RE = re.compile(
    r"no matching distribution|could not find a version", re.IGNORECASE
)


def _req_key(req: str) -> str | None:
    m = _REQ_NAME_RE.match(req)
    return normalize_package_name(m.group(1)) if m else None


def _classify_url(url: str) -> str | None:
    """``.whl`` -> wheel; ``.tar.gz``/``.zip`` -> sdist; anything else -> None."""
    low = url.lower()
    if low.endswith(".whl"):
        return _WHEEL
    if low.endswith(".tar.gz") or low.endswith(".zip"):
        return _SDIST
    return None


def _parse_report(stdout: str) -> dict[str, str]:
    """Parse a pip ``--report`` JSON document into a canonical-name -> kind map."""
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for entry in data.get("install", []):
        if not isinstance(entry, dict):
            continue
        meta = entry.get("metadata") or {}
        dl = entry.get("download_info") or {}
        name = meta.get("name") if isinstance(meta, dict) else None
        url = dl.get("url") if isinstance(dl, dict) else None
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        kind = _classify_url(url)
        if kind is not None:
            out[normalize_package_name(name)] = kind
    return out


def _primary_report(reqs: list[str], executor: Executor) -> dict[str, str]:
    """PRIMARY tier: one native ``pip install --dry-run --report`` over ``reqs``.

    Writes the requirement set to a file (heredoc), runs a resolution-only
    dry-run that emits a machine-readable report, then ``cat``s the report to
    stdout so a single ``executor.run`` yields the JSON. ``--ignore-installed``
    forces base-image-preinstalled packages to appear (so they too are
    classified). Any failure -> ``{}`` (the caller then tries the fallbacks).
    """
    body = "\n".join(reqs)
    cmd = (
        f"cat > {_REQS_PATH} <<'{_REQS_HEREDOC}'\n{body}\n{_REQS_HEREDOC}\n"
        f"rm -f {_REPORT_PATH}\n"
        f"python3 -m pip install --dry-run --ignore-installed "
        f"--report {_REPORT_PATH} -r {_REQS_PATH} >/dev/null 2>&1 "
        f"&& cat {_REPORT_PATH}"
    )
    try:
        result = executor.run(cmd, timeout=300)
    except Exception:
        return {}
    if not result.ok:
        return {}
    return _parse_report(result.stdout or "")


def _target_arch(target_env: TargetEnv) -> str:
    """Wheel-tag arch token from the uv-shaped platform tag (`x86_64-manylinux…`)."""
    return target_env.python_platform_tag.partition("-")[0] or "x86_64"


def _platform_flags(target_env: TargetEnv) -> list[str]:
    """Full expanded glibc platform tag list (manylinux ladder + legacy + linux)."""
    arch = _target_arch(target_env)
    plats = [f"manylinux_{maj}_{mnr}_{arch}" for (maj, mnr) in _GLIBC_MANYLINUX]
    plats += [f"{alias}_{arch}" for alias in _LEGACY_MANYLINUX]
    plats.append(f"linux_{arch}")
    return plats


def _abi_flags(target_env: TargetEnv, platforms: list[str]) -> list[str]:
    """ABI tokens (`cp311`, `abi3`, `none`) for the target, via packaging.tags."""
    try:
        from packaging.tags import cpython_tags

        parts = target_env.python_version.split(".")
        major, minor = int(parts[0]), int(parts[1])
        abis: list[str] = []
        for tag in cpython_tags(python_version=(major, minor), platforms=platforms):
            if tag.abi not in abis:
                abis.append(tag.abi)
        if abis:
            return abis
    except Exception:
        pass
    ver = target_env.python_version.replace(".", "")
    return [f"cp{ver}", "abi3", "none"]


def _platform_fallback(
    reqs: list[str], executor: Executor, target_env: TargetEnv
) -> dict[str, str]:
    """Cross-target availability probe: a resolvable wheel -> wheel; a genuine
    "no matching distribution" -> sdist; any other failure left unclassified.
    """
    platforms = _platform_flags(target_env)
    abis = _abi_flags(target_env, platforms)
    plat_args = " ".join(f"--platform {shlex.quote(p)}" for p in platforms)
    abi_args = " ".join(f"--abi {shlex.quote(a)}" for a in abis)
    py = shlex.quote(target_env.python_version)

    out: dict[str, str] = {}
    for req in reqs:
        key = _req_key(req)
        if key is None:
            continue
        cmd = (
            f"rm -rf {_DL_DIR}; python3 -m pip download --no-deps "
            f"--only-binary=:all: --dest {_DL_DIR} "
            f"--python-version {py} --implementation cp {abi_args} {plat_args} "
            f"{shlex.quote(req)}"
        )
        try:
            result = executor.run(cmd, timeout=180)
        except Exception:
            continue
        if result.ok:
            out[key] = _WHEEL
        elif _NO_DIST_RE.search((result.stderr or "") + "\n" + (result.stdout or "")):
            out[key] = _SDIST
    return out


# PyPI JSON reader run through the executor (the `/<version>/json` endpoint
# exposes that release's files under `urls`; the `/json` endpoint exposes the
# latest release's files under the same key). Kept tiny and stdlib-only.
_PYPI_SNIPPET = (
    "import json,urllib.request,sys;"
    "d=json.load(urllib.request.urlopen("
    "'https://pypi.org/pypi/'+sys.argv[1],timeout=15));"
    "print(chr(10).join(f.get('filename','') for f in d.get('urls',[])))"
)


def _split_req(req: str) -> tuple[str, str | None]:
    """`foo==1.2` -> (`foo`, `1.2`); unpinned -> (`foo`, None)."""
    m = _REQ_NAME_RE.match(req)
    name = m.group(1) if m else req.strip()
    version = None
    if "==" in req:
        version = req.split("==", 1)[1].split(";")[0].strip() or None
    return name, version


def _pypi_files(executor: Executor, name: str, version: str | None) -> list[str]:
    path = f"{name}/{version}/json" if version else f"{name}/json"
    cmd = f"python3 -c {shlex.quote(_PYPI_SNIPPET)} {shlex.quote(path)}"
    try:
        result = executor.run(cmd, timeout=30)
    except Exception:
        return []
    if not result.ok:
        return []
    return [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]


def _pypi_fallback(
    reqs: list[str], executor: Executor, target_env: TargetEnv
) -> dict[str, str]:
    """Lowest-fidelity: classify from the PyPI file list against the target tag.

    A file list containing a wheel compatible with the target platform -> wheel;
    else an sdist archive present -> sdist. Reuses ``wheel_oracle`` tag matching.
    """
    from python_deps.depgraph.wheel_oracle import _wheel_matches_platform

    tp = target_env.python_platform_tag
    out: dict[str, str] = {}
    for req in reqs:
        key = _req_key(req)
        if key is None:
            continue
        name, version = _split_req(req)
        files = _pypi_files(executor, name, version)
        if not files:
            continue
        if any(_wheel_matches_platform(f, tp) for f in files):
            out[key] = _WHEEL
        elif any(f.lower().endswith((".tar.gz", ".zip")) for f in files):
            out[key] = _SDIST
    return out


def resolve_artifact_map(
    reqs: list[str],
    executor: Executor,
    *,
    target_env: TargetEnv | None = None,
) -> dict[str, str]:
    """Canonical package name -> ``"wheel"`` | ``"sdist"``, from the real resolver.

    See the module docstring for the three tiers. ``target_env`` is required only
    to engage the cross-target fallbacks (Tasks 2/3); the native primary path
    ignores it. Never raises; returns ``{}`` when nothing can be classified.
    """
    if not reqs:
        return {}
    mapping = _primary_report(reqs, executor)
    tier = "primary"
    if not mapping and target_env is not None:
        mapping = _platform_fallback(reqs, executor, target_env)
        tier = "platform"
        if not mapping:
            mapping = _pypi_fallback(reqs, executor, target_env)
            tier = "pypi"
    if not mapping:
        tier = "none"
    wheel = sum(1 for v in mapping.values() if v == _WHEEL)
    sdist = sum(1 for v in mapping.values() if v == _SDIST)
    logger.info(
        "artifact_map: tier=%s classified=%d wheel=%d sdist=%d unclassified=%d",
        tier, len(mapping), wheel, sdist, len(reqs) - len(mapping),
    )
    return mapping
