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

# 3) Geçiş (~2-3 dk kesinti: backend embedding modelini yeniden yükler;
#    bu pencerede öğrenci otomatik olarak "bakımdayız" mesajı görür)
docker compose up -d
docker ps        # auzef_backend "healthy" olana kadar bekleyin

# 4) Doğrulama
curl -s https://auzefasistan.istanbul.edu.tr/health      # → {"ok":true}
#   nginx'i devre dışı bırakarak (container içinden) kontrol etmek isterseniz:
#   docker exec auzef_backend wget -qO- http://localhost:8000/health
```

**Notlar**
- Veri asla sıfırlanmaz: PostgreSQL / MeiliSearch / Qdrant verileri Docker
  volume'larında yaşar; deploy yalnızca kodu değiştirir.
- DB migration'ları elle çalıştırılmaz: backend açılışta tabloları, view'ı ve
  index'leri kendisi oluşturur/günceller (idempotent).
- Build sırasında ağ kesilirse komutu tekrar çalıştırın — kaldığı katmandan
  devam eder (pip timeout/retry ayarlı).

---

## 🛠️ Bakım Modu

Bakım modu yalnızca **widget'ı** kapatır (öğrenci "bakımdayız" mesajı görür);
admin paneli ve `/api/` açık kalır, yani bakımı panelden geri kapatabilirsiniz.
Bayrak `ops_flags` volume'unda bir dosyadır — deploy/restart onu silmez.

Üç katman:

1. **Planlı bakım (normal yol):** Panel → Ayarlar → *Bakım Modu* düğmesi
   (yalnızca super_admin). Aç → işini yap → kapat.
2. **Otomatik:** Backend'e ulaşılamıyorsa (çökme, deploy sırasındaki 2-3 dk'lık
   model yüklemesi) nginx widget'a kendiliğinden aynı bakım yanıtını döner —
   kimsenin bir şey açması gerekmez (`nginx.conf` → `error_page 502 503 504`).
3. **Acil durum (backend/panel çalışmıyorken):** sunucuda SSH ile:

```bash
./bakim.sh          # ETKİLEŞİMLİ MENÜ: sistem durumu özeti (hangi container
                    # ayakta, /health, bakım modu) + numaralı seçimler —
                    # projeyi bilmeyen biri için bu yeterli
./bakim.sh on       # widget'ı kapat
./bakim.sh status   # kim/ne zaman açmış
./bakim.sh off      # tekrar yayına al
```

Script bayrağı nginx container'ı üzerinden yönetir; backend'e hiç dokunmaz.
Doğrulama: bakım açıkken `curl -s -o /dev/null -w "%{http_code}\n"
-X POST https://auzefasistan.istanbul.edu.tr/widget-chat` → `503` olmalı.

> Sunucu TAMAMEN çökmüşse (elektrik/disk): bu mekanizmaların hiçbiri çalışmaz;
> widget host sayfada görünmez olur. Bu senaryo için dış uptime izleme +
> kurumla konuşulacak durum sayfası ayrı bir iştir.

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

## 👤 Kullanıcılar ve Roller

Üç rol vardır (yetki matrisi):

| Sayfa / API | editor | admin | super_admin |
|---|---|---|---|
| QnA + Akademik Takvim + Import/Export | ✅ | ✅ | ✅ |
| Konuşmalar + İstatistikler | ❌ | ✅ | ✅ |
| Ayarlar (kullanıcılar, LLM, API anahtarı) | ❌ | ❌ | ✅ |

**Gündelik kullanıcı yönetimi panelden yapılır:** Ayarlar sayfası (yalnızca
super_admin) kullanıcı ekleme, rol değiştirme, parola sıfırlama ve
pasifleştirme işlemlerinin tamamını içerir. CLI yalnızca BOOTSTRAP ve acil
durum içindir:

```bash
# İlk super_admin'i oluştur (parola gizli sorulur, en az 10 karakter)
docker exec -it auzef_backend python -m scripts.create_admin ad.soyad@istanbul.edu.tr --name "Ad Soyad"
# (CLI'dan yeni kullanıcının varsayılan rolü super_admin'dir)

# Var olan kullanıcıyı super_admin'e yükselt — parola sorulduğunda BOŞ
# bırakırsanız parola DEĞİŞMEZ:
docker exec -it auzef_backend python -m scripts.create_admin ad.soyad@istanbul.edu.tr --role super_admin

# Acil durum: erişimi ANINDA kapat (açık oturumları da siler)
docker exec -it auzef_backend python -m scripts.create_admin ad.soyad@istanbul.edu.tr --deactivate
```

**Rol sistemi migration notu (tek seferlik):** rol sisteminden önce açılmış
kullanıcılar ilk boot'ta otomatik olarak `admin` rolü alır (kaybettikleri tek
şey yeni Ayarlar sayfasıdır). Deploy'dan sonra kendi hesabınızı yukarıdaki
komutla super_admin'e yükseltin.

**LLM ayarları:** LLM aç/kapa ve OpenRouter API anahtarı artık Ayarlar
sayfasındadır. Panelden girilen anahtar DB'de tutulur ve `.env`'dekini ezer;
değişiklik deploy/restart GEREKTİRMEZ (tüm worker'lara bir sonraki istekte
yansır). Panel anahtarı silinirse `.env`'e geri dönülür.

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

## 🧪 Otomatik Testler

Backend'in kalıcı test paketi `backend/tests/` altındadır: oturum sistemi,
rol matrisi, ayarlar API'si, konuşma sahiplik token'ları (S5), istatistik
agregasyonu ve saf yardımcı fonksiyonlar.

```bash
cd backend
pip install -r requirements-dev.txt        # pytest + httpx (bir kez)
python -m pytest tests/ -v                 # Docker gerekir: testler kendi
                                           # throwaway Postgres'ini açar/kapatır
# Hazır bir test DB'si kullanmak isterseniz:
TEST_DATABASE_URL=postgresql://... python -m pytest tests/ -v
```

Testler gerçek uygulamayı (middleware dahil) gerçek Postgres'e karşı çalıştırır;
arama/embedding sağlayıcıları stub'lanır (ağ/model gerekmez). **Backend'e
dokunan her değişiklikten sonra suite'i çalıştırın.**

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
`python -m scripts.create_admin` ile yerel kullanıcı oluşturun).

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
