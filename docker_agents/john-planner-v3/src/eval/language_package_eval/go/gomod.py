"""Offline parsers for Go module manifests + the ``module_closure`` authority
ladder. Pure text/JSON — no ``go`` toolchain, no network. Analog of
``node/lockfile.py``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_BLOCK_OPEN = re.compile(r"^(require|replace|exclude)\s*\($")
_SINGLE = re.compile(r"^(require|replace|exclude)\s+(.*)$")


@dataclass(frozen=True)
class Require:
    path: str
    version: str
    indirect: bool = False


@dataclass(frozen=True)
class Replace:
    old_path: str
    old_version: str | None
    new_path: str
    new_version: str | None  # None => local filesystem target


@dataclass(frozen=True)
class Exclude:
    path: str
    version: str


@dataclass(frozen=True)
class GoMod:
    module_path: str
    go_version: str
    toolchain: str | None = None
    requires: tuple[Require, ...] = ()
    replaces: tuple[Replace, ...] = ()
    excludes: tuple[Exclude, ...] = ()


def _strip_comment(line: str) -> tuple[str, str]:
    """Split at the first ``//``; returns (code, comment-without-slashes)."""
    idx = line.find("//")
    if idx == -1:
        return line, ""
    return line[:idx], line[idx + 2 :]


def _go_version_tuple(go_version: str) -> tuple[int, ...]:
    """``"1.21"`` -> ``(1, 21)``; ``"go1.21.0"`` -> ``(1, 21, 0)``; ``""`` -> ``()``."""
    cleaned = go_version.lstrip("go")
    return tuple(int(p) for p in cleaned.split(".") if p.isdigit())


def _consume(directive, rest, comment, requires, replaces, excludes) -> None:
    parts = rest.split()
    if directive == "require" and len(parts) >= 2:
        requires.append(Require(parts[0], parts[1], "indirect" in comment.split()))
    elif directive == "exclude" and len(parts) >= 2:
        excludes.append(Exclude(parts[0], parts[1]))
    elif directive == "replace" and "=>" in parts:
        i = parts.index("=>")
        left, right = parts[:i], parts[i + 1 :]
        if not left or not right:
            return
        replaces.append(
            Replace(
                old_path=left[0],
                old_version=left[1] if len(left) > 1 else None,
                new_path=right[0],
                new_version=right[1] if len(right) > 1 else None,
            )
        )


def parse_go_mod(path: str | Path) -> GoMod:
    text = Path(path).read_text()
    module_path = go_version = ""
    toolchain: str | None = None
    requires: list[Require] = []
    replaces: list[Replace] = []
    excludes: list[Exclude] = []
    block: str | None = None

    for raw in text.splitlines():
        code, comment = _strip_comment(raw)
        s = code.strip()
        if not s:
            continue
        if s == ")":
            block = None
            continue
        m = _BLOCK_OPEN.match(s)
        if m:
            block = m.group(1)
            continue
        if block is not None:
            _consume(block, s, comment, requires, replaces, excludes)
            continue
        m = _SINGLE.match(s)
        if m:
            _consume(m.group(1), m.group(2), comment, requires, replaces, excludes)
            continue
        parts = s.split()
        if parts[0] == "module":
            module_path = parts[1]
        elif parts[0] == "go":
            go_version = parts[1]
        elif parts[0] == "toolchain":
            toolchain = parts[1]

    return GoMod(
        module_path=module_path,
        go_version=go_version,
        toolchain=toolchain,
        requires=tuple(requires),
        replaces=tuple(replaces),
        excludes=tuple(excludes),
    )


_WORK_BLOCK_OPEN = re.compile(r"^use\s*\($")
_WORK_SINGLE = re.compile(r"^use\s+(.+)$")


def parse_vendor_modules_txt(path: str | Path) -> dict[str, str]:
    """``{module: version}`` from ``# <module> <version>`` header lines. ``## ``
    annotation lines and package-path lines are skipped. A ``=> replacement``
    tail takes the replacement's trailing version. Corpus vendored entries are
    chosen without replaces (spec §7); ``=>`` handling here is defensive."""
    out: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        line = raw.rstrip()
        if line.startswith("## ") or not line.startswith("# "):
            continue
        parts = line[2:].split()
        if len(parts) < 2:
            continue
        mod = parts[0]
        if "=>" in parts:
            right = parts[parts.index("=>") + 1 :]
            out[mod] = right[-1] if len(right) >= 2 else parts[1]
        else:
            out[mod] = parts[1]
    return out


def parse_go_sum(path: str | Path) -> frozenset[tuple[str, str]]:
    """``{(module, version)}`` cross-check set — a SUPERSET of the closure, never
    the closure itself. ``<mod> <ver>/go.mod <hash>`` collapses to ``(mod, ver)``."""
    p = Path(path)
    if not p.is_file():
        return frozenset()
    out: set[tuple[str, str]] = set()
    for raw in p.read_text().splitlines():
        parts = raw.split()
        if len(parts) >= 2:
            out.add((parts[0], parts[1].split("/")[0]))
    return frozenset(out)


def parse_go_work(path: str | Path) -> tuple[str, ...]:
    """The ``use ./dir`` member directories from a ``go.work``. Empty if absent."""
    p = Path(path)
    if not p.is_file():
        return ()
    members: list[str] = []
    in_block = False
    for raw in p.read_text().splitlines():
        code, _ = _strip_comment(raw)
        s = code.strip()
        if not s:
            continue
        if s == ")":
            in_block = False
            continue
        if _WORK_BLOCK_OPEN.match(s):
            in_block = True
            continue
        if in_block:
            members.append(s)
            continue
        m = _WORK_SINGLE.match(s)
        if m:
            members.append(m.group(1).strip())
    return tuple(members)


