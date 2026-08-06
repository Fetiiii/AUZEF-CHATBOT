"""Test altyapısı.

Gereksinim: Docker (testler kendi throwaway Postgres 15 container'ını açar)
YA DA hazır bir test veritabanına işaret eden TEST_DATABASE_URL env değişkeni.

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


def _start_throwaway_postgres() -> str:
    subprocess.run(["docker", "rm", "-f", _PG_CONTAINER],
                   capture_output=True, check=False)
    subprocess.run([
        "docker", "run", "--rm", "-d", "--name", _PG_CONTAINER,
        "-e", "POSTGRES_USER=admin", "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=auzef_test", "-p", f"{_PG_PORT}:5432",
        "postgres:15",
    ], check=True, capture_output=True)
    atexit.register(lambda: subprocess.run(
        ["docker", "rm", "-f", _PG_CONTAINER], capture_output=True, check=False))
    for _ in range(60):
        ok = subprocess.run(
            ["docker", "exec", _PG_CONTAINER, "pg_isready", "-U", "admin", "-d", "auzef_test"],
            capture_output=True, check=False)
        if ok.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError("Test Postgres'i ayağa kalkmadı")
    return f"postgresql://admin:test@localhost:{_PG_PORT}/auzef_test"


def pytest_configure(config):
    # 1) Ortam: test DB'si + auth ayarları (main import edilmeden önce!)
    db_url = os.getenv("TEST_DATABASE_URL")
    if not db_url:
        db_url = _start_throwaway_postgres()
    os.environ["DATABASE_URL"] = db_url
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
        def search(self, q, limit=3): return list(self.hits)[:limit]
        def get_suggestions(self, q, limit=3): return list(self.suggestions)[:limit]
        def add_documents(self, docs): FakeMeili.add_calls.append(len(docs))
        def update_documents(self, docs): ...
        def delete_document(self, doc_id): ...

    class FakeQdrant:
        hits = []
        batch_calls = []   # her upsert_points çağrısındaki öğe sayısı (batch kanıtı)
        single_calls = 0   # upsert_point (tekil) çağrı sayısı

        def __init__(self, *a, **k): ...
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
    from core.database import engine
    from sqlalchemy import text
    with engine.connect() as conn:
        # sc_rate_limits, admin_login_attempts: hız sınırı sayaçları. Listede
        # olmazlarsa bir test diğerinin sayacını devralır ve testler SIRAYA
        # BAĞLI olarak patlar. academic_calendar: takvim kayıtları da
        # testler arasında sızıyordu.
        conn.execute(text("""
            TRUNCATE admin_sessions, admin_users, conversation_messages,
                     conversations, query_logs, system_config,
                     qna_queries, qna_tags, tags, qna,
                     sc_rate_limits, academic_calendar, admin_login_attempts
            RESTART IDENTITY CASCADE
        """))
        conn.commit()
    # Sahte arama sonuçlarını + çağrı sayaçlarını sıfırla
    import services.providers as providers
    providers.FakeMeili.hits = []
    providers.FakeMeili.suggestions = []
    providers.FakeMeili.add_calls = []
    providers.FakeQdrant.hits = []
    providers.FakeQdrant.batch_calls = []
    providers.FakeQdrant.single_calls = 0
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
