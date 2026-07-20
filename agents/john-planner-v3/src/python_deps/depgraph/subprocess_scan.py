"""Static Tool-tier discovery: the external CLI binaries a repo shells out to.

Pure (no Executor, no network): walks the repo on disk, AST-parses each in-scope
``.py`` file, and records every ``subprocess.run/Popen/call/check_call/
check_output(...)``, ``os.system(...)`` and ``shutil.which(...)`` whose invoked
program (``argv[0]``) is a KNOWN external tool (``tables.CLI_TOOL_TO_APT``).

These are the standalone command-line programs the repo's OWN code invokes at run
time (adb, git, sqlite3, java, …). ``ldd_probe`` finds shared LIBRARIES linked
into compiled extensions and the pip build path finds compilers, but neither ever
sees a tool the code merely ``subprocess``-es — this scanner closes that gap.

Strict allowlist: only tools in ``CLI_TOOL_TO_APT`` are emitted, so a variable
command, a project-local script, a shell builtin, or a coreutil is never flagged
(false-positive safety). Mirrors ``config_scan``'s directory-exclusion scope so
examples/docs/build don't leak. Repo immutability: every "mutation" returns a
NEW ``DepGraph``.
"""

from __future__ import annotations

import ast
import os

from python_deps.depgraph.config_scan import _const_str, _is_excluded
from python_deps.depgraph.ids import TEST_NODE_ID, tool_id
from python_deps.depgraph.schema import (
    DepGraph,
    DiscoveredBy,
    Edge,
    EdgeType,
    Layer,
    Node,
    NodeType,
    State,
)
from python_deps.depgraph.tables import CLI_TOOL_TO_APT, apt_for_cli_tool

# Call targets whose first positional argument is a command line (list argv, or a
# shell string). ``os.system`` and ``shutil.which`` take a bare program string.
_ARGV_FUNCS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_STRING_FUNCS = frozenset({"system", "which"})


def _program_from_call(call: ast.Call) -> str | None:
    """The invoked program (``argv[0]``, path stripped) for a subprocess-like
    call, or None. Handles a list argv (``["adb", "devices"]``), a shell string
    (``"adb devices"``), and the bare-program forms (``os.system`` /
    ``shutil.which``). Only string LITERALS resolve — a variable command yields
    None (not statically known)."""
    func = call.func
    if isinstance(func, ast.Attribute):
        fname = func.attr
    elif isinstance(func, ast.Name):
        fname = func.id
    else:
        return None
    if fname not in _ARGV_FUNCS and fname not in _STRING_FUNCS:
        return None
    if not call.args:
        return None

    first = call.args[0]
    program: str | None = None
    if isinstance(first, ast.List):
        if first.elts:
            program = _const_str(first.elts[0])          # argv[0] of a list command
    else:
        literal = _const_str(first)
        if literal is not None:
            head = literal.strip().split()               # first token of a shell string
            program = head[0] if head else None
    if not program:
        return None
    return os.path.basename(program)                     # "/usr/bin/adb" -> "adb"


def _scan_source(src: str, rel: str, out: dict[str, str]) -> None:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        program = _program_from_call(node)
        if program is None or program not in CLI_TOOL_TO_APT or program in out:
            continue
        lineno = getattr(node, "lineno", 0)
        snippet = " ".join((lines[lineno - 1] if 0 < lineno <= len(lines) else "").split())
        out[program] = f"{rel}:{lineno}  {snippet}"[:200]


def scan_subprocess_tools(repo_path: str) -> dict[str, str]:
    """Map each allowlisted CLI tool the repo shells out to -> one-line evidence
    (file:line snippet). Only tools in ``CLI_TOOL_TO_APT`` appear."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if not _is_excluded(d)]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, repo_path)
            if _is_excluded(rel):
                continue
            try:
                with open(full, encoding="utf-8") as fh:
                    src = fh.read()
            except (OSError, UnicodeDecodeError):
                # UnicodeDecodeError is a ValueError, NOT an OSError: a single
                # non-UTF-8 file anywhere in the tree must be skipped, never
                # allowed to crash the whole graph build over a cosmetic gap.
                continue
            _scan_source(src, rel, out)
    return out


def _anchor_id(graph: DepGraph) -> str | None:
    """Node the discovered tools hang off: the repo's own code invokes them, so
    prefer the Project hub; fall back to the Test goal; None if neither exists."""
    proj = next((n for n in graph.nodes if n.type is NodeType.PROJECT), None)
    if proj is not None:
        return proj.id
    if graph.get(TEST_NODE_ID) is not None:
        return TEST_NODE_ID
    return None


def add_subprocess_tool_nodes(graph: DepGraph, repo_path: str) -> DepGraph:
    """Add a ``Tool`` node (``apt:`` fix, ``command -v`` check) for each
    allowlisted CLI binary the repo shells out to, with a ``requires`` edge from
    the Project (or Test) anchor. Returns a NEW graph; no-op when none found.

    The tool NAME is the node's canonical id (``tool:adb``) — the apt package is
    the fix, not the identity — mirroring ``ldd_probe``'s soname-is-the-id rule.
    """
    tools = scan_subprocess_tools(repo_path)
    if not tools:
        return graph
    anchor = _anchor_id(graph)
    new = graph
    for tool, evidence in sorted(tools.items()):
        apt = apt_for_cli_tool(tool)  # always set: tools come from the allowlist
        fix = f"apt:{apt}"
        new = new.with_node(
            Node(
                id=tool_id(tool),
                type=NodeType.TOOL,
                name=tool,
                layer=Layer.TOOLCHAIN,
                discovered_by=DiscoveredBy.STATIC_SCAN,
                state=State.UNKNOWN,
                check_command=f"command -v {tool}",
                fix_candidates=(fix,),
                chosen_fix=fix,
                evidence=evidence,
                provenance="subprocess-scan",
            )
        )
        if anchor is not None:
            new = new.with_edge(
                Edge(src=anchor, dst=tool_id(tool),
                     relation=EdgeType.REQUIRES, origin="subprocess-scan")
            )
    return new
