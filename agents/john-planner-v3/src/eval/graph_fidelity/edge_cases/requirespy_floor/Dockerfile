# Held-out oracle for the requirespy_floor edge case.
# Known-answer: the correct runtime is the PIN (.python-version = 3.12),
# not the FLOOR declared by requires-python (>=3.9). Building on the
# floor (e.g. python:3.9-slim) would break `import tomllib`.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
COPY main.py .
RUN pip install --no-cache-dir .

CMD ["python", "main.py"]
