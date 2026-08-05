"""OTP deneme sayacı (ANALIZ.md P0-2).

Ağ YOK: httpx.MockTransport DI ile enjekte edilir; transport CM'ye kaç çağrı
gittiğini sayar — "kilitlendikten sonra CM'ye hiç gidilmiyor" iddiasının kanıtı.

P0-1 SMS limiteri bilinçli olarak COMERT degerlerle override edilir; aksi hâlde
SMS sayacı OTP testlerine karışır ve hata mesajları yanıltıcı olur.

Kapsanan senaryolar:
  1) Esige ulasinca oturum kilitlenir   2) Kilitliyken CM'ye cagri GITMEZ
  3) Esigin altinda eski davranis       4) Dogru kod sayaci sifirlar
  5) Yeni SMS sayaci sifirlar           6) Sayac konusmaya ozgu
  7) Kilit mesaji kod/token sizdirmaz   8) Sayac/kilit TOKEN'a kosullu (yaris)
  9) Deneme CM.den ONCE rezerve edilir 10) Ulasilamayan CM rezervasyonu iade eder
"""
import secrets

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
from integrations.solution_center.constants import (
    PATH_GET_PHONE,
    PATH_SEND_SMS,
    PATH_VERIFY_CODE,
)
from integrations.solution_center.rate_limit import SmsRateLimiter

TC = "11111111111"
GOOD_CODE = "920562"
BAD_CODE = "000000"
MAX_ATTEMPTS = 5

CONFIG = SolutionCenterConfig(
    base_url="https://cm.test",
    service_token="svc-token",
    channel_short_code="AUZEF_WEB_SAYFASI_CHATBOT",
    timeout_seconds=5.0,
    max_retries=0,
    verification_ttl_min=10,
    category_cache_ttl_seconds=900,
    # P0-1 limitleri yolda olmasin: bu dosya YALNIZCA P0-2'yi olcuyor.
    sms_per_conversation=999,
    sms_per_tc=999,
    sms_per_ip=999,
    hash_secret="test-secret",
    otp_max_attempts=MAX_ATTEMPTS,
)


def _total_attempts() -> int:
    """Tum SC oturumlarindaki otp_attempts toplami (ayri session ile taze okur)."""
    from core.database import SessionLocal, SolutionCenterSession
    s = SessionLocal()
    try:
        return sum(r.otp_attempts for r in s.query(SolutionCenterSession).all())
    finally:
        s.close()


