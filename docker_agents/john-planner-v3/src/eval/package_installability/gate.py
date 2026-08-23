"""The 'does it work' gate (design §4): verify the installed package's TOP-LEVEL
import succeeds, plus an optional per-package tail snippet for the dlopen tail.

Why top-level import (not a deep submodule walk): a missing runtime system
library surfaces when the package's own compiled extension loads at import
(e.g. ``import psycopg2`` -> libpq, ``import pyodbc`` -> libodbc.so.2). Walking
every public submodule instead produces FALSE failures — optional integrations
import unrelated third-party packages or optional system libs that were never
part of the install (PIL.ImageTk -> libtk, lxml.html.soupparser -> bs4,
zmq.green -> gevent, plus shipped ``*.tests``). Build-time libs are caught at
install (an sdist build fails, rc != 0, before the gate); call-time dlopen libs
are caught by the tail snippet. ``import_name == ""`` means no importable module
(binary-only, e.g. uWSGI) -> the snippet is the gate."""
from __future__ import annotations

from dataclasses import dataclass

from python_deps.depgraph.executor import Executor

# python3 -c program: import the package; nonzero exit iff the import fails (a
# missing runtime system lib for the package's own extension surfaces here).
_IMPORT = (
    "import importlib, sys\n"
    "try:\n"
    "    importlib.import_module({mod!r})\n"
    "except Exception as e:\n"
    '    print("IMPORT_FAIL", repr(e)); sys.exit(1)\n'
    'print("IMPORT_OK")\n'
)


def import_command(import_name: str) -> str:
    """A shell command that imports ``import_name`` (rc0 == the package's
    top-level import, and thus its core compiled extension, loaded)."""
    prog = _IMPORT.format(mod=import_name)
    escaped = prog.replace("'", "'\"'\"'")
    return f"python3 -c '{escaped}'"


@dataclass(frozen=True)
class GateResult:
    deep_ok: bool          # the import gate passed (top-level import loaded)
    tail_ok: bool | None   # None when the spec has no tail_snippet


def run_gate(executor: Executor, spec, *, timeout: int = 300) -> GateResult:
    """Run the import gate then the tail snippet (if any) in ``executor``.

    A binary-only spec (``import_name == ""``) skips the import gate
    (deep_ok=True); its tail_snippet is the real gate. Any executor error
    surfaces as a failed bool, never an exception."""
    if spec.import_name:
        r = executor.run(import_command(spec.import_name), timeout=timeout)
        deep_ok = r.returncode == 0
    else:
        deep_ok = True

    tail_ok: bool | None = None
    if spec.tail_snippet:
        tail = executor.run(spec.tail_snippet, timeout=timeout)
        tail_ok = tail.returncode == 0
    return GateResult(deep_ok=deep_ok, tail_ok=tail_ok)
