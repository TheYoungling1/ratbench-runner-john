"""Root selection for the resolver — manifest-declared roots only, filtered.

The resolver (stage 3) needs a set of *distribution* roots to lock.  Picking the
wrong roots is what caused the "Run-A collapse": feeding non-distributions
(stdlib modules, Python-2 shims, typing-only stubs) to ``uv`` made the whole
resolve fail and produced an empty Package layer.

This module realizes the spec's "Root selection" ladder
(``docs/superpowers/specs/2026-06-23-uv-enriched-depgraph.md``):

1. **Manifest-declared roots only** — declared dependencies (parsed via
   :func:`python_deps.evidence.collect_python_dependency_evidence`) are the
   ONLY roots.  They carry no import node, so their ``import_id`` is always
   ``None``.  Imports never generate roots: they are the post-install audit
   signal (catching under-declaration), never a source of install roots (the
   pipreqs mistake).  The installed environment is the naming truth; a later
   post-install repair fixpoint re-homes under-declared-alias recovery.  See
   the two-phase declared-roots design.
2. **Filter non-distributions** — stdlib modules, known Py2 shims, typing-only
   stubs, and obvious junk are dropped before anything reaches ``uv``: a name
   that isn't plausibly a PyPI distribution never becomes a root.

Pure (no Executor / no network): it reads the declared manifest on disk and
returns the root list.  The scanned ``graph`` is accepted but NOT consulted for
roots (reserved; imports are audited post-install, not consulted for roots).
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from python_deps.depgraph.resolve_lock import _marker_applies
from python_deps.depgraph.schema import DepGraph
from python_deps.evidence import collect_python_dependency_evidence
from python_deps.import_mapping import (
    normalize_package_name,
    top_level_import_name,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids any import-order risk
    from python_deps.depgraph.target_env import TargetEnv

# Python-2 modules that survive a py3 static scan as "external" (they are neither
# py3 stdlib nor project-local) but are NOT installable PyPI distributions.
PY2_SHIM_DENYLIST: frozenset[str] = frozenset(
    {
        "StringIO",
        "cStringIO",
        "BaseHTTPServer",
        "SimpleHTTPServer",
        "CGIHTTPServer",
        "urllib2",
        "urlparse",
        "Queue",
        "cPickle",
        "cProfile",
        "Tkinter",
        "httplib",
        "cookielib",
        "Cookie",
        "thread",
        "copy_reg",
        "__builtin__",
        "ConfigParser",
        "HTMLParser",
        "SocketServer",
        "xmlrpclib",
        "robotparser",
        "repr",
    }
)

# Typing-only / stub-only modules that exist for static analysis, not runtime.
TYPING_ONLY_DENYLIST: frozenset[str] = frozenset({"_typeshed"})

# Obvious junk that is never a distribution root.
JUNK_DENYLIST: frozenset[str] = frozenset({"", "__future__", "__main__"})

# Every PEP 508 marker variable name `packaging` can parse into a Marker (the
# grammar restricts marker variables to exactly this set -- see
# https://peps.python.org/pep-0508/#environment-markers). Used by
# ``_env_marker_excludes`` to detect a marker referencing a field the TARGET
# cannot supply (see that function's docstring for why this matters).
_PEP508_MARKER_FIELDS: frozenset[str] = frozenset(
    {
        "os_name",
        "sys_platform",
        "platform_machine",
        "platform_python_implementation",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_version",
        "python_full_version",
        "implementation_name",
        "implementation_version",
        "extra",
    }
)

# A version specifier safe to embed verbatim in the resolver's temp pyproject
# (a TOML double-quoted string) and the fallback heredoc body: PEP 440 operators
# plus version characters only — no quotes, spaces, newlines, shell metacharacters
# or environment markers.  Anything else falls back to the bare name.
_SAFE_SPECIFIER_RE = re.compile(
    r"^[<>=!~]=?[A-Za-z0-9.][A-Za-z0-9.*+!-]*"
    r"(?:,[<>=!~]=?[A-Za-z0-9.][A-Za-z0-9.*+!-]*)*$"
)


def _manifest_root_token(req) -> str:
    """Distribution token for a manifest dep, carrying a safe version specifier.

    Carrying the declared constraint (e.g. ``numpy<2``) is what lets the resolver
    SEE a conflict — the spec's "project pinning numpy<2 plus a dep requiring
    numpy>=2" example.  The specifier is dropped (bare name only) when it is empty,
    carries a marker, or fails the injection-safety check.

    Per-dep extras (``uvicorn[standard]``) are carried too — NOT stripped — so
    the extra's own transitive deps reach the resolver instead of silently
    vanishing under a ``--no-deps`` install (Task 8).
    """
    spec = (getattr(req, "specifier", "") or "").replace(" ", "")
    extras = getattr(req, "extras", ()) or ()
    name = f"{req.name}[{','.join(extras)}]" if extras else req.name
    if spec and _SAFE_SPECIFIER_RE.match(spec):
        return f"{name}{spec}"
    return name


# Group/role sub-label embedded at the tail of a requirement's ``source`` string
# by evidence.py:
#   pyproject.toml:project.optional-dependencies.test  -> test    (feature extra)
#   setup.cfg:options.extras_require.docs              -> docs    (feature extra)
#   pyproject.toml:dependency-groups.typing            -> typing  (PEP 735 dev group)
#   requirements-file.dev                              -> dev     (dev/test reqs file)
_OPTIONAL_GROUP_RE = re.compile(
    r"(?:optional-dependencies|extras_require|dependency-groups|requirements-file)\.(.+)$"
)


def _requirement_group(source: str) -> str:
    """Group/role sub-label a non-runtime requirement belongs to (``""`` if none)."""
    match = _OPTIONAL_GROUP_RE.search(source or "")
    return match.group(1) if match else ""


# Dev/test groups NOT needed to run the test suite: docs builders and
# release/packaging tooling. Every OTHER dev_group (test, lint, typing, dev, ...)
# is default-included (recall-first; the testability gate + Phase-A repair back it
# up). Matched case-insensitively against the normalized group name.
_DEV_GROUP_DENYLIST: frozenset[str] = frozenset(
    {
        "docs", "doc", "documentation",
        "release", "publish", "deploy",
        "benchmark", "benchmarks", "profiling",
        "examples", "demo",
    }
)


def _in_test_scope(req, in_scope_extras: frozenset[str]) -> bool:
    """Testability-scope membership for a declared requirement (fixed policy).

    Single goal = run the tests, so the closure targets runtime ∪ dev/test groups
    ∪ import-signalled feature extras:

    * ``kind=="dependency"`` (runtime) -> always in.
    * ``kind=="optional_dependency"`` (feature extra) -> in IFF its group is a
      member of ``in_scope_extras`` (``needed_extras`` ∪ the repo's own
      ``-e .[...]`` extras). Extras stay gated so mutually-exclusive groups
      (cpu/gpu, conflicting DB drivers) can't collide the resolve — the bug
      ``needed_extras`` was built to prevent. Extras the tests import but no
      signal names are left to the Phase-A repair loop.
    * ``kind=="dev_group"`` (PEP 735 group / dev|test requirements file) -> in
      UNLESS its group is a docs/release group (``_DEV_GROUP_DENYLIST``):
      default-include dev/test/lint/typing (recall-first, gate-backstopped),
      exclude only docs/packaging bloat.
    * anything else (``constraint`` / unknown) -> not a root.
    """
    kind = getattr(req, "kind", "dependency")
    if kind == "dependency":
        return True
    group = normalize_package_name(_requirement_group(getattr(req, "source", "")))
    if kind == "optional_dependency":
        return group in in_scope_extras
    if kind == "dev_group":
        return group not in _DEV_GROUP_DENYLIST
    return False


def _is_non_distribution(import_name: str) -> bool:
    """True when ``import_name`` cannot be a real PyPI distribution root."""
    top = top_level_import_name(import_name).strip()
    if top in JUNK_DENYLIST:
        return True
    if top in PY2_SHIM_DENYLIST:
        return True
    if top in TYPING_ONLY_DENYLIST:
        return True
    # Stdlib for the running interpreter (proxy for the target). scan.py already
    # drops py3 stdlib, but this defends the manifest path and odd classifications.
    # TODO(target-stdlib): this is the HOST interpreter's stdlib set, not the
    # TARGET container's (Task 7 threads a TargetEnv through resolve/marker-eval
    # but this filter runs at root-selection time, before any container is
    # probed, and the target's stdlib set isn't available here). A host running
    # a different python minor than the target could under/over-filter modules
    # that moved in/out of stdlib between versions (e.g. `tomllib` 3.11+,
    # `distutils` removed in 3.12). Low blast radius today (root-selection only
    # drops names it is CONFIDENT aren't distributions), but not target-honest.
    if top in sys.stdlib_module_names:
        return True
    # Leading-underscore private/dunder modules are not distributions.
    if top.startswith("_"):
        return True
    return False


def _env_marker_excludes(req, target_env: "TargetEnv | None") -> bool:
    """True only when we are CONFIDENT ``req`` must not be a root for the target.

    Conservative-skip rule (review "no silent shrink" constraint), generalized
    past the original ``extra``-only special case: a dep is excluded ONLY when
    ALL of the following hold —

    * a real ``target_env`` was given (no target -> nothing is filterable),
    * the requirement HAS a marker at all,
    * EVERY PEP 508 field the marker references is one ``target_env.marker_env()``
      actually supplies (see ``uncovered`` below), and
    * the marker EVALUATES to False against ``target_env.marker_env()``.

    Why generalize: ``TargetEnv.marker_env()`` supplies the 9 PEP 508 fields the
    target confidently controls (``python_version``, ``python_full_version``,
    ``platform_machine``, ``sys_platform``, ``os_name``, ``platform_system``,
    ``platform_python_implementation``, ``implementation_name``,
    ``implementation_version``) but DELIBERATELY withholds the three it cannot
    (``platform_release`` / ``platform_version`` — kernel-specific strings — and
    ``extra`` — a per-requirement grouping flag, not an environment fact).
    ``packaging.markers.Marker.evaluate()`` silently fills any field missing from
    the given environment from the HOST's own ``default_environment()`` -- so a
    marker referencing e.g. ``platform_release`` would be evaluated against the
    HOST's kernel string, not the TARGET's, making a drop decision confidently
    WRONG. The same "field not covered by the target" rule subsumes the old
    ``"extra" in marker`` special-case without naming it specially: ``uncovered``
    always contains ``extra`` alongside ``platform_release`` / ``platform_version``,
    and gating on ANY uncovered field being referenced keeps ``extra``-gated deps
    out of this filter exactly as before (they are already handled by the
    ``needed_extras`` mechanism above; this function must never re-judge them, or
    it would silently drop an extra's dep for a reason unrelated to environment).
    ``uncovered`` is DERIVED from ``target_env.marker_env()`` each call: now that
    ``marker_env()`` covers the interpreter-implementation trio,
    ``uncovered == {"platform_release", "platform_version", "extra"}`` — so a
    ``platform_python_implementation`` / ``implementation_name`` /
    ``implementation_version`` marker is now evaluated against the target instead
    of forcing a keep, while the two kernel fields still protect their deps.

    Every other case — no target_env, no marker, a marker referencing an
    uncovered field, a True evaluation, or any evaluation error
    (``_marker_applies`` returns ``None`` on ``packaging`` absence or a
    malformed marker) — KEEPS the dep. Keeping on uncertainty is the point: an
    over-eager filter here would silently shrink the closure fed to ``uv``,
    which is worse than resolving one extra unneeded root.

    Substring matching (``field in marker``) below errs toward KEEP: a marker
    string that merely happens to contain a field name as a substring (of e.g.
    a version literal) only causes over-inclusion into ``uncovered``, which
    means MORE deps get kept, never fewer — the safe direction. Do not tighten
    this into exact tokenization; that could only ever make the filter drop
    more, which is the wrong direction for this "no silent shrink" invariant.
    """
    if target_env is None:
        return False
    marker = getattr(req, "marker", "") or ""
    if not marker:
        return False
    covered = set(target_env.marker_env())
    uncovered = _PEP508_MARKER_FIELDS - covered
    if any(field in marker for field in uncovered):
        return False
    try:
        verdict = _marker_applies(marker, target_env.marker_env())
    except Exception:
        return False
    return verdict is False


def select_roots(
    repo_path: str,
    graph: DepGraph,
    needed_extras: frozenset[str] = frozenset(),
    *,
    target_env: "TargetEnv | None" = None,
) -> list[tuple[str | None, str]]:
    """Return ``(None, distribution_name)`` resolver roots — declared-only.

    Roots are the manifest-declared dependencies ONLY; every root carries
    ``import_id=None``.  Imports never generate roots (they are the post-install
    audit signal, not a root source).  ``graph`` is accepted but NOT consulted
    for roots (reserved; imports are audited post-install, not consulted for
    roots) — kept in the signature to avoid churning ``build.py`` and every
    caller/test; signature slimming is a deferred follow-up.  Non-distributions
    are filtered out, and each distribution appears once (deduped by normalized
    name).

    Fixed testability-scope policy (see :func:`_in_test_scope`): the closure
    targets runtime ∪ dev/test groups ∪ import-signalled feature extras —
    ``in_scope_extras = needed_extras ∪ evidence.used_extras`` (the caller's
    override union'd with the repo's own ``-e .[...]`` self-install signals).
    Dev/test groups (PEP 735 ``[dependency-groups]`` and dev/test requirements
    files) are default-INCLUDED except the docs/release denylist
    (``_DEV_GROUP_DENYLIST``); ``kind=="optional_dependency"`` feature extras
    STAY gated by ``in_scope_extras`` — mutually exclusive groups (e.g.
    ``cpu``/``gpu``) must not both enter one resolve, which is why extras
    are never default-included even though dev/test groups are. The default
    (``needed_extras=frozenset()``) therefore no longer means "runtime only";
    it means "runtime + dev/test groups + whatever extras the repo's own
    ``-e .[...]`` lines already signal".

    ``target_env`` (Task 8 review fix), when given, additionally drops a
    manifest dependency whose PEP 508 environment marker evaluates False for
    the TARGET (e.g. ``foo ; sys_platform == 'win32'`` on a Linux target) —
    see :func:`_env_marker_excludes` for the conservative "keep unless
    certain" rule. Default ``None`` preserves the pre-Task-8-review behavior
    for every existing caller (``advise.py`` and current tests).
    """
    evidence = collect_python_dependency_evidence(repo_path)

    # Feature-extras in scope = caller/CI override ∪ the repo's own ``-e .[...]``
    # self-install signals (evidence.used_extras). Normalized PEP 685-style
    # (separators -/_/. collapsed to a single ``-``, lowercased) via the same
    # `normalize_package_name` used for distribution names, so a dash-form
    # signal (``-e .[socks-extra]``) matches an underscore-form declared group
    # (``socks_extra``) instead of silently missing it.
    in_scope_extras = frozenset(
        normalize_package_name(extra) for extra in needed_extras
    ) | frozenset(normalize_package_name(extra) for extra in evidence.used_extras)

    roots: list[tuple[str | None, str]] = []
    seen: set[str] = set()

    # Manifest-declared dependencies are the ONLY roots (highest trust). Imports
    # never generate roots; they are audited post-install, not consulted here.
    # Testability scope: runtime + dev/test groups (minus docs/release) + signalled
    # feature extras — see _in_test_scope.
    for req in evidence.declared_dependencies:
        if not _in_test_scope(req, in_scope_extras):
            continue
        if _env_marker_excludes(req, target_env):
            continue
        normalized = normalize_package_name(req.name)
        if normalized in seen:
            continue
        if _is_non_distribution(req.name):
            continue
        seen.add(normalized)
        roots.append((None, _manifest_root_token(req)))

    return roots
