"""Triggering import for the srclayout_editable edge case.

The package lives under src/srclayout_editable/ (src-layout), so `import
srclayout_editable` resolves ONLY when the repo is editable-installed
(`pip install -e .`). A dependency graph that installs the pinned closure but
omits the project's own editable install (finding A) fails here at runtime.
"""

import srclayout_editable

print(srclayout_editable.hello())
