"""``TargetEnv`` — the ONE set of facts a resolve must honor: the TARGET
container's python + platform, never the HOST running the resolve.

``packaging.markers.Marker.evaluate(environment)`` fills any PEP 508 key
missing from ``environment`` from ``packaging.markers.default_environment()``
— which reflects the HOST process, not the container being built. Before this
module existed, ``resolve_lock._python_marker_env`` passed only
``python_version`` / ``python_full_version``, so ``sys_platform`` /
``platform_machine`` / ``os_name`` silently leaked from the host. On a
non-x86_64-linux dev host that wrongly prunes (or keeps) platform-gated deps
for the container.

``TargetEnv.marker_env()`` returns every field the target CONFIDENTLY controls
(the python/platform facts plus the interpreter-implementation trio) so none of
those is ever left for the host default to fill in; only genuinely-unknowable
fields (kernel ``platform_release`` / ``platform_version``) and the
per-requirement ``extra`` flag are deliberately withheld — see
:meth:`TargetEnv.marker_env`. ``detect_target_env`` probes the container ONCE
(python + implementation + platform.machine + a libc guess for the wheel/uv
platform tag needed at PARSE time) and degrades to sensible defaults on any
failure — detection must never crash the resolve. ``uv.lock`` itself is
universal/cross-platform, so this platform tag is never passed to ``uv lock``
(it has no such flag); it instead drives ``parse_uv_lock``/
``native_risk_from_lock``'s wheel-artifact matching.
"""

from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.executor import Executor

# Single probe: version, os.name, sys.platform, machine, platform.system, plus
# platform.python_implementation() + sys.implementation.name — one process, one
# round trip into the container. `python3` ONLY, no `python`
# fallback: the rest of the construction/certification pipeline (build.py's
# runtime check, runtime_classify.py's import checks, emit.py's install steps)
# hard-codes `python3`, so a `python`-only image is not a supported target —
# detecting it here would just defer the failure to `python3: not found` later
# in the pipeline. Absent/unusable `python3` safely degrades to the default
# TargetEnv below instead. The last two tokens (the interpreter implementation)
# are read positionally and OPTIONAL: a legacy 5-token probe output still parses,
# defaulting the implementation to CPython (the only base this pipeline builds —
# ImageSelector always picks an official `python:X` / `python:X-slim` image).
_PROBE_BODY = (
    "import platform,sys,os; "
    "print(sys.version.split()[0], os.name, sys.platform, "
    "platform.machine(), platform.system(), "
    "platform.python_implementation(), sys.implementation.name)"
)
_PROBE_CMD = f'python3 -c "{_PROBE_BODY}"'
_LIBC_PROBE_CMD = "ldd --version"

# Defaults used both as the "never crash" fallback and as the base target this
# codebase already assumes elsewhere (DEFAULT_TARGET_PLATFORM in resolve_lock.py
# is also x86_64-manylinux_2_28 — never manylinux2014, it silently downgrades
# wheels like numpy).
_DEFAULT_PYTHON_FULL = "3.11.0"
_DEFAULT_PYTHON_VERSION = "3.11"
_DEFAULT_MACHINE = "x86_64"
_DEFAULT_SYS_PLATFORM = "linux"
_DEFAULT_OS_NAME = "posix"
_DEFAULT_PLATFORM_SYSTEM = "Linux"
# The target is ALWAYS CPython: base images come from ImageSelector, which only
# ever selects official `python:X` / `python:X-slim` (Docker Hub `python:` tags
# are CPython). detect_target_env still PROBES the real interpreter and threads
# whatever it reports, so a non-CPython image (were one ever used) is honored;
# these are only the "never crash" degrade values.
_DEFAULT_PYTHON_IMPL = "CPython"
_DEFAULT_IMPL_NAME = "cpython"
_GLIBC_TAG = "manylinux_2_28"
_MUSL_TAG = "musllinux_1_2"

