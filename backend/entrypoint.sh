#!/bin/sh
set -e

echo "⏳ Veritabanı tabloları kontrol ediliyor..."
python -c "from init_system import setup; setup()"

echo "🚀 Backend başlatılıyor..."
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
