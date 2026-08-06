"""Çözüm Merkezi entegrasyonu — SPEC "Test Senaryoları" (9 senaryo).

Ağ YOK: httpx.MockTransport DI ile enjekte edilir. Testler GERÇEK app'i
(main.app) middleware'leriyle kullanır; SC oturum durumu throwaway Postgres'te
tutulur (conftest). Kategori cache + config + client bağımlılıkları test başına
override edilir.

Kapsanan senaryolar:
  1) Geçerli TC            2) Geçersiz TC        3) Yanlış OTP
  4) Süresi dolmuş token   5) Çoklu öğrenci      6) Kategori bulunamadı
  7) Talep başarılı        8) API timeout        9) API 500
"""
import secrets
from datetime import timedelta

import httpx
import pytest

from integrations.solution_center import (
    get_category_cache,
    get_config,
    get_sc_client,
)
from integrations.solution_center.base_client import SolutionCenterConfig
from integrations.solution_center.category_cache import CategoryCache
from integrations.solution_center.client import SolutionCenterClient
from integrations.solution_center.constants import (
    PATH_CREATE_TICKET,
    PATH_GET_CATEGORIES,
    PATH_GET_PHONE,
    PATH_SEND_SMS,
    PATH_VERIFY_CODE,
)

ENABLED_CONFIG = SolutionCenterConfig(
    base_url="https://cm.test",
    service_token="svc-token",
    channel_short_code="AUZEF_WEB_SAYFASI_CHATBOT",
    timeout_seconds=5.0,
    max_retries=1,  # timeout/500 testleri hızlı bitsin
    verification_ttl_min=10,
    category_cache_ttl_seconds=900,
)


# ── Mock transport ───────────────────────────────────────────────────────────
# routes: path -> (status, body) | "timeout" | "connect"

def _default_routes() -> dict:
    return {
        PATH_GET_PHONE: (200, {"isSuccess": True, "code": 200,
                               "data": {"maskedPhoneNumber": "*******208"}}),
        PATH_SEND_SMS: (200, {"isSuccess": True, "code": 200,
                              "data": {"verificationToken": "tok-abc"}}),
        PATH_VERIFY_CODE: (200, {"isSuccess": True, "code": 200, "data": [
            {"ogrenciId": 649203, "birimAdi": "Birim", "fakulteAdi": "Fakülte"},
        ]}),
        PATH_GET_CATEGORIES: (200, {"isSuccess": True, "code": 200, "data": [
            {"uuid": "u1", "shortCode": "OGR_ISLERI", "name": "Öğrenci İşleri",
             "isLeaf": False, "sortOrder": 1, "parentUuid": None},
            {"uuid": "u2", "shortCode": "DERS_KAYDI", "name": "Ders Kaydı",
             "isLeaf": True, "sortOrder": 2, "parentUuid": "u1"},
        ]}),
        PATH_CREATE_TICKET: (200, {"isSuccess": True, "code": 200,
                                   "data": {"id": 2050995, "uuid": "BMGJRL"}}),
    }


