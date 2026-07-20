# Held-out oracle for the marker_target_vs_host edge case.
# Known-answer: both markers evaluate TRUE against this target (linux,
# Python 3.10 pinned by .python-version), so both pyinotify and tomli
# must be installed -- even though the host building the graph is
# macOS on a possibly different Python minor.
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
