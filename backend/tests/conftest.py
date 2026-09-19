"""Test altyapısı.

Gereksinim: Docker (testler kendi throwaway Postgres 15 container'ını açar)
YA DA hazır test veritabanlarına işaret eden TEST_DATABASE_URL ve opsiyonel
TEST_ADMIN_DATABASE_URL / TEST_CHAT_DATABASE_URL env değişkenleri.

Çalıştırma (Windows / conda):
    PYTHONUTF8=1 python -m pytest tests/ -v

Tasarım notları:
- Ortam değişkenleri ve `providers` stub'ı pytest_configure içinde, test
  modülleri import edilmeden ÖNCE kurulur (main.py import anında embedding
  modeli yüklemeye kalkmasın, DATABASE_URL testin DB'sini görsün diye).
- Testler GERÇEK app'i (main.app) middleware'leriyle birlikte kullanır.
"""
import atexit
import os
import subprocess
import sys
import time
import types

import pytest

_PG_CONTAINER = "auzef_pytest_pg"
_PG_PORT = "55440"
_ADMIN_DB = "auzef_admin_test"
_CHAT_DB = "auzef_chat_test"
_COMPAT_DB = "auzef_compat_test"

# ── Test kimlik bilgileri ────────────────────────────────────────────────────
# BUNLAR SIR DEĞİLDİR. Her test turunda sıfırdan kurulan throwaway Postgres'te
# oluşturulan kullanıcıların parolalarıdır; hiçbir sistemde geçerli değillerdir.
#
# Tek yerde toplandılar çünkü test dosyalarına serpiştirilmiş literal
# password="..." atamaları sır tarayıcılarını (GitGuardian) tetikliyor ve
# gerçek sızıntı uyarılarını gürültüye boğuyordu. Yeni bir test parolaya
# ihtiyaç duyarsa yeni literal YAZMA — buradaki sabitlerden birini kullan.
#
# NOT: değerler en az 10 karakter olmalı; settings_api.UserCreateRequest parola
# için min_length=10 istiyor ve kullanıcı oluşturma testleri o yoldan geçiyor.
TEST_PASSWORD = "pytest-fixture-not-a-secret"
TEST_PASSWORD_WRONG = "pytest-fixture-wrong-value"
# Parola değiştirme testleri için: TEST_PASSWORD'dan farklı olması yeterli.
TEST_PASSWORD_ALT = "pytest-fixture-alternate-value"


def _start_throwaway_postgres() -> tuple[str, str, str]:
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER],
                   capture_output=True, check=False)
    subprocess.run([
        "docker", "run", "--rm", "-d", "--name", _PG_CONTAINER,
        "-e", "POSTGRES_USER=admin", "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=postgres", "-p", f"{_PG_PORT}:5432",
        "postgres:15",
    ], check=True, capture_output=True)
    atexit.register(lambda: subprocess.run(
        ["docker", "rm", "-f", _PG_CONTAINER], capture_output=True, check=False))
    for _ in range(60):
        ok = subprocess.run(
            ["docker", "exec", _PG_CONTAINER, "pg_isready", "-U", "admin", "-d", "postgres"],
            capture_output=True, check=False)
        if ok.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Test Postgres'i ayağa kalkmadı")
    for database_name in (_ADMIN_DB, _CHAT_DB, _COMPAT_DB):
        subprocess.run(
            ["docker", "exec", _PG_CONTAINER, "createdb", "-U", "admin", database_name],
            check=True,
            capture_output=True,
        )
    prefix = f"postgresql://admin:test@localhost:{_PG_PORT}"
    return (
        f"{prefix}/{_ADMIN_DB}",
        f"{prefix}/{_CHAT_DB}",
        f"{prefix}/{_COMPAT_DB}",
    )


