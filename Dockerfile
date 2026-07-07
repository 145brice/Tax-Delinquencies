# Railway build. Nixpacks (the default) installs the `playwright` pip package
# but never downloads the Chromium binary or its system libraries, so any
# browser-based scraper dies with "Chromium distribution 'chrome' is not found".
# A Debian base lets `playwright install --with-deps` pull the browser AND the
# apt system libs it needs.
FROM python:3.12-slim

WORKDIR /app

# System deps for the non-browser scrapers:
#   poppler-utils -> pdf2image, tesseract-ocr -> pytesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + all the shared libraries it needs, matched to the installed
# playwright version. Runs apt-get under the hood (root in the build = fine).
RUN python -m playwright install --with-deps chromium

COPY . .

# app.py reads PORT from the environment; Railway injects it at runtime.
ENV PORT=8095
CMD ["python", "app.py"]