# `platform.machine()` inside the container is always Linux (the target is
# always a Linux container), but some images/kernels report non-canonical
# aliases (`arm64`, `amd64`) instead of the `aarch64`/`x86_64` values wheel
# tags expect. Normalize ONLY for the wheel/uv platform tag (used at PARSE
# time by parse_uv_lock/native_risk_from_lock, never passed to `uv lock`
# itself) — `packaging` compares PEP 508 `platform_machine` markers against
# `platform.machine()` VERBATIM, so `TargetEnv.platform_machine` must stay RAW
# or a marker like `platform_machine == 'arm64'` would wrongly evaluate False.
_MACHINE_ALIASES = {"arm64": "aarch64", "amd64": "x86_64", "x86-64": "x86_64"}


def _normalize_machine(machine: str) -> str:
    return _MACHINE_ALIASES.get(machine.strip().lower(), machine.strip())


@dataclass(frozen=True)
class TargetEnv:
    """Facts about the TARGET container a resolve/marker-eval must honor.

    One instance flows from ``detect_target_env`` (or a caller override) through
    ``resolve_closure`` into every marker evaluation and into the PARSE-time
    wheel-artifact match, so a mismatched dev host never substitutes its own
    platform for the container's. Concretely: ``build.py`` passes THIS OBJECT
    (never two decomposed strings) into ``resolve_closure``, which threads it
    into ``parse_uv_lock``/``native_risk_from_lock`` for marker evaluation via
    :meth:`marker_env` — so a marker like ``platform_machine == 'arm64'`` sees
    the container's own RAW ``platform.machine()``, while ``python_platform_tag``
    (the NORMALIZED wheel tag) is what those same parsers use for wheel
    matching (``uv.lock`` is universal, so this tag is never passed to ``uv
    lock`` itself).  Reconstructing a ``TargetEnv`` FROM those two strings
    after the fact (as ``resolve_lock._target_env_for`` still does, for
    callers with no real instance to pass) can only ever recover the
    normalized arch — never a raw alias — which is exactly the bug this
    end-to-end threading closes.
    """

    python_full: str
    python_version: str
    platform_machine: str
    sys_platform: str
    os_name: str
    platform_system: str
    python_platform_tag: str
    # Interpreter implementation — probed (or CPython-defaulted). Appended with
    # defaults so every existing ``TargetEnv(...)`` construction stays valid.
    platform_python_implementation: str = _DEFAULT_PYTHON_IMPL
    implementation_name: str = _DEFAULT_IMPL_NAME

    def marker_env(self) -> dict[str, str]:
        """PEP 508 marker-evaluation environment for this target.

        Supplies every field the TARGET can CONFIDENTLY control, so
        ``Marker.evaluate()`` never falls back to its HOST-derived
        ``default_environment()`` for a value the container actually determines:
        the six python/platform facts PLUS the interpreter-implementation trio
        (``platform_python_implementation``, ``implementation_name``,
        ``implementation_version``). The implementation is probed by
        ``detect_target_env`` and defaults to CPython (the only base this
        pipeline builds); ``implementation_version`` is the CPython-correct
        derivation from ``python_full`` (for CPython ``sys.implementation.version``
        equals ``sys.version_info``, so its PEP 508 rendering is ``python_full``).

        Three PEP 508 fields are DELIBERATELY absent — ``platform_release`` and
        ``platform_version`` are kernel-specific strings the container cannot know
        ahead of run time, and ``extra`` is a per-requirement grouping flag, not
        an environment fact. Leaving them out is intentional: ``roots._env_marker_excludes``
        treats a marker over any absent field as "keep unless certain", so a dep
        gated on a genuinely-unknowable field is never silently pruned.
        """
        return {
            "python_version": self.python_version,
            "python_full_version": self.python_full,
            "platform_machine": self.platform_machine,
            "sys_platform": self.sys_platform,
            "os_name": self.os_name,
            "platform_system": self.platform_system,
            "platform_python_implementation": self.platform_python_implementation,
            "implementation_name": self.implementation_name,
            # CPython: sys.implementation.version == sys.version_info, so its
            # PEP 508 rendering is exactly python_full. Safe because the target
            # is always CPython (see class default / detect probe).
            "implementation_version": self.python_full,
        }


