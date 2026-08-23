"""Triggering imports for the marker_target_vs_host edge case.

Both dependencies are conditional on environment markers that must be
evaluated against the TARGET container (linux, Python 3.10 pinned via
.python-version), not the host machine running construction (typically
macOS, on a different Python minor). A naive host-eval would wrongly
drop pyinotify (host sys_platform == "darwin") and possibly drop tomli
too (if the host's own interpreter is >= 3.11).
"""

import sys

if sys.platform == "linux":
    import pyinotify  # noqa: F401  (sys_platform == "linux")

if sys.version_info < (3, 11):
    import tomli  # noqa: F401  (python_version < "3.11")
else:
    import tomllib  # noqa: F401