class _Transport:
    """CM cagrilarini sayar; verify_code yanitini test kontrol eder."""

    def __init__(self):
        self.calls = 0
        self.verify_calls = 0
        # CM'nin cagrildigi ANDA sayacin degeri. Rezervasyon modelinin
        # kanitidir: deneme CM'ye gitmeden ONCE dusulmus olmali.
        self.attempts_at_call = []
        self.fail_verify = False   # True -> CM'ye ulasilamiyor (timeout)

    def build(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            path = request.url.path
            if path == PATH_GET_PHONE:
                return httpx.Response(200, json={
                    "isSuccess": True, "data": {"maskedPhoneNumber": "*******208"}})
            if path == PATH_SEND_SMS:
                return httpx.Response(200, json={
                    "isSuccess": True, "data": {"verificationToken": "tok-abc"}})
            if path == PATH_VERIFY_CODE:
                self.verify_calls += 1
                self.attempts_at_call.append(_total_attempts())
                if self.fail_verify:
                    raise httpx.TimeoutException("mock timeout")
                # Yalnizca GOOD_CODE kabul edilir.
                if request.content and GOOD_CODE.encode() in request.content:
                    return httpx.Response(200, json={"isSuccess": True, "data": [
                        {"ogrenciId": 649203, "birimAdi": "Birim", "fakulteAdi": "Fak"}]})
                return httpx.Response(200, json={"isSuccess": False, "data": None})
            return httpx.Response(404, json={"isSuccess": False})
        return httpx.MockTransport(handler)


@pytest.fixture()
def sc(app, db):
    from core.database import Conversation
    from fastapi.testclient import TestClient

    transport = _Transport()
    app.dependency_overrides[get_config] = lambda: CONFIG
    app.dependency_overrides[get_sc_client] = lambda: SolutionCenterClient(
        CONFIG, transport=transport.build())
    app.dependency_overrides[get_category_cache] = lambda: CategoryCache(900)
    app.dependency_overrides[get_sms_rate_limiter] = lambda: SmsRateLimiter(CONFIG)
    client = TestClient(app)

    def new_conversation() -> dict:
        conv = Conversation(client_token=secrets.token_urlsafe(24), ip_address="127.0.0.1")
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return {"id": conv.id, "token": conv.client_token}

    def start(conv: dict):
        r = client.post("/api/solution-center/send-sms", json={
            "conversation_id": conv["id"], "conversation_token": conv["token"],
            "kimlik_no": TC})
        assert r.status_code == 200, r.text
        return r

    def verify(conv: dict, code: str):
        return client.post("/api/solution-center/verify-otp", json={
            "conversation_id": conv["id"], "conversation_token": conv["token"],
            "code": code})

    yield {"new_conversation": new_conversation, "start": start,
           "verify": verify, "transport": transport}

    for dep in (get_config, get_sc_client, get_category_cache, get_sms_rate_limiter):
        app.dependency_overrides.pop(dep, None)


def _row(conv_id: int):
    from core.database import SessionLocal, SolutionCenterSession
    s = SessionLocal()
    try:
        r = s.query(SolutionCenterSession).filter_by(conversation_id=conv_id).first()
        return None if r is None else {
            "state": r.state,
            "verification_token": r.verification_token,
            "otp_attempts": r.otp_attempts,
        }
    finally:
        s.close()


# ── 1) Esige ulasinca oturum kilitlenir ──────────────────────────────────────

def test_lock_after_max_wrong_attempts(sc):
    conv = sc["new_conversation"]()
    sc["start"](conv)

    for i in range(MAX_ATTEMPTS - 1):
        r = sc["verify"](conv, BAD_CODE)
        assert r.status_code == 400, f"{i + 1}. deneme henüz kilitlememeli"

    r = sc["verify"](conv, BAD_CODE)          # MAX'inci deneme
    assert r.status_code == 429
    assert "baştan" in r.json()["detail"].lower()

    row = _row(conv["id"])
    assert row["verification_token"] is None, "token silinmeliydi"
    assert row["state"] == "SC_WAIT_TC", "akis basa donmeliydi"


# ── 2) Kilitliyken CM'ye cagri GITMEZ ────────────────────────────────────────

def test_locked_session_never_calls_cm(sc):
    conv = sc["new_conversation"]()
    sc["start"](conv)
    for _ in range(MAX_ATTEMPTS):
        sc["verify"](conv, BAD_CODE)

    calls_before = sc["transport"].verify_calls
    r = sc["verify"](conv, BAD_CODE)
    # Kilit state'i SC_WAIT_TC'ye cektigi icin bu istek state guard'ina takilir
    # → 409 "OTP beklenmiyor". (Kilit state'i korusaydi 429 donerdi; ikisi de
    # kabul, cunku ASIL iddia asagidaki satir: CM'ye YENI cagri GITMEMESI.)
    assert r.status_code in (409, 429), r.text
    assert sc["transport"].verify_calls == calls_before, "kilitli oturum CM'ye gitti!"


# ── 3) Esigin altinda eski davranis korunur ──────────────────────────────────

def test_below_threshold_keeps_invalid_otp_behaviour(sc):
    conv = sc["new_conversation"]()
    sc["start"](conv)

    r = sc["verify"](conv, BAD_CODE)
    assert r.status_code == 400
    assert "doğrulama kodu hatalı" in r.json()["detail"].lower()
    assert _row(conv["id"])["otp_attempts"] == 1
    assert _row(conv["id"])["verification_token"] == "tok-abc", "token durmali"


# ── 4) Dogru kod sayaci sifirlar ─────────────────────────────────────────────

def test_success_resets_counter(sc):
    conv = sc["new_conversation"]()
    sc["start"](conv)
    for _ in range(MAX_ATTEMPTS - 1):
        sc["verify"](conv, BAD_CODE)
    assert _row(conv["id"])["otp_attempts"] == MAX_ATTEMPTS - 1

    r = sc["verify"](conv, GOOD_CODE)
    assert r.status_code == 200, r.text
    assert _row(conv["id"])["otp_attempts"] == 0


# ── 5) Yeni SMS sayaci sifirlar ──────────────────────────────────────────────

def test_new_sms_resets_counter(sc):
    """SMS'i gec gelen kullanici yeni kod isteyince tukenmis sayacla
    karsilasmamali. Bu sifirlamanin sinirsiz istismari P0-1 SMS limitiyle
    engellenir (bu testte limit bilincli olarak acik birakildi)."""
    conv = sc["new_conversation"]()
    sc["start"](conv)
    for _ in range(MAX_ATTEMPTS - 1):
        sc["verify"](conv, BAD_CODE)
    assert _row(conv["id"])["otp_attempts"] == MAX_ATTEMPTS - 1

    sc["start"](conv)                          # yeni SMS
    assert _row(conv["id"])["otp_attempts"] == 0
    assert sc["verify"](conv, BAD_CODE).status_code == 400, "yine hak olmali"


# ── 6) Sayac konusmaya ozgu ──────────────────────────────────────────────────

def test_counter_is_per_conversation(sc):
    a = sc["new_conversation"]()
    b = sc["new_conversation"]()
    sc["start"](a)
    sc["start"](b)

    for _ in range(MAX_ATTEMPTS):
        sc["verify"](a, BAD_CODE)
    assert _row(a["id"])["verification_token"] is None, "a kilitlenmeliydi"

    # b etkilenmemeli
    assert _row(b["id"])["otp_attempts"] == 0
    assert sc["verify"](b, GOOD_CODE).status_code == 200


# ── 7) Kilit mesaji kod/token sizdirmaz ──────────────────────────────────────

def test_lock_message_leaks_nothing(sc):
    conv = sc["new_conversation"]()
    sc["start"](conv)
    for _ in range(MAX_ATTEMPTS - 1):
        sc["verify"](conv, BAD_CODE)
    r = sc["verify"](conv, BAD_CODE)

    assert r.status_code == 429
    body = r.text
    assert BAD_CODE not in body, "girilen kod yanitta gorunmemeli"
    assert "tok-abc" not in body, "verificationToken yanitta gorunmemeli"
    assert TC not in body, "TC yanitta gorunmemeli"
    # Deneme sayisi da sizmamali (saldirgana ilerleme geri bildirimi vermez).
    assert str(MAX_ATTEMPTS) not in r.json()["detail"]


# ── 8) Sayac ve kilit TOKEN'a kosullu (yaris korumasi) ───────────────────────
# CM cagrisi beklenirken (await) kullanici yeni SMS isteyip token'i yenileyebilir.
# Gec gelen istek YENI oturuma dokunmamali: ne sayacini kirletmeli ne de onu
# kilitlemeli. Bu yarisi TestClient ile gercek zamanli kurmak mumkun olmadigi
# icin, ic metotlar dogrudan cagrilarak kosul dogrulanir.

def _service(db):
    """Yalnizca DB kullanan ic metotlari test etmek icin minimal servis."""
    from integrations.solution_center.service import SolutionCenterService
    return SolutionCenterService(db=db, client=None, cache=None,
                                 config=CONFIG, limiter=None)


def test_attempt_counter_is_token_scoped(sc):
    from core.database import SessionLocal

    conv = sc["new_conversation"]()
    sc["start"](conv)                                  # token = tok-abc, sayac 0

    s = SessionLocal()
    try:
        svc = _service(s)
        # ESKI (artik gecersiz) token ile artirma -> hicbir satir eslesmez
        assert svc._consume_otp_attempt(conv["id"], "eski-token") is None
        assert _row(conv["id"])["otp_attempts"] == 0, "yeni oturumun sayaci kirlendi"
        # GECERLI token ile artirma normal calisir
        assert svc._consume_otp_attempt(conv["id"], "tok-abc") == 1
    finally:
        s.close()


def test_lock_is_token_scoped(sc):
    from core.database import SessionLocal

    conv = sc["new_conversation"]()
    sc["start"](conv)

    s = SessionLocal()
    try:
        svc = _service(s)
        # ESKI token ile kilitleme YENI oturumu iptal ETMEMELI
        svc._lock_verification(conv["id"], "eski-token")
        row = _row(conv["id"])
        assert row["verification_token"] == "tok-abc", "gecerli token silindi!"
        assert row["state"] == "SC_WAIT_OTP", "gecerli oturum bastan basa dondu!"
        # GECERLI token ile kilitleme calisir
        svc._lock_verification(conv["id"], "tok-abc")
        row = _row(conv["id"])
        assert row["verification_token"] is None
        assert row["state"] == "SC_WAIT_TC"
    finally:
        s.close()


# ── 9) Deneme CM'ye gitmeden ONCE rezerve edilir (paralel patlama korumasi) ──
# Sayac eskiden CM YANITINDAN SONRA artiyordu. O sirada N paralel istek on
# kontrolu AYNI (henuz artmamis) degerle geciyor ve hepsi CM'ye tahmin
# gonderiyordu; atomik artirma sayacin BOZULMASINI engeller ama bu ASIMI
# engellemez. verify-otp nginx'te de hiz sinirli olmadigi icin pratikte tek SMS
# basina yuzlerce tahmin demekti.
#
# Paralelligi TestClient ile kurmak mumkun olmadigi icin ORDERING invaryanti
# olculur: CM cagrildigi ANDA sayac zaten artmis olmali.

def test_attempt_is_reserved_before_cm_call(sc):
    conv = sc["new_conversation"]()
    sc["start"](conv)

    sc["verify"](conv, BAD_CODE)

    assert sc["transport"].attempts_at_call == [1], (
        "sayac CM cagrisindan SONRA artiyor -> paralel istekler butceyi asabilir "
        f"(olculen: {sc['transport'].attempts_at_call})"
    )


def test_unreachable_cm_refunds_the_reservation(sc):
    """Rezervasyon modelinin ikinci yarisi: CM'ye ULASILAMAZSA hak iade edilir.
    Timeout bir TAHMIN degildir; mesru kullanicinin hakki CM'nin sorunlari
    yuzunden yanmamali."""
    conv = sc["new_conversation"]()
    sc["start"](conv)

    sc["transport"].fail_verify = True
    r = sc["verify"](conv, BAD_CODE)
    assert r.status_code == 503, r.text          # ApiConnectionException

    assert _row(conv["id"])["otp_attempts"] == 0, "ag hatasi kullanicinin hakkini yakti"

    # Iade sonrasi tam hak duruyor: MAX kadar deneme hala yapilabilmeli
    sc["transport"].fail_verify = False
    for i in range(MAX_ATTEMPTS - 1):
        assert sc["verify"](conv, BAD_CODE).status_code == 400, f"{i + 1}. deneme"
    assert sc["verify"](conv, BAD_CODE).status_code == 429
