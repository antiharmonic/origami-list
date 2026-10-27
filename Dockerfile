FROM python:3.13-slim

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

COPY app ./app
COPY tests ./tests
COPY pytest.ini ./

ENV BOOKS_DIR=/books DATA_DIR=/data PDFJS_DIR=/srv/pdfjs PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
