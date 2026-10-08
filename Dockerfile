# ImageToConfig — container image so the app runs on an always-on server
# (not tied to anyone's laptop). Build once, run anywhere Docker runs.
FROM python:3.12-slim

# Tesseract for local OCR/MICR (optional feature; app still runs without it).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better build caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Data (SQLite DB + images) lives here — mount a volume so it persists across
# restarts and redeploys.
VOLUME ["/app/data"]

EXPOSE 5000
ENV HOST=0.0.0.0 PORT=5000

# waitress (production WSGI server) via serve.py
CMD ["python", "serve.py"]
