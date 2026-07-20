# Held-out oracle for the pgconfig_psycopg2 edge case.
# Known-answer: building psycopg2 (source dist) from sdist needs a C
# compiler plus libpq-dev, which supplies both the pg_config binary and
# the libpq headers/shared library required at build time.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