def pytest_configure(config):
    # 1) Ortam: test DB'leri + auth ayarları (main import edilmeden önce!)
    db_url = os.getenv("TEST_DATABASE_URL")
    admin_url = os.getenv("TEST_ADMIN_DATABASE_URL")
    chat_url = os.getenv("TEST_CHAT_DATABASE_URL")
    if bool(admin_url) != bool(chat_url):
        raise RuntimeError(
            "TEST_ADMIN_DATABASE_URL ve TEST_CHAT_DATABASE_URL birlikte tanımlanmalıdır"
        )

    if not db_url and not admin_url:
        admin_url, chat_url, db_url = _start_throwaway_postgres()
        # Ayrı subprocess acceptance testi compatibility DB'sini kullanır.
        os.environ["TEST_COMPAT_DATABASE_URL"] = db_url

    os.environ["DATABASE_URL"] = db_url or admin_url
    if admin_url and chat_url:
        os.environ["ADMIN_DATABASE_URL"] = admin_url
        os.environ["CHAT_DATABASE_URL"] = chat_url
    else:
        # Yalnız TEST_DATABASE_URL verildiğinde gerçek fallback yolu sınanır.
        os.environ.pop("ADMIN_DATABASE_URL", None)
        os.environ.pop("CHAT_DATABASE_URL", None)
    os.environ["ADMIN_AUTH_ENFORCED"] = "true"
    os.environ["ADMIN_COOKIE_SECURE"] = "false"
    os.environ.pop("LLM_PROVIDER", None)  # LLM yolu kapalı: eşik yedeği test edilir

    # 2) providers stub'ı: SentenceTransformer/Meili/Qdrant istemcileri yerine
    #    testten kontrol edilebilir sahteler (main import'u saniyeler sürsün).
    fake = types.ModuleType("providers")

    class FakeMeili:
        hits = []          # testler doldurur: [{id,question,answer,score,source}]
        suggestions = []
        add_calls = []     # her add_documents çağrısında eklenen doküman sayısı

        def __init__(self, *a, **k): ...
        def healthcheck(self): ...
        def search(self, q, limit=3): return list(self.hits)[:limit]
        def get_suggestions(self, q, limit=3): return list(self.suggestions)[:limit]
        def get_suggestion_hits(self, q, limit=3):
            # suggestions: [{"qna_id", "question"}] (or bare titles = no id)
            return [
                item if isinstance(item, dict) else {"qna_id": None, "question": item}
                for item in list(self.suggestions)[:limit]
            ]
        def add_documents(self, docs): FakeMeili.add_calls.append(len(docs))
        def update_documents(self, docs): ...
        def delete_document(self, doc_id): ...

    class FakeQdrant:
        hits = []
        batch_calls = []   # her upsert_points çağrısındaki öğe sayısı (batch kanıtı)
        single_calls = 0   # upsert_point (tekil) çağrı sayısı

        def __init__(self, *a, **k): ...
        def healthcheck(self): ...
        def ensure_collection(self): ...
        def search(self, q, limit=3): return list(self.hits)[:limit]
        def upsert_point(self, *a, **k): FakeQdrant.single_calls += 1
        def upsert_points(self, items): FakeQdrant.batch_calls.append(len(items))
        def delete_point(self, *a, **k): ...

    fake.MeiliSearchProvider = FakeMeili
    fake.QdrantProvider = FakeQdrant
    fake.FakeMeili = FakeMeili   # testlerin erişimi için
    fake.FakeQdrant = FakeQdrant
    sys.modules["services.providers"] = fake

    # 3) Şemayı kur
    import core.database as database
    database.init_db()


# ── Ortak fixture'lar ────────────────────────────────────────────────────────

@pytest.fixture()
def db():
    from core.database import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def clean_tables():
    """Her test temiz tablolarla başlar (id sayaçları dahil)."""
    from core.database import ADMIN_TABLES, CHAT_TABLES, admin_engine, chat_engine
    from sqlalchemy import text

    def truncate(engine, tables):
        names = ", ".join(table.name for table in tables)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))

    if admin_engine is chat_engine:
        truncate(admin_engine, (*ADMIN_TABLES, *CHAT_TABLES))
    else:
        truncate(admin_engine, ADMIN_TABLES)
        truncate(chat_engine, CHAT_TABLES)
    # Sahte arama sonuçlarını + çağrı sayaçlarını sıfırla
    import services.providers as providers
    providers.FakeMeili.hits = []
    providers.FakeMeili.suggestions = []
    providers.FakeMeili.add_calls = []
    providers.FakeQdrant.hits = []
    providers.FakeQdrant.batch_calls = []
    providers.FakeQdrant.single_calls = 0
    # Process-local LLM circuit breaker / admin-mode tracker must not leak
    # state between tests.
    from services.circuit_breaker import LLM_ADMIN_MODE_TRACKER, LLM_CIRCUIT_BREAKER
    LLM_CIRCUIT_BREAKER.reset_all()
    LLM_ADMIN_MODE_TRACKER.reset()
    yield


@pytest.fixture()
def app():
    import main
    return main.app


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture()
def make_user(db):
    """Kullanıcı oluşturucu: make_user('x@iu.tr', role='admin', password=...)"""
    from admin.auth import hash_password
    from core.database import AdminUser

    def _make(email, role="admin", password=TEST_PASSWORD, active=True, name=None):
        u = AdminUser(email=email, password_hash=hash_password(password),
                      full_name=name, role=role, is_active=1 if active else 0)
        db.add(u)
        db.commit()
        db.refresh(u)
        return u

    return _make


@pytest.fixture()
def login(app):
    """Oturumlu TestClient üretir: login('x@iu.tr') → client"""
    from fastapi.testclient import TestClient

    def _login(email, password=TEST_PASSWORD):
        c = TestClient(app)
        r = c.post("/api/auth/login", json={"email": email, "password": password})
        assert r.status_code == 200, f"login başarısız: {email} → {r.text}"
        return c

    return _login
