# Held-out oracle for the extras_closure edge case.
# Known-answer: the `socks` extra on requests must expand to its
# transitive dependency PySocks -- there is no separate apt/system
# requirement, this is purely a PACKAGE-tier closure-expansion case.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
