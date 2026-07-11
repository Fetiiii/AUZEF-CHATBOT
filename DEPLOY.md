# 🚀 Deploy Rehberi — AUZEF Akıllı Asistan

Bu belge, sunucuda (auzefasistan.istanbul.edu.tr) yeni sürüm yayınlamanın
tamamını kapsar: rutin deploy, ilk kurulum sonrası go-live, admin kullanıcı
yönetimi ve geri alma (rollback).

> **Özet:** Rutin bir deploy = `git pull` → `build` → `up -d` → `/health` kontrolü.
> Gerisi ya tek seferlik ya da sorun çıkarsa başvurulan adımlardır.

---

## 📦 Rutin Deploy (her sürümde)

```bash
cd /path/to/AUZEF-CHATBOT

# 1) (Önerilen) Yedek: veri + geri alınabilir imaj
docker exec auzef_db pg_dump -U admin auzef_bot > ~/auzef_backup_$(date +%Y%m%d).sql
docker tag auzefchatbot-backend:latest  auzefchatbot-backend:onceki
docker tag auzefchatbot-frontend:latest auzefchatbot-frontend:onceki

# 2) Kodu çek ve imajları oluştur (çalışan siteyi ETKİLEMEZ, kesinti yok)
git pull
docker compose build backend frontend
#   * yalnızca backend değiştiyse:  docker compose build backend
#   * yalnızca frontend/nginx ise:  docker compose build frontend
#   * emin değilseniz ikisini de build edin — güvenli, sadece yavaş.

# 3) Geçiş (~2-3 dk kesinti: backend embedding modelini yeniden yükler)
docker compose up -d
docker ps        # auzef_backend "healthy" olana kadar bekleyin

# 4) Doğrulama
curl -s https://auzefasistan.istanbul.edu.tr/health      # → {"ok":true}
```

**Notlar**
- Veri asla sıfırlanmaz: PostgreSQL / MeiliSearch / Qdrant verileri Docker
  volume'larında yaşar; deploy yalnızca kodu değiştirir.
- DB migration'ları elle çalıştırılmaz: backend açılışta tabloları, view'ı ve
  index'leri kendisi oluşturur/günceller (idempotent).
- Build sırasında ağ kesilirse komutu tekrar çalıştırın — kaldığı katmandan
  devam eder (pip timeout/retry ayarlı).

---

## 🔙 Rollback (deploy sorun çıkarırsa, ~1 dk)

```bash
docker tag auzefchatbot-backend:onceki  auzefchatbot-backend:latest
docker tag auzefchatbot-frontend:onceki auzefchatbot-frontend:latest
docker compose up -d --force-recreate backend frontend
```

Veri geri yüklemek gerekirse (nadiren):

```bash
cat ~/auzef_backup_YYYYMMDD.sql | docker exec -i auzef_db psql -U admin -d auzef_bot
```

---

## 👤 Admin Kullanıcı Yönetimi (gerektikçe)

Yönetim paneline giriş, oturum tabanlı kimlik doğrulamayla korunur
(`backend/auth.py`). Kullanıcılar sunucuda CLI ile yönetilir:

```bash
# Yeni personel (parola gizli sorulur, en az 10 karakter)
docker exec -it auzef_backend python create_admin.py ad.soyad@istanbul.edu.tr --name "Ad Soyad"

# Parola sıfırlama = aynı komut (mevcut e-posta ile çalıştırın;
# eski oturumları da güvenlik gereği kapatır)
docker exec -it auzef_backend python create_admin.py ad.soyad@istanbul.edu.tr

# Personel ayrıldı → erişimi ANINDA kapat (açık oturumları da siler)
docker exec -it auzef_backend python create_admin.py ad.soyad@istanbul.edu.tr --deactivate
```

---

## 🏁 İlk Go-Live Doğrulaması (tek seferlik yapıldıysa atlayın)

Auth sistemli ilk deploy'dan sonra bir kez çalıştırılır:

```bash
# Yönetim uçları kilitli mi?
curl -s -o /dev/null -w "%{http_code}\n" https://auzefasistan.istanbul.edu.tr/api/qna
# → 401 olmalı

# Backend porta dışarıdan erişim kapalı mı?
curl -s -o /dev/null --connect-timeout 5 http://SUNUCU_IP:8000/health ; echo $?
# → bağlantı reddi / timeout olmalı (0 OLMAMALI)
```

Sonra tarayıcıdan: widget'a soru sorun (öğrenci yolu) ve panele giriş yapın
(personel yolu). İlk go-live'da ayrıca `docker compose up -d --remove-orphans`
kullanın (eski pgAdmin container'ını kaldırır — pgAdmin artık yalnızca
geliştirme profilindedir).

---

## ⚙️ Ortam Değişkenleri (.env)

| Değişken | Sunucuda | Yerel geliştirmede |
|---|---|---|
| `ADMIN_AUTH_ENFORCED` | yazmayın (varsayılan `true`) | `false` (panel kilidi kapalı) |
| `ADMIN_COOKIE_SECURE` | yazmayın (varsayılan `true`) | `false` (http'de cookie çalışsın) |
| `POSTGRES_PASSWORD`, `MEILI_MASTER_KEY` | güçlü, benzersiz değerler | varsayılan olabilir |
| `OPENROUTER_API_KEY` vb. | LLM seçici için gerekli | opsiyonel (eşik yedeğiyle çalışır) |

Güvenli varsayılan: `ADMIN_AUTH_ENFORCED` env'de hiç yoksa sistem **kilitli**
çalışır — yanlışlıkla açık kalamaz.

---

## 💻 Yerel Geliştirme / Test

```bash
# HTTP-only dev nginx ile (SSL sertifikası gerekmez, port 80)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# pgAdmin gerekiyorsa (yalnızca dev)
docker compose --profile dev up -d pgadmin

# Kapatma (volume'lar/veri kalır)
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

Yerel `.env`'e `ADMIN_AUTH_ENFORCED=false` ve `ADMIN_COOKIE_SECURE=false`
eklemeyi unutmayın (ya da kilidi test etmek için `ENFORCED=true` bırakıp
`create_admin.py` ile yerel kullanıcı oluşturun).

---

## 🗺️ Hangi adım ne sıklıkla?

| Adım | Sıklık |
|---|---|
| `git pull` + `build` + `up -d` | **Her deploy** |
| pg_dump + imaj tag yedeği | Her deploy (önerilen, ~30 sn) |
| `/health` kontrolü | Her deploy |
| `--remove-orphans` | Tek seferlik (ilk go-live) |
| Admin kullanıcı oluşturma | Personel değişince |
| Tam doğrulama bataryası (401/port testleri) | İlk go-live + büyük altyapı değişiklikleri |
| Rollback | Yalnızca sorun çıkarsa |
