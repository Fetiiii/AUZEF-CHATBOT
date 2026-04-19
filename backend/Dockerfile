# ─── Build Stage ───────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

# Sistem bağımlılıkları (psycopg2 için)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Önce sadece requirements — layer cache için
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sonra kaynak kod
COPY . .

# ─── Runtime ───────────────────────────────────────────────────────────────────
EXPOSE 8000

# sentence-transformers model cache'i için volume mount noktası
# (docker-compose.yml'de volume tanımlanır)
ENV HF_HOME=/app/.cache/huggingface

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
