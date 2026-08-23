# Held-out oracle for the pil_pillow edge case.
# Known-answer: `from PIL import Image` requires the pip package
# `Pillow` -- not a package literally named `PIL` or `image`. Pillow
# ships manylinux wheels, so no apt packages are needed here.
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "main.py"]
