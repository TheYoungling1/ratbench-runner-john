# Held-out oracle for the subprocess_cli_tools edge case.
# Known-answer: the code shells out to adb, git and sqlite3 — external CLI tools
# that a graph built only from pip metadata + ldd never discovers. They must be
# apt-installed for the code to run.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    adb \
    git \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install -e .

CMD ["python", "-c", "import subprocess_cli_tools"]