def _make_transport(routes: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        spec = routes.get(request.url.path)
        if spec == "timeout":
            raise httpx.TimeoutException("mock timeout")
        if spec == "connect":
            raise httpx.ConnectError("mock connect")
        if spec is None:
            return httpx.Response(404, json={"isSuccess": False, "message": "no route"})
        status, body = spec
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


# ── DB yardımcıları (isteğin kendi oturumundan bağımsız okuma/yazma) ─────────

def _read_row(conv_id: int):
    from core.database import SessionLocal, SolutionCenterSession
    s = SessionLocal()
    try:
        row = s.query(SolutionCenterSession).filter_by(conversation_id=conv_id).first()
        if row is None:
            return None
        return {
            "state": row.state,
            "verification_token": row.verification_token,
            "selected_student_id": row.selected_student_id,
            "category_short_code": row.category_short_code,
            "expires_at": row.expires_at,
        }
    finally:
        s.close()


def _expire_row(conv_id: int):
    from core.database import SessionLocal, SolutionCenterSession, utcnow
    s = SessionLocal()
    try:
        row = s.query(SolutionCenterSession).filter_by(conversation_id=conv_id).first()
        row.expires_at = utcnow() - timedelta(minutes=1)
        s.commit()
    finally:
        s.close()


# ── Fixture: konuşma + override'lı TestClient üretici ────────────────────────

@pytest.fixture()
def sc(app, db):
    from core.database import Conversation
    conv = Conversation(client_token=secrets.token_urlsafe(24), ip_address="127.0.0.1")
    db.add(conv)
    db.commit()
    db.refresh(conv)

    from fastapi.testclient import TestClient

    def configure(routes=None):
        routes = routes if routes is not None else _default_routes()
        cache = CategoryCache(900)
        app.dependency_overrides[get_config] = lambda: ENABLED_CONFIG
        app.dependency_overrides[get_sc_client] = lambda: SolutionCenterClient(
            ENABLED_CONFIG, transport=_make_transport(routes))
        app.dependency_overrides[get_category_cache] = lambda: cache
        return TestClient(app)

    yield {"conv_id": conv.id, "token": conv.client_token, "configure": configure}

    for dep in (get_config, get_sc_client, get_category_cache):
        app.dependency_overrides.pop(dep, None)


def _body(sc, **extra) -> dict:
    return {"conversation_id": sc["conv_id"], "conversation_token": sc["token"], **extra}


# ── 1) Geçerli TC ────────────────────────────────────────────────────────────

def test_valid_tc_shows_phone_and_stores_token_server_side(sc):
    c = sc["configure"]()
    r = c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["masked_phone"] == "*******208"
    assert data["state"] == "SC_WAIT_OTP"
    # verificationToken ASLA yanıtta olmamalı (SPEC güvenlik kuralı)
    assert "tok-abc" not in r.text
    # ...ama sunucuda saklanmalı
    row = _read_row(sc["conv_id"])
    assert row["verification_token"] == "tok-abc"
    assert row["state"] == "SC_WAIT_OTP"


# ── 2) Geçersiz TC ───────────────────────────────────────────────────────────

def test_invalid_tc_returns_not_found(sc):
    routes = _default_routes()
    routes[PATH_GET_PHONE] = (200, {"isSuccess": False, "data": None, "message": "yok"})
    c = sc["configure"](routes)
    r = c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="00000000000"))
    assert r.status_code == 404
    assert "yok" not in r.text  # API mesajı sızmamalı


# ── 3) Yanlış OTP ────────────────────────────────────────────────────────────

def test_wrong_otp_rejected(sc):
    routes = _default_routes()
    routes[PATH_VERIFY_CODE] = (200, {"isSuccess": False, "data": None})
    c = sc["configure"](routes)
    assert c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111")).status_code == 200
    r = c.post("/api/solution-center/verify-otp", json=_body(sc, code="000000"))
    assert r.status_code == 400
    assert "doğrulama kodu hatalı" in r.json()["detail"].lower()


# ── 4) Süresi dolmuş verificationToken ───────────────────────────────────────

def test_expired_verification_token(sc):
    c = sc["configure"]()
    assert c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111")).status_code == 200
    _expire_row(sc["conv_id"])
    r = c.post("/api/solution-center/verify-otp", json=_body(sc, code="920562"))
    assert r.status_code == 400
    assert "süre" in r.json()["detail"].lower()


# ── 5) Birden fazla öğrenci ──────────────────────────────────────────────────

