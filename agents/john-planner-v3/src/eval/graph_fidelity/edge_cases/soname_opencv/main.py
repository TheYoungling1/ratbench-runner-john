"""Triggering import for the soname_opencv edge case.

opencv-python ships a compiled cv2 extension linked against libGL.so.1
(and transitively libglib2.0). On a lean -slim base these shared objects
are absent unless the apt packages libgl1 (+ libglib2.0-0) are installed
first -- pip alone cannot supply them, so `import cv2` fails at runtime
even though `pip install opencv-python` succeeded.
"""

import cv2

print(cv2.__version__)
