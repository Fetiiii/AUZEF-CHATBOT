#!/bin/sh
set -e

echo "⏳ Veritabanı tabloları kontrol ediliyor..."
python -c "from scripts.init_system import setup; setup()"

echo "🚀 Backend başlatılıyor..."
# --workers 2: embedding modeli HER worker'a ayrı yüklenir (yüzlerce MB - GB);
#   4 worker 4G bellek limitini zorlar. Endpoint'ler sync def olduğundan her
#   worker ~40 istegi threadpool'da eszamanli isler — 2 worker fazlasiyla yeter.
# --proxy-headers: nginx'in gönderdiği X-Forwarded-For'dan GERÇEK istemci IP'sini
#   al (yoksa tüm loglara proxy IP'si yazılır). Backend host'a yayınlanmadığı için
#   (docker-compose'da ports yok) forwarded-allow-ips="*" güvenlidir: bu porta
#   yalnızca Docker ağı içindeki nginx erişebilir.
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2 \
    --proxy-headers --forwarded-allow-ips="*"
