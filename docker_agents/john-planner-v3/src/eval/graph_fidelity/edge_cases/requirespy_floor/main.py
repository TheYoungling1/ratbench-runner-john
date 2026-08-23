"""Triggering import for the requirespy_floor edge case.

tomllib is stdlib only from Python 3.11 onward. `requires-python>=3.9`
in pyproject.toml is only a compatibility FLOOR (any minor >= 3.9 is
declared acceptable); `.python-version` = 3.12 pins the actual target
used for development/CI. A graph that selects the floor (3.9) instead
of the pin would make this import fail at runtime with
`ModuleNotFoundError: No module named 'tomllib'`.
"""

import tomllib

print(tomllib.__doc__)
