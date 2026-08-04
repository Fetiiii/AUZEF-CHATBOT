# AUZEF Akıllı Asistan 🤖

AUZEF öğrencilerinin sorularını yanıtlamak için geliştirilmiş **hibrit arama asistanı**: MeiliSearch (anahtar kelime) + Qdrant (semantik vektör) + LLM (RAG yedeği) zinciriyle çalışır. Öğrenciler ayrıca sohbet içinden AUZEF **Çözüm Merkezi**'ne destek talebi oluşturabilir.

Öğrenci arayüzü, herhangi bir AUZEF sayfasına gömülebilen bir **widget**'tır; yönetim ise oturum korumalı bir **Angular paneli** üzerinden yapılır.

> 📘 Bu belge kurulum + geliştirme + genel bakış içindir. **Production deploy, bakım modu, rollback ve rol yönetimi** için ayrıntılı runbook: [`DEPLOY.md`](DEPLOY.md).

---

## ✨ Özellikler

- **Hibrit arama:** Meili (anahtar kelime) + Qdrant (semantik) + LLM seçici/RAG yedeği. LLM kapalıysa sistem eşik yedeğiyle çalışmaya devam eder.
- **Akademik takvim yanıtları:** Tarih/dönem sorularına takvim verisinden yanıt.
- **Çözüm Merkezi talep akışı:** Sohbet içinden TC → SMS/OTP → öğrenci → kategori → talep (config'e bağlı; kapalıyken harici sayfaya yönlendirir).
- **Yönetim paneli (rol tabanlı):** QnA yönetimi, akademik takvim, konuşma kayıtları, istatistikler, ayarlar — `editor` / `admin` / `super_admin` yetki matrisiyle.
- **Konuşma kaydı + geri bildirim:** Sohbetler kaydedilir; cevaplara yıldız puanı ve düşük puanda talep akışı sunulur.
- **Bakım modu:** Panelden ya da `bakim.sh` ile widget kapatılabilir; backend çökükse nginx otomatik "bakımdayız" yanıtı döner.
- **Gömülebilir widget:** Tek `<script>` satırıyla herhangi bir sayfaya eklenir.

---

## 🏗️ Mimari

```
Tarayıcı ──► Frontend (nginx :80/:443)
                │  /api/*, /widget-chat, /health  → proxy
                ▼
         FastAPI Backend (:8000, 2 worker — dışarı YAYINLANMAZ)
         ├── PostgreSQL   (:5432)  — Ana veri tabanı
         ├── MeiliSearch  (:7700)  — Anahtar kelime araması
         ├── Qdrant       (:6333)  — Semantik vektör araması (embedding modeli backend'de)
         ├── LLM (OpenRouter / OpenAI / Gemini) — RAG / seçici yedeği
         └── Çözüm Merkezi API (dış)  — Destek talebi oluşturma (opsiyonel, config'e bağlı)
```

> **Güvenlik:** Dahili servisler (DB, MeiliSearch, Qdrant) ve backend dış dünyaya **kapalıdır** — yalnızca Docker ağı içinden erişilir. Dışarıya yalnızca frontend (nginx, 80/443) açıktır; tüm istekler nginx üzerinden geçer (TLS, güvenlik başlıkları, rate limit, oturum koruması).

**Teknoloji:** FastAPI (Python 3.11) · Angular 19 + nginx · PostgreSQL 15 · MeiliSearch v1.12 · Qdrant v1.13.2 · sentence-transformers (`turkce-embedding-bge-m3`).

---

## ✅ Ön Koşullar

| Araç | Versiyon | İndirme |
|---|---|---|
| Docker Desktop | 4.x | https://www.docker.com/products/docker-desktop |
| Git | herhangi | https://git-scm.com |

**Python, Node.js veya başka bir şey kurmanıza gerek yok** — her şey Docker içinde çalışır. (İsteğe bağlı Docker'sız geliştirme için aşağıya bakın.)

---

## 🚀 Hızlı Başlangıç (Docker, 3 Adım)

### 1. Repoyu klonlayın
```bash
git clone <repo-url>
cd "AUZEF CHATBOT"
```

### 2. Ortam değişkenlerini ayarlayın
```bash
cp .env.example .env
```
`.env` içinde en azından şunları doldurun (tam liste için [Yapılandırma](#-yapılandırma-env)):
- **Önerilen:** `OPENROUTER_API_KEY` (LLM seçici için; boşsa eşik yedeğiyle çalışır)
- **Production'da zorunlu:** `POSTGRES_PASSWORD` ve `MEILI_MASTER_KEY` güçlü değerlerle değiştirilmeli
- **Opsiyonel:** `CM_SERVICE_TOKEN` (Çözüm Merkezi talep entegrasyonu; bkz. [ilgili bölüm](#-çözüm-merkezi-talep-entegrasyonu))

### 3. Sistemi başlatın
```bash
docker compose up -d
```
> ⏳ İlk çalıştırmada Docker image'ları ve embedding modeli (~1.5 GB) indirilir — **yalnızca bir kez**. Backend açılışta veritabanı tablolarını, view'ı ve index'leri **otomatik** oluşturur (elle migration yok).

---

## 🗄️ İlk Veri Yükleme

Tablolar otomatik oluşur; sadece içeriği (QnA verisi) yüklemeniz gerekir:

```bash
# 1. CSV verisini PostgreSQL + MeiliSearch'e aktar
docker compose exec backend python -m scripts.importer

# 2. Vektörleri Qdrant'a yükle (semantik arama için)
docker compose exec backend python -m scripts.vector_sync
```

CSV dosyaları [`data/`](data/) klasöründedir. (Diğer scriptler için [Scriptler](#-scriptler) bölümüne bakın.)

---

## 🌐 Erişim Adresleri

Öğrenci deneyimi bir **sayfa değil, widget'tır** — AUZEF sayfalarına gömülür (bkz. [Widget Gömme](#-widget-gömme)). `/` kökü **yönetim paneline** (giriş) yönlenir.

| Ne | URL (yerel) | Erişim |
|---|---|---|
| **Yönetim paneli — giriş** | http://localhost/chatbot/sign-in | Personel |
| — QnA & Belge yükleme | `/chatbot/document-upload` | editor+ |
| — Akademik takvim | `/chatbot/academic-calendar` | editor+ |
| — Veri görüntüleme | `/chatbot/view-data` | admin+ |
| — Konuşmalar | `/chatbot/conversations` | admin+ |
| — Ayarlar (kullanıcı / LLM / API anahtarı) | `/chatbot/settings` | super_admin |
| **Öğrenci chatbot** | Widget (`/widget.js`, sayfaya gömülür) | Herkes |
| Sağlık kontrolü | http://localhost/health | `{"ok":true}` |

> **API dokümanı (Swagger):** Backend dış dünyaya yayınlanmadığından ve FastAPI docs'u `/api/` altında olmadığından, Swagger UI nginx üzerinden erişilemez. Yalnızca backend'i **doğrudan** (uvicorn) çalıştırdığınızda `http://localhost:8000/docs` adresinde görünür.
>
> **Not:** `pgAdmin` ve `MeiliSearch` arayüzleri production'da kapalıdır; yalnızca dev profilinde açılır (bkz. [Yerel Geliştirme](#-yerel-geliştirme)).

---

## 🧩 Widget Gömme

Widget, frontend tarafından `/widget.js` adresinde servis edilir. Herhangi bir sayfaya tek satırla eklenir:

```html
<script src="https://auzefasistan.istanbul.edu.tr/widget.js" defer></script>
```

API adresi, script'in yüklendiği origin'den **otomatik** çözülür. İsteğe bağlı `data-*` öznitelikleriyle özelleştirilebilir:

| Öznitelik | Varsayılan | Açıklama |
|---|---|---|
| `data-api-url` | script origin + `/widget-chat` | Backend API adresini elle belirtir |
| `data-position` | `right` | Widget konumu: `left` / `right` |
| `data-solution-url` | Çözüm Merkezi giriş sayfası | Talep akışı kapalıyken/başarısızken açılacak harici sayfa |
| `data-nav-mode` | `external` | Hızlı-erişim bağlantılarının açılma modu: `external` / `internal` |

```html
<script src="https://auzefasistan.istanbul.edu.tr/widget.js"
        data-position="left"
        data-solution-url="https://cozummerkeziauzef.istanbul.edu.tr/student/sign-in"
        defer></script>
```

> `widget.js` sabit adla ama değişen içerikle servis edilir; nginx onu `no-cache` ile döner, böylece yeni sürüm tarayıcıya hemen ulaşır.

---

## ⚙️ Yapılandırma (`.env`)

Tüm değişkenler [`.env.example`](.env.example) içinde şablonludur. `docker-compose.yml` bunları backend container'ına `env_file` ile geçirir.

### LLM Sağlayıcısı
| Değişken | Açıklama |
|---|---|
| `LLM_PROVIDER` | `openrouter` (önerilen) / `openai` / `gemini`. Boşsa LLM yolu kapalı, eşik yedeğiyle çalışır. |
| `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | Seçilen sağlayıcının anahtarı. OpenRouter anahtarı panelden de girilebilir (DB'deki `.env`'i ezer, restart gerektirmez). |

### Veritabanı & Servisler
| Değişken | Açıklama |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | PostgreSQL. **Production'da güçlü şifre.** |
| `MEILI_MASTER_KEY` | MeiliSearch. **Production'da güçlü değer.** |
| `PGADMIN_EMAIL` / `PGADMIN_PASSWORD` | pgAdmin (yalnızca dev profili). |
| `CIRCUIT_BREAKER_TIME` | Meili çökükse kaç sn atlanacağı (varsayılan 30). |

### Yönetim Oturumu
| Değişken | Sunucu | Yerel |
|---|---|---|
| `ADMIN_AUTH_ENFORCED` | yazmayın (varsayılan `true` = kilitli) | `false` (panel kilidi kapalı) |
| `ADMIN_COOKIE_SECURE` | yazmayın (varsayılan `true`) | `false` (http'de cookie çalışsın) |

> Güvenli varsayılan: `ADMIN_AUTH_ENFORCED` hiç yoksa sistem **kilitli** çalışır.

### Çözüm Merkezi (opsiyonel)
| Değişken | Açıklama |
|---|---|
| `CM_BASE_URL` | Çözüm Merkezi API kökü (`https://service-cozummerkeziauzef.istanbul.edu.tr`). |
| `CM_SERVICE_TOKEN` | API anahtarı (önek OLMADAN). **Boşsa entegrasyon kapalı.** |
| `CM_AUTH_SCHEME` | Header şeması, varsayılan `Api-Key` → `Authorization: Api-Key <token>`. |
| `CM_CHANNEL_SHORTCODE` | Kanal kodu, varsayılan `AUZEF_WEB_SAYFASI_CHATBOT`. |
| `CM_TIMEOUT` / `CM_MAX_RETRIES` / `CM_VERIFICATION_TTL_MIN` / `CM_CATEGORY_CACHE_TTL` | Opsiyonel ince ayar (10sn / 2 / 10dk / 900sn). |

> ⚠️ **`.env` değişikliğini uygulamak için** `restart` YETMEZ — `env_file` değişiklikleri yalnızca container yeniden **oluşturulunca** okunur:
> ```bash
> docker compose up -d backend        # gerekirse: --force-recreate backend
> ```

---

## 🎫 Çözüm Merkezi (Talep) Entegrasyonu

Öğrenciler sohbet içinden AUZEF Çözüm Merkezi'ne destek talebi oluşturabilir:
**TC → SMS/OTP doğrulama → öğrenci seçimi → kategori → talep**. Chatbot yalnızca istemcidir; tüm iş mantığı Çözüm Merkezi API'si üzerinden ilerler.

- **Config kapalıyken** (`CM_BASE_URL` veya `CM_SERVICE_TOKEN` boş) `/api/solution-center/*` uçları nazikçe `503` döner ve widget kullanıcıyı harici Çözüm Merkezi sayfasına yönlendirir — yani entegrasyon olmadan da sistem sorunsuz çalışır.
- `verificationToken` / TC / OTP **asla** yanıtta veya loglarda görünmez; akış durumu sunucuda (`solution_center_sessions` tablosu) tutulur.
- Backend kodu: [`backend/integrations/solution_center/`](backend/integrations/solution_center/) (katmanlı, async, DI'lı; `client` yalnızca HTTP, `service` yalnızca iş mantığı).
- Uçlar: `POST /api/solution-center/{send-sms, verify-otp, select-student, categories, select-category, create-ticket}`.

---

## 💻 Yerel Geliştirme

### Docker ile (önerilen)

Dev override dosyası ([`docker-compose.dev.yml`](docker-compose.dev.yml)) HTTP-only nginx (SSL sertifikası gerekmez) + Angular canlı-yeniden-yükleme (`ng serve`) sağlar:

```bash
# Tüm sistemi dev modunda başlat (nginx :80 + hot-reload :4200)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# pgAdmin gerekiyorsa (yalnızca dev)
docker compose --profile dev up -d pgadmin

# Kapat (veriler korunur)
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

| Servis | URL |
|---|---|
| Frontend — canlı yeniden yükleme (`ng serve`) | http://localhost:4200 |
| Frontend — statik nginx (built SPA) | http://localhost |
| pgAdmin (dev profili) | http://localhost:5050 |

> Yerelde panele girebilmek için `.env`'e `ADMIN_AUTH_ENFORCED=false` ekleyin (dev override zaten `ADMIN_COOKIE_SECURE=false` yapar). Alternatif: kilidi açık bırakıp `docker exec -it auzef_backend python -m scripts.create_admin <email>` ile yerel kullanıcı oluşturun.

### Docker'sız (yalnızca backend/frontend geliştirme)

Altyapı servislerini Docker'da, backend/frontend'i lokalde çalıştırmak için:

```bash
# 1. Altyapıyı aç
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db meilisearch qdrant

# 2. Backend (yeni terminal)
cd backend
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000             # Swagger: http://localhost:8000/docs

# 3. Frontend (yeni terminal)
cd chatbot-web
npm install --legacy-peer-deps
npm start                                          # http://localhost:4200
```

---

## 🛑 Sistemi Durdurma / Yeniden Başlatma

```bash
docker compose down                 # Durdur (veriler korunur)
docker compose down -v              # Durdur + TÜM verileri sil (sıfırdan)
docker compose restart backend      # Backend'i yeniden başlat (kod/env değişmediyse)
docker compose up -d backend        # .env / kod değiştiyse: container'ı yeniden oluştur
docker compose logs -f backend      # Logları takip et
```

---

## 🧪 Testler

Backend'in kalıcı test paketi [`backend/tests/`](backend/tests/) altındadır (oturum, rol matrisi, ayarlar, konuşma sahiplik token'ları, istatistikler, Çözüm Merkezi 9 senaryosu, saf yardımcılar).

```bash
cd backend
pip install -r requirements-dev.txt        # pytest + httpx (bir kez)
python -m pytest tests/ -v                 # Docker gerekir: testler kendi throwaway Postgres'ini açar
# Hazır bir test DB'siyle:
TEST_DATABASE_URL=postgresql://... python -m pytest tests/ -v
```

Testler gerçek uygulamayı (middleware dahil) gerçek Postgres'e karşı çalıştırır; arama/embedding sağlayıcıları stub'lanır. **Backend'e dokunan her değişiklikten sonra suite'i çalıştırın.**

---

## 🚢 Deploy & Operasyon

Ayrıntılı, otoriter runbook: **[`DEPLOY.md`](DEPLOY.md)** (rutin deploy, bakım modu, rollback, rol yönetimi, go-live doğrulaması). Özet:

```bash
# Rutin deploy (çalışan siteyi etkilemez; ~2-3 dk geçiş)
git pull
docker compose build backend frontend
docker compose up -d
curl -s https://auzefasistan.istanbul.edu.tr/health   # → {"ok":true}
```

- **Veri asla sıfırlanmaz:** PostgreSQL/Meili/Qdrant verileri Docker volume'larında yaşar; deploy yalnızca kodu değiştirir.
- **Migration yok:** backend açılışta tabloları/view/index'leri idempotent oluşturur.
- **SSL:** production nginx `./ssl/certs/{fullchain,privkey}.pem` bekler (443). Dev'de `nginx.dev.conf` HTTP-only çalışır.

### 🛠️ Bakım Modu

Widget'ı kapatır (öğrenci "bakımdayız" görür); panel ve `/api/` açık kalır.

```bash
./bakim.sh          # etkileşimli menü (durum + seçimler)
./bakim.sh on       # widget'ı kapat
./bakim.sh status   # kim/ne zaman açtı
./bakim.sh off      # tekrar yayına al
```

Backend çökükse nginx widget'a **otomatik** bakım yanıtı döner (kimsenin açması gerekmez).

### 👤 Kullanıcılar & Roller

| Sayfa / API | editor | admin | super_admin |
|---|---|---|---|
| QnA + Akademik Takvim + Import/Export | ✅ | ✅ | ✅ |
| Konuşmalar + İstatistikler | ❌ | ✅ | ✅ |
| Ayarlar (kullanıcılar, LLM, API anahtarı) | ❌ | ❌ | ✅ |

Gündelik kullanıcı yönetimi **panelden** (Ayarlar, super_admin) yapılır. CLI yalnızca ilk kurulum/acil durum içindir:

```bash
# İlk super_admin (parola gizli sorulur, ≥10 karakter)
docker exec -it auzef_backend python -m scripts.create_admin ad.soyad@istanbul.edu.tr --name "Ad Soyad"
```

Ayrıntı (rol yükseltme, pasifleştirme, rollback, LLM anahtarı yönetimi) → [`DEPLOY.md`](DEPLOY.md).

---

## 📁 Proje Yapısı

```
AUZEF CHATBOT/
├── backend/                    # FastAPI uygulaması (Python 3.11)
│   ├── main.py                 # Uygulama montajı (uvicorn main:app)
│   ├── core/                   # Altyapı: database.py (modeller), deps.py (DB/sağlayıcı/LLM)
│   ├── services/               # İş mantığı: answer_pipeline, providers (Meili+Qdrant),
│   │                           #   llm_provider, calendar_utils, csv_utils
│   ├── admin/                  # Oturum + ayarlar: auth.py, settings_api.py
│   ├── integrations/           # Dış entegrasyonlar: solution_center/ (Çözüm Merkezi talep API'si)
│   ├── routers/                # API uçları: chat, conversations, qna, stats, calendar, solution_center
│   ├── scripts/                # CLI: importer, vector_sync, init_system, create_admin, import_etiya_history
│   ├── tests/                  # pytest paketi
│   ├── Dockerfile · entrypoint.sh
│   └── requirements*.txt
├── chatbot-web/                # Angular 19 frontend + gömülü widget
│   ├── src/app/                # Yönetim paneli (routes: app.routes.ts)
│   ├── src/widget.js           # Gömülebilir öğrenci widget'ı (/widget.js)
│   ├── nginx.conf              # Production (443/SSL, rate limit, bakım)
│   ├── nginx.dev.conf          # Geliştirme (HTTP-only, 80)
│   └── Dockerfile
├── data/                       # CSV veri dosyaları
├── ssl/certs/                  # Production TLS sertifikaları (fullchain/privkey)
├── docker-compose.yml          # Tüm servisler (production)
├── docker-compose.dev.yml      # Dev override (HTTP nginx + ng serve hot-reload)
├── bakim.sh                    # Bakım modu (acil durum CLI)
├── .env.example                # Ortam değişkenleri şablonu
├── DEPLOY.md                   # Deploy / operasyon runbook'u
└── SPEC.md                     # Çözüm Merkezi entegrasyon spesifikasyonu
```

---

## 📜 Scriptler

`docker compose exec backend python -m scripts.<ad>` ile çalıştırılır:

| Script | Görev |
|---|---|
| `importer` | CSV → PostgreSQL + MeiliSearch (QnA verisi). |
| `vector_sync` | QnA → Qdrant vektörleri (semantik arama). |
| `init_system` | Tablolar/view/index + başlangıç config (boot'ta `entrypoint.sh` çağırır). |
| `create_admin` | Admin kullanıcısı oluştur/güncelle/pasifleştir (bkz. `--role`, `--deactivate`). |
| `import_etiya_history` | Etiya chatbot geçmişini `conversations` / `conversation_messages`'a aktarır. |
