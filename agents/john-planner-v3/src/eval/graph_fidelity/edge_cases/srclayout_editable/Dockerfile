# Held-out oracle for the srclayout_editable edge case.
# Known-answer: a src-layout project is importable only after the repo itself is
# installed editable; installing just its dependency (click) is not enough.
FROM python:3.11-slim

WORKDIR /app
COPY . .
RUN pip install -e .

CMD ["python", "main.py"]
