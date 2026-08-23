"""Triggering import for the nowheel_buildessential edge case.

numpy==1.21.0 predates Python 3.11/3.12 and ships no manylinux wheel for
the pinned target runtime (see .python-version = 3.12). pip must fall
back to the sdist and compile it, which requires a C toolchain
(build-essential) on the image -- a plain pip install with no compiler
present fails with "error: command 'gcc' failed" (or similar).
"""

import numpy

print(numpy.__version__)
