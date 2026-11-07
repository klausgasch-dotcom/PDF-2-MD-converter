# Multi-Arch-Image: läuft auf x86_64-Servern und auf dem Raspberry Pi 5 (arm64).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMP_THREAD_LIMIT=2

# OCR-Werkzeuge: Tesseract (+ deutsche/englische Sprachdaten) und Ghostscript
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng ghostscript \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-web.txt ./
RUN pip install -r requirements-web.txt
COPY pdf2md.py hotfolder.py app.py ./

RUN useradd --system --uid 10001 --home-dir /app pdf2md
USER pdf2md

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,urllib.request; sys.exit(urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4).status != 200)"

# Nur EIN Worker: Batches werden im Speicher dieses Prozesses verwaltet.
# PORT wird von vielen Hostern (Render, Railway, Cloud Run, Fly.io) vorgegeben.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