@dataclass(frozen=True)
class Closure:
    packages: dict[str, str]  # {module: version}; working map (not hashed)
    source: str  # workspace | vendor | gomod-pruned | resolve-required
    go_version: str
    toolchain: str | None
    replace_local: tuple[str, ...]  # module keys dropped due to a local replace
    direct: int
    indirect: int
    resolve_required: bool


def _resolve_required(gm: "GoMod") -> Closure:
    return Closure(
        packages={},
        source="resolve-required",
        go_version=gm.go_version,
        toolchain=gm.toolchain,
        replace_local=(),
        direct=0,
        indirect=0,
        resolve_required=True,
    )


def _semver_key(v: str) -> tuple:
    """Sort key for a module version. Numeric core first (`v1.10.0` > `v1.2.0`),
    then the raw string as a deterministic tie-break for pseudo/pre-release forms."""
    core = v.lstrip("v").split("-")[0].split("+")[0]
    nums: list[int] = []
    for p in core.split("."):
        if p.isdigit():
            nums.append(int(p))
        else:
            break
    return (tuple(nums), v)


def _max_version(a: str, b: str) -> str:
    return a if _semver_key(a) >= _semver_key(b) else b


def _finalize(gm: "GoMod", pkgs: dict[str, str], source: str) -> Closure:
    """Apply replace/exclude, drop the main module, count direct/indirect."""
    # `exclude` FORBIDS a version and affects MVS SELECTION, which happens BEFORE
    # `replace` substitution. So excludes must be checked against the AS-SELECTED
    # versions (`pkgs` as passed in, before any replace mutates it) -- otherwise an
    # unconditional `replace X => Y vN` could rewrite X's version first and mask an
    # exclude that should taint the closure. MVS then selects the next version if
    # excluded; we cannot compute that offline, so an exclude of the SELECTED
    # version taints to resolve-required. An exclude of any other version is a no-op.
    for e in gm.excludes:
        if pkgs.get(e.path) == e.version:
            return _resolve_required(gm)
    replace_local: list[str] = []
    for r in gm.replaces:
        # Honor an old-version constraint: `replace X vOld => ...` applies ONLY when
        # the selected version of X is vOld. `replace X => ...` (no old version) always applies.
        if r.old_version is not None and pkgs.get(r.old_path) != r.old_version:
            continue
        if (
            r.new_version is None
        ):  # local filesystem replace -> drop (target deps invisible offline)
            if pkgs.pop(r.old_path, None) is not None:
                replace_local.append(r.old_path)
        elif (
            r.old_path in pkgs
        ):  # registry replace -> rewrite version, keep old key (matches `go list`)
            pkgs[r.old_path] = r.new_version
    pkgs.pop(gm.module_path, None)
    return Closure(
        packages=pkgs,
        source=source,
        go_version=gm.go_version,
        toolchain=gm.toolchain,
        replace_local=tuple(replace_local),
        direct=sum(1 for r in gm.requires if not r.indirect),
        indirect=sum(1 for r in gm.requires if r.indirect),
        resolve_required=False,
    )


def _work_has_replace(work_path: Path) -> bool:
    """True if go.work carries a `replace` directive (single-line or block open)."""
    for raw in work_path.read_text().splitlines():
        s = _strip_comment(raw)[0].strip()
        if s.startswith("replace"):
            return True
    return False


def _workspace_closure(repo: Path, members: tuple[str, ...]) -> Closure:
    """One global MVS across all members: the MAX version of each module wins
    (not last-write). A workspace-level `replace` directive taints to
    resolve-required (spec §3.1); `go.work.sum` is not inspected in slice 1."""
    if _work_has_replace(repo / "go.work"):
        return _resolve_required(GoMod("", ""))
    merged: dict[str, str] = {}
    replace_local: list[str] = []
    go_version = ""
    for rel in members:
        member = (repo / rel).resolve()
        if not (member / "go.mod").is_file():
            return _resolve_required(GoMod("", go_version))  # missing member taints
        sub = _member_closure(member)
        if sub.resolve_required:
            return _resolve_required(GoMod("", sub.go_version))
        for mod, ver in sub.packages.items():
            merged[mod] = _max_version(merged[mod], ver) if mod in merged else ver
        replace_local.extend(sub.replace_local)
        go_version = go_version or sub.go_version
    for rel in members:  # a member is never an external dep of another
        member = (repo / rel).resolve()
        merged.pop(parse_go_mod(member / "go.mod").module_path, None)
    return Closure(
        packages=merged,
        source="workspace",
        go_version=go_version,
        toolchain=None,
        replace_local=tuple(replace_local),
        direct=0,
        indirect=0,
        resolve_required=False,
    )


def _member_closure(repo: Path) -> Closure:
    """Non-workspace closure for a single module dir (the go.mod ladder only).
    Used directly and for each go.work member, so a `use .` member cannot re-enter
    the workspace branch and infinite-recurse."""
    gm = parse_go_mod(repo / "go.mod")
    vendor = repo / "vendor" / "modules.txt"
    if vendor.is_file():
        return _finalize(gm, parse_vendor_modules_txt(vendor), "vendor")
    if _go_version_tuple(gm.go_version) >= (1, 17):
        return _finalize(gm, {r.path: r.version for r in gm.requires}, "gomod-pruned")
    return _resolve_required(gm)


def module_closure(repo_dir: str | Path) -> Closure:
    """Offline ``{module: version}`` closure via the authority ladder
    (go.work -> vendor -> gomod-pruned -> resolve-required). Spec §3.1."""
    repo = Path(repo_dir)
    if (repo / "go.work").is_file():
        return _workspace_closure(repo, parse_go_work(repo / "go.work"))
    return _member_closure(repo)
