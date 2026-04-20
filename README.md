# AUZEF Akıllı Asistan 🤖

AUZEF öğrencilerinin sorularını yanıtlamak için geliştirilmiş, MeiliSearch + Qdrant + LLM (RAG) zincirli hibrit arama asistanı.

---

## 🏗️ Mimari

```
Tarayıcı → Frontend (nginx :80)
                │  /api/* proxy
                ▼
         FastAPI Backend (:8000)
         ├── MeiliSearch  (:7700)  — Anahtar kelime araması
         ├── Qdrant        (:6333)  — Semantik vektör araması
         ├── PostgreSQL   (:5432)  — Ana veri tabanı
         └── LLM (OpenRouter/OpenAI/Gemini) — RAG fallback
```

> **Not:** Dahili servisler (DB, MeiliSearch, Qdrant) dış dünyaya kapalıdır — sadece Docker ağı içinden erişilebilir. Yalnızca frontend (port 80) dışarıya açıktır.

---

## ✅ Ön Koşullar

| Araç | Versiyon | İndirme |
|---|---|---|
| Docker Desktop | 4.x | https://www.docker.com/products/docker-desktop |
| Git | herhangi | https://git-scm.com |

**Python, Node.js veya başka bir şey kurmanıza gerek yok.**

---

## 🚀 Kurulum (3 Adım)

### 1. Repoyu klonlayın
```bash
git clone <repo-url>
cd "AUZEF CHATBOT"
```

### 2. Ortam değişkenlerini ayarlayın
```bash
cp .env.example .env
```
`.env` dosyasını açıp şunları doldurun:
- **Zorunlu:** En az bir LLM API anahtarı (`OPENROUTER_API_KEY` önerilir)
- **Production'da zorunlu:** `POSTGRES_PASSWORD` ve `MEILI_MASTER_KEY` güçlü şifrelerle değiştirilmeli

```env
OPENROUTER_API_KEY=sk-or-...          # openrouter.ai'dan ücretsiz alınabilir
POSTGRES_PASSWORD=guclu_bir_sifre     # Production'da mutlaka değiştirin!
MEILI_MASTER_KEY=guclu_bir_key        # Production'da mutlaka değiştirin!
```

### 3. Sistemi başlatın
```bash
docker compose up -d
```
> ⏳ İlk çalıştırmada Docker image'ları ve embedding modeli (~1.5 GB) indirilecektir. Bu **yalnızca bir kez** olur.

---

## 🗄️ İlk Veri Yükleme

Sistem ilk kez ayağa kalktığında veritabanı boş gelir. Sırasıyla şu komutları çalıştırın:

```bash
# 1. Veritabanı tablolarını ve sistem ayarlarını oluştur
docker compose exec backend python init_system.py

# 2. CSV verisini PostgreSQL + MeiliSearch'e aktar
docker compose exec backend python importer.py

# 3. Vektörleri Qdrant'a yükle (semantic search için)
docker compose exec backend python vector_sync.py
```

---

## 🌐 Erişim Adresleri

| Servis | URL | Kullanım |
|---|---|---|
| **Chatbot** | http://localhost/chat | Öğrenci arayüzü |
| **Admin Panel** | http://localhost/chatbot/sign-in | Yönetim (fb/1) |
| **Veri Yönetimi** | http://localhost/chatbot/document-upload | QnA + LLM switch |
| **API Docs** | http://localhost/api/docs | Swagger UI (nginx üzerinden) |

> pgAdmin ve MeiliSearch arayüzleri production'da kapalıdır. Geliştirme modunda açılır (aşağıya bakın).

---

## 🛑 Sistemi Durdurma / Yeniden Başlatma

```bash
# Durdur (veriler korunur)
docker compose down

# Durdur ve tüm verileri sil (sıfırdan başla)
docker compose down -v

# Sadece backend'i yeniden başlat
docker compose restart backend

# Log'ları takip et
docker compose logs -f backend
```

---

## 💻 Geliştirme Modu (Lokal)

Docker olmadan geliştirmek için:

```bash
# 1. Altyapı servislerini + pgAdmin'i aç (dev profili)
docker compose --profile dev up -d db meilisearch qdrant pgadmin

# 2. Backend'i lokalde başlat
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
uvicorn main:app --reload --port 8000

# 3. Frontend'i lokalde başlat (yeni terminal)
cd chatbot-web
npm install
npm start
# → http://localhost:4200
```

**Geliştirme modu erişim adresleri:**
| Servis | URL |
|---|---|
| Frontend (ng serve) | http://localhost:4200 |
| Backend (uvicorn) | http://localhost:8000 |
| pgAdmin | http://localhost:5050 |
| MeiliSearch | http://localhost:7700 |

---

## ⚙️ LLM Sağlayıcı Değiştirme

`.env` dosyasında `LLM_PROVIDER` değerini değiştirin:

```env
LLM_PROVIDER=openrouter   # openrouter.ai (önerilen, ücretsiz tier var)
LLM_PROVIDER=openai       # OpenAI GPT
LLM_PROVIDER=gemini       # Google Gemini
```

Değiştirdikten sonra backend'i yeniden başlatın:
```bash
docker compose restart backend
```

---

## 📁 Proje Yapısı

```
AUZEF CHATBOT/
├── backend/              # FastAPI uygulaması
│   ├── main.py           # API endpoint'leri
│   ├── database.py       # SQLAlchemy modeller
│   ├── providers.py      # MeiliSearch + Qdrant
│   ├── llm_provider.py   # LLM sağlayıcıları
│   ├── importer.py       # CSV → DB aktarımı
│   ├── vector_sync.py    # DB → Qdrant vektör senkronizasyonu
│   ├── init_system.py    # İlk kurulum
│   └── Dockerfile
├── chatbot-web/          # Angular 19 frontend
│   ├── src/
│   ├── Dockerfile
│   └── nginx.conf
├── data/                 # CSV veri dosyaları
├── docker-compose.yml    # Tüm servisler
├── .env.example          # Ortam değişkenleri şablonu
└── requirements.txt      # Python bağımlılıkları
```
