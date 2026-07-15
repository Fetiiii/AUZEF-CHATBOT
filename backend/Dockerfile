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
# --timeout/--retries: torch gibi ~GB'lik paketlerde tek bir ağ kesintisi
# 25+ dakikalık build'i baştan başlatmasın (yaşandı: pip urllib3 hatasıyla öldü).
RUN pip install --no-cache-dir --timeout 120 --retries 10 -r requirements.txt

# Sonra kaynak kod
COPY . .

# Windows'ta kayıt edilen .sh script'i Linux'ta çalışabilen hale getir
RUN sed -i 's/\r$//' entrypoint.sh && chmod +x entrypoint.sh

# ─── Güvenlik: non-root kullanıcı ─────────────────────────────────────────────
# /app/flags: bakım bayrağı volume'unun mount noktası. Named volume ilk
# oluşturulduğunda sahiplik/izinler bu imaj dizininden kopyalanır; 0777
# olmalı ki hem appuser (backend) hem nginx container'ı yazabilsin.
RUN adduser --disabled-password --no-create-home --gecos "" appuser \
    && mkdir -p /app/.cache/huggingface /app/flags \
    && chown -R appuser:appuser /app \
    && chmod 0777 /app/flags

USER appuser

# ─── Runtime ───────────────────────────────────────────────────────────────────
EXPOSE 8000

# sentence-transformers model cache'i için volume mount noktası
ENV HF_HOME=/app/.cache/huggingface

# Production: 4 worker (CPU sayısına göre ayarlayın)
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
