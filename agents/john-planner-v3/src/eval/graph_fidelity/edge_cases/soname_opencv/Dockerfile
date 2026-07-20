# Held-out oracle for the soname_opencv edge case.
# Known-answer: opencv-python's compiled cv2 extension needs libGL.so.1
# and libglib2.0 at runtime -- neither is present on python:*-slim, and
# pip cannot install them (they are not PyPI packages).
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
