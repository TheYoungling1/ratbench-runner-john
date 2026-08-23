"""Triggering import for the pil_pillow edge case.

The import name (`PIL`) differs from the installable PyPI distribution
name (`Pillow`). A graph that resolves import names literally -- e.g.
looking for a package named `PIL` or `image` -- will fail to find the
correct pip dependency and drop it from the PACKAGE tier.
"""

from PIL import Image

print(Image.__name__)
