# Held-out oracle for the nowheel_buildessential edge case.
# Known-answer: numpy==1.21.0 has no wheel for the pinned target
# (Python 3.12), forcing an sdist build that needs build-essential
# (gcc/g++/make) -- the wheel-oracle prior (cluster 1a) should seed it.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
