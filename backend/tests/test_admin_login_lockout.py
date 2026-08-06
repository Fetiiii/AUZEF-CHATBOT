"""Admin girişi kaba kuvvet koruması (ANALIZ.md P0-3).

Eşikler monkeypatch.setenv ile küçültülür (test_maintenance.py'deki
MAINTENANCE_FLAG_DIR kalıbının aynısı) — login_lockout.py env'i çağrı anında
okur, modül yüklenirken donmuyor.

Kapsanan senaryolar:
  1) E-posta kapsamı kilitler          2) IP kapsamı kilitler (spray)
  3) Kilitliyken sayaç büyümeye devam etmez (idempotent kısa devre)
  4) Doğru parola sayaçları sıfırlar   5) Eşik altında eski davranış (401) korunur
  6) Farklı e-posta/IP birbirini etkilemez
  7) Pencere dolunca sayaç sıfırlanır
  8) Kilit mesajı deneme sayısı/kapsam sızdırmaz
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_PASSWORD, TEST_PASSWORD_WRONG

EMAIL_MAX = 3
IP_MAX = 5


@pytest.fixture(autouse=True)
def tight_thresholds(monkeypatch):
    """Testler için küçük eşikler — üretim varsayılanları (5/20) turu yavaşlatırdı."""
    monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", str(EMAIL_MAX))
    monkeypatch.setenv("ADMIN_LOGIN_IP_MAX_ATTEMPTS", str(IP_MAX))
    monkeypatch.setenv("ADMIN_LOGIN_WINDOW_MIN", "15")


def _client(app, ip: str = "10.0.0.1") -> TestClient:
    return TestClient(app, client=(ip, 50000))


def _login(c: TestClient, email: str, password: str = TEST_PASSWORD_WRONG):
    return c.post("/api/auth/login", json={"email": email, "password": password})


# ── 1) E-posta kapsamı kilitler ──────────────────────────────────────────────

def test_email_scope_locks_after_max_attempts(app, make_user):
    make_user("hedef@iu.tr")
    c = _client(app, "10.0.0.10")

    for i in range(EMAIL_MAX - 1):
        r = _login(c, "hedef@iu.tr")
        assert r.status_code == 401, f"{i + 1}. deneme henüz kilitlememeli"

    r = _login(c, "hedef@iu.tr")           # EMAIL_MAX'inci deneme
    assert r.status_code == 429
    assert "çok fazla" in r.json()["detail"].lower()


# ── 2) IP kapsamı kilitler (spray — farklı, var olmayan e-postalar) ─────────

def test_ip_scope_locks_across_different_emails(app):
    c = _client(app, "10.0.0.20")

    for i in range(IP_MAX - 1):
        r = _login(c, f"spray-{i}@iu.tr")   # her seferinde FARKLI e-posta
        assert r.status_code == 401, f"{i + 1}. deneme henüz kilitlememeli"

    r = _login(c, "spray-son@iu.tr")
    assert r.status_code == 429


# ── 3) Kilitliyken sayaç büyümeye devam etmez ────────────────────────────────

def test_locked_requests_dont_keep_incrementing(app, make_user, db):
    from core.database import AdminLoginAttempt

    make_user("hedef2@iu.tr")
    c = _client(app, "10.0.0.30")
    for _ in range(EMAIL_MAX):
        _login(c, "hedef2@iu.tr")

    row = db.query(AdminLoginAttempt).filter_by(scope="email", identifier="hedef2@iu.tr").first()
    count_after_lock = row.count

    _login(c, "hedef2@iu.tr")               # kilitliyken bir istek daha
    _login(c, "hedef2@iu.tr")
    db.expire_all()
    row = db.query(AdminLoginAttempt).filter_by(scope="email", identifier="hedef2@iu.tr").first()
    assert row.count == count_after_lock, "kilitliyken sayaç artmaya devam etti"


# ── 4) Doğru parola sayaçları sıfırlar ───────────────────────────────────────

def test_success_resets_counters(app, make_user):
    make_user("hedef3@iu.tr")
    c = _client(app, "10.0.0.40")

    for _ in range(EMAIL_MAX - 1):
        _login(c, "hedef3@iu.tr")

    r = c.post("/api/auth/login", json={"email": "hedef3@iu.tr", "password": TEST_PASSWORD})
    assert r.status_code == 200, r.text

    # Sıfırlandığı için yeniden EMAIL_MAX-1 yanlış deneme kilitlememeli.
    for i in range(EMAIL_MAX - 1):
        r = _login(c, "hedef3@iu.tr")
        assert r.status_code == 401, f"sıfırlama sonrası {i + 1}. deneme kilitli görünüyor"


# ── 5) Eşik altında eski davranış korunur (regresyon) ────────────────────────

def test_below_threshold_keeps_normal_401_behaviour(app, make_user):
    make_user("aktif@iu.tr")
    make_user("pasif@iu.tr", active=False)
    c = _client(app, "10.0.0.50")

    for payload in [
        {"email": "aktif@iu.tr", "password": TEST_PASSWORD_WRONG},
        {"email": "bilinmeyen@iu.tr", "password": TEST_PASSWORD},
        {"email": "pasif@iu.tr", "password": TEST_PASSWORD},
    ]:
        r = c.post("/api/auth/login", json=payload)
        assert r.status_code == 401
        assert r.json()["detail"] == "E-posta ya da parola hatalı."


# ── 6) Farklı e-posta/IP birbirini etkilemez ─────────────────────────────────

def test_scopes_are_isolated(app, make_user):
    make_user("a@iu.tr")
    make_user("b@iu.tr")
    c1 = _client(app, "10.0.0.60")

    for _ in range(EMAIL_MAX):
        _login(c1, "a@iu.tr")
    assert _login(c1, "a@iu.tr").status_code == 429, "a@iu.tr kilitli olmalıydı"

    # Aynı IP'den b@iu.tr — e-posta kapsamı b için sıfır, hâlâ deneyebilmeli
    # (IP kapsamı EMAIL_MAX'ten büyük IP_MAX eşiğine sahip, henüz dolmadı).
    r = c1.post("/api/auth/login", json={"email": "b@iu.tr", "password": TEST_PASSWORD})
    assert r.status_code == 200, "farklı e-posta aynı IP'den hâlâ giriş yapabilmeliydi"

    # Farklı IP'den a@iu.tr — DOĞRU parolayla bile reddedilmeli: e-posta kapsamı
    # zaten dolu, IP fark etmez.
    c2 = _client(app, "10.0.0.61")
    r = c2.post("/api/auth/login", json={"email": "a@iu.tr", "password": TEST_PASSWORD})
    assert r.status_code == 429, "e-posta kapsamı IP'den bağımsız kilitli kalmalı"


# ── 7) Pencere dolunca sayaç sıfırlanır ──────────────────────────────────────

def test_counter_resets_after_window(app, make_user, db):
    from core.database import AdminLoginAttempt
    from core.database import utcnow as _utcnow

    make_user("hedef4@iu.tr")
    c = _client(app, "10.0.0.70")
    for _ in range(EMAIL_MAX):
        _login(c, "hedef4@iu.tr")
    assert _login(c, "hedef4@iu.tr").status_code == 429

    db.query(AdminLoginAttempt).update({AdminLoginAttempt.window_started_at: _utcnow() - timedelta(hours=2)})
    db.commit()

    r = _login(c, "hedef4@iu.tr")
    assert r.status_code == 401, "pencere dolunca yeniden izin verilmeliydi"


# ── 8) Kilit mesajı sızdırmaz ────────────────────────────────────────────────

def test_lockout_message_leaks_nothing(app, make_user):
    make_user("hedef5@iu.tr")
    c = _client(app, "10.0.0.80")
    for _ in range(EMAIL_MAX):
        _login(c, "hedef5@iu.tr")
    r = _login(c, "hedef5@iu.tr")

    assert r.status_code == 429
    body = r.text
    assert "hedef5@iu.tr" not in body, "e-posta yanıtta görünmemeli"
    assert str(EMAIL_MAX) not in r.json()["detail"], "deneme sayısı sızmamalı"
    assert "email" not in r.json()["detail"].lower(), "hangi kapsamın dolduğu sızmamalı"


# ── 9) Temizlik hatası kilidi BOZMAMALI ──────────────────────────────────────
# _cleanup best-effort'tur, ama hatası aynı transaction'ı abort ettiği için
# sayaç artışları da geri alınıyordu → kilit sessizce FAIL-OPEN oluyordu.
# (Aynı hata rate_limit.py'de ölçülerek doğrulandı.)

def test_cleanup_failure_does_not_break_lockout(app, make_user, monkeypatch):
    from sqlalchemy import text as _text

    from admin import login_lockout
    from core.database import AdminLoginAttempt, SessionLocal

    monkeypatch.setattr(login_lockout, "_CLEANUP_SQL", _text("DELETE FROM tablo_yok_ki"))

    make_user("temizlik@iu.tr")
    c = _client(app, "10.0.0.90")

    r = _login(c, "temizlik@iu.tr")
    assert r.status_code == 401, f"temizlik hatasi istegi dusurdu: {r.text}"

    # ASIL İDDİA: başarısız deneme SAYILMIŞ olmalı, yoksa kilit fail-open olur.
    s = SessionLocal()
    try:
        row = s.query(AdminLoginAttempt).filter_by(
            scope="email", identifier="temizlik@iu.tr").first()
        assert row is not None and row.count == 1, "deneme sayilmadi (fail-open)"
    finally:
        s.close()


# ── 10) Esik 0 = KAPSAM KAPALI, "herkesi kilitle" DEGIL ──────────────────────
# count >= 0 her zaman dogru oldugu icin is_locked() daima True donuyordu:
# operatörün "bu kapsami kapatayim" diye 0 yazmasi, kurumu kendi paneline
# kilitlemeye yetiyordu. rate_limit._consume ile ayni sozlesme uygulanir.

def test_zero_threshold_disables_scope_instead_of_locking_everyone(app, make_user, monkeypatch):
    monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "0")
    monkeypatch.setenv("ADMIN_LOGIN_IP_MAX_ATTEMPTS", "0")

    make_user("sifir@iu.tr")
    c = _client(app, "10.0.0.99")

    # Yanlis parola: normal 401 vermeli, kilit DEGIL
    for i in range(EMAIL_MAX + 3):
        assert _login(c, "sifir@iu.tr").status_code == 401, f"{i + 1}. denemede kilitlendi"

    # Dogru parola: girise izin verilmeli
    r = c.post("/api/auth/login", json={"email": "sifir@iu.tr", "password": TEST_PASSWORD})
    assert r.status_code == 200, "esik 0 iken tum admin girisi kilitlendi"


# ── 11) Gecersiz env degeri girisi KILITLEMEZ (ValueError guard) ────────────
# _max_attempts/_ip_max_attempts/_window_minutes ciplak int() kullaniyordu ve
# is_locked() her giris denemesinde ILK calisan sey. Tek bir yazim hatasi
# (ADMIN_LOGIN_MAX_ATTEMPTS=bes) ValueError firlatir, login() 500 doner ve kurum
# kendi yonetim paneline TAMAMEN kilitlenirdi.

def test_invalid_env_value_falls_back_instead_of_500(app, make_user, monkeypatch):
    monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "bes")
    monkeypatch.setenv("ADMIN_LOGIN_IP_MAX_ATTEMPTS", "yirmi")
    monkeypatch.setenv("ADMIN_LOGIN_WINDOW_MIN", "onbes")

    make_user("gecersiz@iu.tr")
    c = _client(app, "10.0.1.10")

    # Dogru parola: 500 DEGIL, normal giris
    r = c.post("/api/auth/login", json={"email": "gecersiz@iu.tr", "password": TEST_PASSWORD})
    assert r.status_code == 200, f"gecersiz env girisi kilitledi: {r.status_code} {r.text}"

    # Yanlis parola: 500 DEGIL, normal 401
    assert _login(c, "gecersiz@iu.tr").status_code == 401


# ── 12) Sifir/negatif pencere kilidi devre disi birakamaz ──────────────────
# window_floor simdiden ILERI kayarsa _CONSUME_SQL her satiri "eski" sayar,
# sayac her istekte 1'e doner ve kilit sessizce tamamen kapanir.

def test_zero_or_negative_window_is_clamped(monkeypatch):
    from admin import login_lockout

    for deger in ("0", "-5"):
        monkeypatch.setenv("ADMIN_LOGIN_WINDOW_MIN", deger)
        assert login_lockout._window_minutes() >= 1, f"{deger} kelepcelenmedi"

    # LIMIT degerinde 0 hala "kapsam kapali" olarak GECERLI kalmali (kelepce YOK)
    monkeypatch.setenv("ADMIN_LOGIN_MAX_ATTEMPTS", "0")
    assert login_lockout._max_attempts() == 0


# ── 13) Temizlik KENDI Session'inda: hatasi istegi ve sayaci bozmaz ────────
# Onceki tasarimda temizlik caller'in Session'ini paylasiyordu; DELETE patlayinca
# transaction abort oluyor ve ayni transaction'daki sayac artislari da geri
# aliniyordu → kilit fail-open. Ayri Session bunu yapisal olarak imkansiz kilar.

def test_cleanup_failure_never_touches_request_session(app, make_user, monkeypatch):
    from sqlalchemy import text as _text

    from admin import login_lockout
    from core.database import AdminLoginAttempt, SessionLocal

    monkeypatch.setattr(login_lockout, "_CLEANUP_SQL", _text("DELETE FROM tablo_yok_ki"))

    make_user("izole@iu.tr")
    c = _client(app, "10.0.1.20")
    assert _login(c, "izole@iu.tr").status_code == 401

    s = SessionLocal()
    try:
        row = s.query(AdminLoginAttempt).filter_by(
            scope="email", identifier="izole@iu.tr").first()
    finally:
        s.close()
    assert row is not None and row.count == 1, "temizlik hatasi sayaci geri aldi (fail-open)"


# ── 14) _cleanup KENDI commit'ini yapiyor (dogrudan cagri) ────────────────
# Baska bir Session'dan, hicbir ek commit calistirmadan silmenin kalici oldugu
# dogrulanir. KABUL KRITERI: _cleanup icindeki db.commit() silinince DUSMELI.

def test_cleanup_commits_on_its_own(db):
    from datetime import timedelta

    from admin import login_lockout
    from core.database import AdminLoginAttempt, SessionLocal, utcnow

    s = SessionLocal()
    try:
        s.add(AdminLoginAttempt(
            scope="email", identifier="eski@iu.tr", count=1,
            window_started_at=utcnow() - timedelta(hours=login_lockout._RETENTION_HOURS + 1)))
        s.add(AdminLoginAttempt(
            scope="email", identifier="taze@iu.tr", count=1,
            window_started_at=utcnow()))
        s.commit()
    finally:
        s.close()

    login_lockout._cleanup()          # DOGRUDAN cagri, baska commit yok

    s = SessionLocal()
    try:
        kalanlar = {r.identifier for r in s.query(AdminLoginAttempt).all()}
    finally:
        s.close()
    assert "eski@iu.tr" not in kalanlar, "_cleanup kendi commit'ini yapmiyor"
    assert "taze@iu.tr" in kalanlar, "temizlik taze satiri da sildi"


# ── 15) Pencere retention'dan uzunsa GECERLI sayac silinmez ───────────────

def test_cleanup_respects_window_longer_than_retention(db, monkeypatch):
    from datetime import timedelta

    from admin import login_lockout
    from core.database import AdminLoginAttempt, SessionLocal, utcnow

    monkeypatch.setenv("ADMIN_LOGIN_WINDOW_MIN", str(48 * 60))   # 48 saat

    s = SessionLocal()
    try:
        s.add(AdminLoginAttempt(
            scope="email", identifier="uzun@iu.tr", count=1,
            window_started_at=utcnow() - timedelta(hours=login_lockout._RETENTION_HOURS + 6)))
        s.commit()
    finally:
        s.close()

    login_lockout._cleanup()

    s = SessionLocal()
    try:
        kaldi = s.query(AdminLoginAttempt).filter_by(identifier="uzun@iu.tr").first() is not None
    finally:
        s.close()
    assert kaldi, "pencere icindeki gecerli sayac silindi -> kilit fail-open olur"
