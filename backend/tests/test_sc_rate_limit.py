"""SMS hız sınırı (ANALIZ.md P0-1).

Ağ YOK: httpx.MockTransport DI ile enjekte edilir; transport ayrıca CM'ye KAÇ
çağrı gittiğini sayar — "limit dolunca CM'ye hiç gidilmiyor" iddiasının kanıtı
budur (yalnızca HTTP durumuna bakmak yeterli olmazdı).

Kapsanan senaryolar:
  1) Konuşma limiti                 2) Limit dolunca CM'ye çağrı GİTMEZ
  3) TC limiti (konuşmalar arası)   4) IP limiti (numaralandırma senaryosu)
  5) Pencere dolunca sayaç sıfırlanır
  6) Başarısız TC denemesi de sayılır
  7) Tabloda HAM TC yok (güvenlik regresyonu)
"""
import secrets
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from integrations.solution_center import (
    get_category_cache,
    get_config,
    get_sc_client,
    get_sms_rate_limiter,
)
from integrations.solution_center.base_client import SolutionCenterConfig
from integrations.solution_center.category_cache import CategoryCache
from integrations.solution_center.client import SolutionCenterClient
from integrations.solution_center.constants import PATH_GET_PHONE, PATH_SEND_SMS
from integrations.solution_center.rate_limit import SmsRateLimiter

TC_A = "11111111111"
TC_B = "22222222222"

BASE_CONFIG = SolutionCenterConfig(
    base_url="https://cm.test",
    service_token="svc-token",
    channel_short_code="AUZEF_WEB_SAYFASI_CHATBOT",
    timeout_seconds=5.0,
    max_retries=0,
    verification_ttl_min=10,
    category_cache_ttl_seconds=900,
    # Testler için küçük limitler (davranış aynı, tur sayısı az).
    sms_per_conversation=3,
    sms_conversation_window_min=15,
    sms_per_tc=5,
    sms_tc_window_min=60,
    sms_per_ip=1000,          # varsayılan olarak yolda değil; ilgili test kısar
    sms_ip_window_min=60,
    hash_secret="test-secret",
)


# ── Çağrı sayan mock transport ───────────────────────────────────────────────

