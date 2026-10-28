# Stages: base (system packages, PDF.js, Python deps) -> app (the default; what compose runs)
# and base -> e2e-deps -> e2e (adds Playwright + Chromium for the browser tests; only built on request).
FROM python:3.13-slim AS base

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# The prebuilt PDF.js viewer, served at /pdfjs for the book page's embedded reader.
ARG PDFJS_VERSION=6.4.299
ARG PDFJS_SHA256=f2245eb7ef3f674c872fe568a177bfec15874f5c034613e55d8e307f93ebd86d
RUN python -c "import hashlib, io, sys, urllib.request, zipfile; \
data = urllib.request.urlopen('https://github.com/mozilla/pdf.js/releases/download/v${PDFJS_VERSION}/pdfjs-${PDFJS_VERSION}-dist.zip').read(); \
hashlib.sha256(data).hexdigest() == '${PDFJS_SHA256}' or sys.exit('PDF.js checksum mismatch'); \
zipfile.ZipFile(io.BytesIO(data)).extractall('/srv/pdfjs')"

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

ENV BOOKS_DIR=/books DATA_DIR=/data PDFJS_DIR=/srv/pdfjs PYTHONUNBUFFERED=1


# Browser tests: Playwright and its Chromium, before the code so a code change doesn't reinstall them.
FROM base AS e2e-deps
COPY requirements-e2e.txt ./
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir -r requirements-e2e.txt && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

FROM e2e-deps AS e2e
COPY app ./app
COPY tests ./tests
COPY pytest.ini ./
CMD ["pytest", "-q", "tests/e2e"]


FROM base AS app
COPY app ./app
COPY tests ./tests
COPY pytest.ini ./
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