def test_multiple_students_requires_selection(sc):
    routes = _default_routes()
    routes[PATH_VERIFY_CODE] = (200, {"isSuccess": True, "data": [
        {"ogrenciId": 111, "birimAdi": "A", "fakulteAdi": "FA"},
        {"ogrenciId": 222, "birimAdi": "B", "fakulteAdi": "FB"},
    ]})
    c = sc["configure"](routes)
    assert c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111")).status_code == 200
    r = c.post("/api/solution-center/verify-otp", json=_body(sc, code="920562"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["auto_selected"] is False
    assert data["state"] == "SC_WAIT_STUDENT_SELECTION"
    assert len(data["students"]) == 2
    # Seçim yap
    r2 = c.post("/api/solution-center/select-student", json=_body(sc, ogrenci_id=222))
    assert r2.status_code == 200, r2.text
    assert r2.json()["state"] == "SC_WAIT_CATEGORY_SELECTION"
    assert _read_row(sc["conv_id"])["selected_student_id"] == 222


# ── 6) Kategori bulunamadı / seçilemez ───────────────────────────────────────

def test_non_leaf_category_rejected(sc):
    c = sc["configure"]()
    assert c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111")).status_code == 200
    # tek öğrenci → otomatik seçilir → SC_WAIT_CATEGORY_SELECTION
    assert c.post("/api/solution-center/verify-otp", json=_body(sc, code="920562")).status_code == 200
    # OGR_ISLERI yaprak DEĞİL → seçilemez
    r = c.post("/api/solution-center/select-category", json=_body(sc, category_short_code="OGR_ISLERI"))
    assert r.status_code == 400
    assert "kategori" in r.json()["detail"].lower()


# ── 7) Talep başarıyla oluşturuldu (tam mutlu yol) ───────────────────────────

def test_full_happy_path_creates_ticket(sc):
    c = sc["configure"]()
    assert c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111")).status_code == 200
    assert c.post("/api/solution-center/verify-otp", json=_body(sc, code="920562")).status_code == 200
    # Kategori ağacı gelmeli (yaprak seçilebilir)
    rc = c.post("/api/solution-center/categories", json=_body(sc))
    assert rc.status_code == 200, rc.text
    roots = rc.json()["categories"]
    assert roots[0]["shortCode"] == "OGR_ISLERI"
    assert roots[0]["children"][0]["shortCode"] == "DERS_KAYDI"
    # Yaprak seç → açıklama → talep
    assert c.post("/api/solution-center/select-category", json=_body(sc, category_short_code="DERS_KAYDI")).status_code == 200
    r = c.post("/api/solution-center/create-ticket", json=_body(sc, description="Ders kaydımda sorun var."))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["state"] == "SC_FINISHED"
    assert data["ticket"]["uuid"] == "BMGJRL"
    # Token kullanıldı → silinmeli
    assert _read_row(sc["conv_id"])["verification_token"] is None


# ── 8) API Timeout ───────────────────────────────────────────────────────────

def test_api_timeout_maps_to_503(sc):
    routes = _default_routes()
    routes[PATH_GET_PHONE] = "timeout"
    c = sc["configure"](routes)
    r = c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111"))
    assert r.status_code == 503


# ── 9) API 500 hatası ────────────────────────────────────────────────────────

def test_api_500_maps_to_503(sc):
    routes = _default_routes()
    routes[PATH_GET_PHONE] = (500, {"isSuccess": False, "message": "boom"})
    c = sc["configure"](routes)
    r = c.post("/api/solution-center/send-sms", json=_body(sc, kimlik_no="11111111111"))
    assert r.status_code == 503
    assert "boom" not in r.text


# ── Bonus: config kapalıyken (base_url/token yok) nazik 503 ──────────────────

def test_disabled_when_unconfigured(app, db):
    # HERMETİK: geliştiricinin .env'inde gerçek CM_BASE_URL/CM_SERVICE_TOKEN olsa
    # bile (load_dotenv onları test ortamına sızdırır) bu test config'i AÇIKÇA
    # kapalıya override eder. dataclasses.replace tüm alanları kopyaladığı için
    # config'e yeni alan eklenen branch'lerde de çalışır.
    import dataclasses
    from fastapi.testclient import TestClient
    from core.database import Conversation
    from integrations.solution_center import get_config

    disabled = dataclasses.replace(get_config(), base_url=None, service_token=None)
    app.dependency_overrides[get_config] = lambda: disabled
    try:
        conv = Conversation(client_token=secrets.token_urlsafe(24))
        db.add(conv)
        db.commit()
        db.refresh(conv)
        c = TestClient(app)
        r = c.post("/api/solution-center/send-sms", json={
            "conversation_id": conv.id, "conversation_token": conv.client_token,
            "kimlik_no": "11111111111",
        })
        assert r.status_code == 503
    finally:
        app.dependency_overrides.pop(get_config, None)


# ── Bonus: sahiplik token'ı yanlışsa 403 ─────────────────────────────────────

def test_wrong_owner_token_forbidden(sc):
    c = sc["configure"]()
    r = c.post("/api/solution-center/send-sms", json={
        "conversation_id": sc["conv_id"], "conversation_token": "yanlis-token",
        "kimlik_no": "11111111111",
    })
    assert r.status_code == 403