class _CountingTransport:
    """CM'ye giden istekleri sayar; testler 'hiç çağrı gitmedi'yi doğrulayabilsin."""

    def __init__(self, phone_ok: bool = True):
        self.calls = 0
        self.phone_ok = phone_ok

    def build(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            if request.url.path == PATH_GET_PHONE:
                if not self.phone_ok:
                    return httpx.Response(200, json={"isSuccess": False, "data": None})
                return httpx.Response(200, json={
                    "isSuccess": True, "data": {"maskedPhoneNumber": "*******208"}})
            if request.url.path == PATH_SEND_SMS:
                return httpx.Response(200, json={
                    "isSuccess": True, "data": {"verificationToken": "tok-abc"}})
            return httpx.Response(404, json={"isSuccess": False})
        return httpx.MockTransport(handler)


# ── Fixture ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sc(app, db):
    """Konuşma üretici + override'lı TestClient üretici."""
    from core.database import Conversation
    from fastapi.testclient import TestClient

    created = []

    def new_conversation() -> dict:
        conv = Conversation(client_token=secrets.token_urlsafe(24), ip_address="127.0.0.1")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        created.append(conv)
        return {"id": conv.id, "token": conv.client_token}

    state = {"transport": None}

    def configure(*, phone_ok: bool = True, ip: str = "10.0.0.1", **limit_overrides):
        cfg = replace(BASE_CONFIG, **limit_overrides) if limit_overrides else BASE_CONFIG
        counter = _CountingTransport(phone_ok=phone_ok)
        state["transport"] = counter
        app.dependency_overrides[get_config] = lambda: cfg
        app.dependency_overrides[get_sc_client] = lambda: SolutionCenterClient(
            cfg, transport=counter.build())
        app.dependency_overrides[get_category_cache] = lambda: CategoryCache(900)
        app.dependency_overrides[get_sms_rate_limiter] = lambda: SmsRateLimiter(cfg)
        # client=(ip, port) → request.client.host, yani IP kapsamının gördüğü değer.
        return TestClient(app, client=(ip, 50000))

    yield {
        "new_conversation": new_conversation,
        "configure": configure,
        "transport": lambda: state["transport"],
    }

    for dep in (get_config, get_sc_client, get_category_cache, get_sms_rate_limiter):
        app.dependency_overrides.pop(dep, None)


def _send(client, conv: dict, tc: str = TC_A):
    return client.post("/api/solution-center/send-sms", json={
        "conversation_id": conv["id"],
        "conversation_token": conv["token"],
        "kimlik_no": tc,
    })


def _rate_limit_rows():
    from core.database import SCRateLimit, SessionLocal
    s = SessionLocal()
    try:
        return [(r.scope, r.key_hash, r.count) for r in s.query(SCRateLimit).all()]
    finally:
        s.close()


# ── 1) Konuşma limiti ────────────────────────────────────────────────────────

def test_conversation_limit_blocks_fourth_sms(sc):
    c = sc["configure"]()
    conv = sc["new_conversation"]()

    for i in range(3):
        assert _send(c, conv).status_code == 200, f"{i + 1}. SMS geçmeliydi"

    r = _send(c, conv)
    assert r.status_code == 429
    # Kullanıcıya nazik Türkçe mesaj döner; teknik ayrıntı SIZMAZ.
    detail = r.json()["detail"]
    assert "çok fazla" in detail.lower()
    assert "kapsam" not in detail.lower()  # hangi limitin dolduğu söylenmez


# ── 2) Limit dolunca CM API'sine çağrı GİTMEZ ────────────────────────────────

def test_blocked_request_never_reaches_cm_api(sc):
    c = sc["configure"]()
    conv = sc["new_conversation"]()

    for _ in range(3):
        _send(c, conv)
    calls_before = sc["transport"]().calls
    assert calls_before == 6  # 3 SMS × (get_phone + send_sms)

    assert _send(c, conv).status_code == 429
    # Asıl kanıt: engellenen istek CM'ye HİÇ ulaşmadı (SMS maliyeti oluşmadı).
    assert sc["transport"]().calls == calls_before


# ── 3) TC limiti — farklı konuşmalar aynı numarayı bombalayamaz ──────────────

def test_tc_limit_spans_conversations(sc):
    """Konuşma limiti tek başına yeterli değil: saldırgan yeni konuşma açabilir.
    TC kapsamı aynı numaraya toplam bütçe koyar."""
    c = sc["configure"](sms_per_conversation=1, sms_per_tc=3)

    for i in range(3):
        conv = sc["new_conversation"]()      # her seferinde TAZE konuşma
        assert _send(c, conv, TC_A).status_code == 200, f"{i + 1}. deneme geçmeliydi"

    conv = sc["new_conversation"]()
    assert _send(c, conv, TC_A).status_code == 429
    # Başka bir TC hâlâ çalışmalı — sınır TC'ye özgü, topyekûn kilit değil.
    assert _send(c, sc["new_conversation"](), TC_B).status_code == 200


# ── 4) IP limiti — numaralandırma senaryosu ──────────────────────────────────

def test_ip_limit_stops_tc_enumeration(sc):
    """Numaralandırmada her TC yalnızca BİR kez denenir → TC kapsamı tetiklenmez.
    Bunu durduran şey IP kapsamıdır."""
    c = sc["configure"](sms_per_conversation=1, sms_per_tc=99, sms_per_ip=3, ip="10.0.0.7")

    for i in range(3):
        tc = f"{i:011d}"                      # her seferinde FARKLI TC
        assert _send(c, sc["new_conversation"](), tc).status_code == 200

    r = _send(c, sc["new_conversation"](), "99999999999")
    assert r.status_code == 429

    # Farklı IP'den gelen istek etkilenmemeli (sınır IP'ye özgü).
    c2 = sc["configure"](sms_per_conversation=1, sms_per_tc=99, sms_per_ip=3, ip="10.0.0.8")
    assert _send(c2, sc["new_conversation"](), "88888888888").status_code == 200


# ── 5) Pencere dolunca sayaç sıfırlanır ──────────────────────────────────────

def test_counter_resets_after_window(sc):
    from core.database import SCRateLimit, SessionLocal, utcnow

    c = sc["configure"]()
    conv = sc["new_conversation"]()
    for _ in range(3):
        _send(c, conv)
    assert _send(c, conv).status_code == 429

    # Pencereyi geriye al (gerçek zaman beklemeden) — _expire_row ile aynı kalıp.
    s = SessionLocal()
    try:
        s.query(SCRateLimit).update(
            {SCRateLimit.window_started_at: utcnow() - timedelta(hours=2)})
        s.commit()
    finally:
        s.close()

    assert _send(c, conv).status_code == 200, "pencere dolunca yeniden izin verilmeli"


# ── 6) Başarısız TC denemesi de sayılır ──────────────────────────────────────

def test_failed_tc_lookup_still_counts(sc):
    """Numaralandırma BAŞARISIZ aramalarla yapılır; yalnızca başarılıları saymak
    korumayı tamamen delerdi."""
    c = sc["configure"](phone_ok=False)       # CM: kayıt yok → 404
    conv = sc["new_conversation"]()

    for _ in range(3):
        assert _send(c, conv).status_code == 404

    assert _send(c, conv).status_code == 429


# ── 7) Güvenlik regresyonu: tabloda HAM TC yok ───────────────────────────────

def test_raw_tc_is_never_stored(sc):
    c = sc["configure"]()
    _send(c, sc["new_conversation"](), TC_A)

    rows = _rate_limit_rows()
    assert rows, "sayaç satırı oluşmalıydı"
    for scope, key_hash, _count in rows:
        assert TC_A not in key_hash, "HAM TC sayaç tablosuna yazılmış!"
        assert len(key_hash) == 64, "anahtar HMAC-SHA256 hex olmalı"
    # Üç kapsam da yazılmış olmalı
    assert {scope for scope, _k, _c in rows} == {"conv", "tc", "ip"}


# ── 8) Temizlik hatası hız sınırını BOZMAMALI ────────────────────────────────
# _cleanup "best-effort" diye hatasını yutuyordu, ama yutmak yetmiyordu: DELETE
# transaction'ı abort ettiği için aynı transaction'da bekleyen sayaç artışları
# da geri alınıyordu.
#
# Düzeltmesiz davranış ÖLÇÜLDÜ (tahmin değil): http=200, exception=None,
# sayaç satırı=0. Yani hata hiçbir yere yansımıyor — ne kullanıcıya, ne log'a,
# ne de bir 500'e; istek başarılı görünürken hız sınırı SESSİZCE fail-open
# oluyor. Sessiz olması tespiti daha da zorlaştırıyor.
#
# Üstelik deadlock/kilit çakışması olasılığı tam da yük altında, yani korumanın
# en çok gerektiği anda yükselir.

def test_cleanup_failure_does_not_break_rate_limiting(sc, monkeypatch):
    from sqlalchemy import text as _text

    from integrations.solution_center import rate_limit

    # Temizliği kasten patlat (var olmayan tablo → DBAPI hatası → transaction abort)
    monkeypatch.setattr(rate_limit, "_CLEANUP_SQL", _text("DELETE FROM tablo_yok_ki"))

    c = sc["configure"]()
    conv = sc["new_conversation"]()

    r = _send(c, conv)
    assert r.status_code == 200, f"temizlik hatasi istegi dusurdu: {r.text}"

    # ASIL İDDİA: sayaç artışı KAYBOLMAMALI, yoksa limit fail-open olur.
    # ÜÇ kapsam da beklenir (TestClient istemci IP'si verdiği için ip de yazılır):
    # tam küme eşitliği, kısmi geri alma / eksik yazma durumlarını da yakalar —
    # "conv ve tc var" demek, ip kapsamının sessizce kaybolmasını gözden kaçırırdı.
    rows = _rate_limit_rows()
    scopes = {scope for scope, _k, _c in rows}
    assert scopes == {"conv", "tc", "ip"}, f"sayaclar geri alindi/eksik: {rows}"
