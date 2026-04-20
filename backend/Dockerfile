# ─── Build Stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# Sistem bağımlılıkları (psycopg2 için)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Önce sadece requirements — layer cache için
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sonra kaynak kod
COPY . .

# Windows'ta kayıt edilen .sh script'i Linux'ta çalışabilen hale getir
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# ─── Güvenlik: non-root kullanıcı ─────────────────────────────────────────────
RUN adduser --disabled-password --no-create-home --gecos "" appuser \
    && mkdir -p /app/.cache/huggingface \
    && chown -R appuser:appuser /app

USER appuser

# ─── Runtime ───────────────────────────────────────────────────────────────────
EXPOSE 8000

# sentence-transformers model cache'i için volume mount noktası
ENV HF_HOME=/app/.cache/huggingface

# Production: 4 worker (CPU sayısına göre ayarlayın)
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
