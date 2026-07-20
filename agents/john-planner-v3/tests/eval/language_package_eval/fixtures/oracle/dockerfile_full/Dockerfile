FROM python:3.11.4-slim-bookworm AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        gcc \
        curl=7.88.1-10 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt \
        requests==2.31.0 \
        "flask>=2.0,<3.0" \
    && pip install -e .

CMD ["python", "-m", "myapp"]
