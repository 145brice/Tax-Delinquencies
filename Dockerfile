# Lightweight website/API image. Scheduled scraper jobs install and own their
# browser separately, then exit; Chromium is deliberately absent here.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# app.py reads PORT from the environment; Railway injects it at runtime.
ENV PORT=8095
CMD ["python", "serve.py"]