def _default_target_env() -> TargetEnv:
    return TargetEnv(
        python_full=_DEFAULT_PYTHON_FULL,
        python_version=_DEFAULT_PYTHON_VERSION,
        platform_machine=_DEFAULT_MACHINE,
        sys_platform=_DEFAULT_SYS_PLATFORM,
        os_name=_DEFAULT_OS_NAME,
        platform_system=_DEFAULT_PLATFORM_SYSTEM,
        python_platform_tag=f"{_DEFAULT_MACHINE}-{_GLIBC_TAG}",
        platform_python_implementation=_DEFAULT_PYTHON_IMPL,
        implementation_name=_DEFAULT_IMPL_NAME,
    )


def _platform_tag(machine: str, libc: str) -> str:
    suffix = _MUSL_TAG if libc == "musl" else _GLIBC_TAG
    return f"{machine}-{suffix}"


def _detect_libc(executor: Executor) -> str:
    """glibc vs musl guess via ``ldd --version`` (musl prints to stderr, rc!=0).

    Defaults to glibc (the common case, and the non-downgrading choice) on any
    ambiguity or probe failure.
    """
    try:
        result = executor.run(_LIBC_PROBE_CMD)
    except Exception:
        return "glibc"
    text = f"{result.stdout or ''} {result.stderr or ''}".lower()
    return "musl" if "musl" in text else "glibc"


def detect_target_env(executor: Executor) -> TargetEnv:
    """Probe the TARGET container once for the facts a marker-honest resolve
    needs, deriving ``python_platform_tag`` from the machine + a libc guess.

    Degrades to sensible (linux/posix/x86_64/glibc) defaults whenever the probe
    fails, is unparsable, or the executor raises — detection must never crash
    the resolve.
    """
    try:
        result = executor.run(_PROBE_CMD)
    except Exception:
        return _default_target_env()
    if not result.ok:
        return _default_target_env()

    parts = (result.stdout or "").split()
    if len(parts) < 5:
        return _default_target_env()

    python_full, os_name, sys_platform, machine, platform_system = parts[:5]
    # Interpreter implementation is read positionally and stays OPTIONAL: a
    # legacy 5-token probe (or any output without the trailing two tokens)
    # degrades to CPython — the only base this pipeline builds.
    python_impl = parts[5] if len(parts) > 5 else _DEFAULT_PYTHON_IMPL
    impl_name = parts[6] if len(parts) > 6 else _DEFAULT_IMPL_NAME
    version_parts = python_full.split(".")
    python_version = (
        ".".join(version_parts[:2]) if len(version_parts) >= 2 else python_full
    )

    # Reject Python 2 (some images alias `python3` to a legacy 2.x build). A
    # 2.x TargetEnv would make `uv lock --python 2.7` disagree with build.py's
    # runtime node, which is always python3 — degrade to the known-good
    # default instead of building a self-inconsistent graph.
    major = version_parts[0]
    if not major.isdigit() or int(major) < 3:
        return _default_target_env()

    libc = _detect_libc(executor)
    return TargetEnv(
        python_full=python_full,
        python_version=python_version,
        # RAW machine, verbatim from `platform.machine()` — packaging compares
        # PEP 508 `platform_machine` markers against this exact string; only
        # the wheel/uv platform TAG below (`python_platform_tag`) needs
        # normalization.
        platform_machine=machine,
        sys_platform=sys_platform,
        os_name=os_name,
        platform_system=platform_system,
        python_platform_tag=_platform_tag(_normalize_machine(machine), libc),
        platform_python_implementation=python_impl,
        implementation_name=impl_name,
    )


def pip_wheel_platform_tag(env: TargetEnv) -> str:
    """Convert the uv-shaped platform tag to pip's ``--platform`` wheel-tag order.

    ``python_platform_tag`` is uv-shaped (``x86_64-manylinux_2_28``); ``pip
    download --platform`` expects wheel-tag order (``manylinux_2_28_x86_64``).
    Split on the first ``-`` (the arch prefix) and reorder to ``<policy>_<arch>``.
    A tag without a ``-`` is returned unchanged.
    """
    machine, sep, policy = env.python_platform_tag.partition("-")
    if not sep:
        return env.python_platform_tag
    return f"{policy}_{machine}"
