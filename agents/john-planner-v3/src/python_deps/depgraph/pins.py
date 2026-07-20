"""Derive an `exclude_newer` resolve cutoff from the project's pinned roots.

The uv resolver picks the *latest* version of any transitive dep an upstream
package leaves unbounded. That breaks ABI-pinned ecosystems: ``opencv-python==
4.9.0.80`` (Dec 2023) declares only ``numpy>=...``, so uv resolves ``numpy==2.4.6``
— incompatible with opencv 4.9's numpy-1.x C-ABI. The agent then has to discover
and downgrade numpy itself.

Fix: when the project explicitly pins a root (``name==version``), resolve the
*rest* of the closure as of that root's release era. We look up the pinned
versions' upload dates on PyPI and set ``exclude_newer`` to the newest of them
(+1 day, so the pinned release itself is included). This is the reproducible-build
default — "resolve transitive deps as of when the project's pins were chosen" —
and it only engages when a root is actually pinned (modern/unpinned repos get
``None`` and resolve latest, unchanged).

Pure logic + an injectable ``fetch`` (no network in unit tests). Degrades to
``None`` on any PyPI failure, so a resolve is never worse than today.
"""

from __future__ import annotations

import datetime
import json
import re
import urllib.request

_PIN_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._!+-]*)")


def parse_pinned_roots(roots: list[tuple[str | None, str]]) -> list[tuple[str, str]]:
    """Extract ``(name, version)`` from roots specced as ``name==version``.

    Unpinned roots (``opencv-python``, ``flask>=2``) are skipped — only an exact
    ``==`` pin is a deliberate version choice we anchor the cutoff to.
    """
    out: list[tuple[str, str]] = []
    for _import_id, spec in roots:
        match = _PIN_RE.match(spec or "")
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def _default_fetch(name: str, version: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
        return json.load(response)


def pypi_upload_date(name: str, version: str, fetch=_default_fetch) -> str | None:
    """The ``YYYY-MM-DD`` release date of ``name==version`` (earliest uploaded
    file), or ``None`` on any failure (network, missing version, malformed)."""
    try:
        data = fetch(name, version)
        times = [
            u.get("upload_time")
            for u in (data.get("urls") or [])
            if u.get("upload_time")
        ]
        return min(times)[:10] if times else None
    except Exception:  # noqa: BLE001 — best-effort; never break the resolve
        return None


def _plus_one_day(date_str: str) -> str:
    day = datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)
    return day.isoformat()


def compute_exclude_newer(
    roots: list[tuple[str | None, str]], fetch=_default_fetch
) -> str | None:
    """An ``exclude_newer`` cutoff (``YYYY-MM-DD``) from the pinned roots' newest
    release date (+1 day), or ``None`` when no root is pinned / no date resolves.
    """
    dates = [
        date
        for name, version in parse_pinned_roots(roots)
        if (date := pypi_upload_date(name, version, fetch))
    ]
    if not dates:
        return None
    return _plus_one_day(max(dates))
