"""Triggering import for the extras_closure edge case.

requests[socks] declares the `socks` extra, whose closure pulls in the
separate PySocks distribution (import name `socks`). A graph that
resolves only the base `requests` package and drops the bracketed
extra will never add PySocks, so `import socks` fails at runtime even
though `pip install requests[socks]` reports success.
"""

import requests
import socks  # noqa: F401  (provided transitively by requests[socks])

print(requests.__version__)
