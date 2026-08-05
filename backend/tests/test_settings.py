"""Ayarlar API'si: kullanıcı CRUD hataları + LLM ayarları."""
import pytest

from conftest import TEST_PASSWORD, TEST_PASSWORD_ALT


@pytest.fixture()
def sup(make_user, login):
    make_user("super@iu.tr", role="super_admin")
    return login("super@iu.tr")


def test_create_user_and_error_paths(sup):
    r = sup.post("/api/settings/users", json={
        "email": " Yeni@iu.tr ", "password": TEST_PASSWORD,
        "full_name": "Yeni Editör", "role": "editor"})
    assert r.status_code == 201
    body = r.json()
    assert body["email"] == "yeni@iu.tr"          # normalize edilir
    assert body["role"] == "editor" and body["is_active"] is True

    # Aynı e-posta → 409
    assert sup.post("/api/settings/users", json={
        "email": "yeni@iu.tr", "password": TEST_PASSWORD, "role": "editor"}).status_code == 409
    # Geçersiz rol → 400
    assert sup.post("/api/settings/users", json={
        "email": "x@iu.tr", "password": TEST_PASSWORD, "role": "patron"}).status_code == 400
    # Kısa parola → 422 (pydantic). Buradaki "kisa" bir kimlik bilgisi DEĞİL,
    # min_length=10 doğrulamasını tetikleyen geçersiz girdi örneğidir — bu
    # yüzden sabite çevrilmedi.
    assert sup.post("/api/settings/users", json={
        "email": "x@iu.tr", "password": "kisa", "role": "editor"}).status_code == 422
    # E-posta biçimsiz → 400
    assert sup.post("/api/settings/users", json={
        "email": "epostadegil", "password": TEST_PASSWORD, "role": "editor"}).status_code == 400


def test_password_reset_kills_sessions(sup, login):
    r = sup.post("/api/settings/users", json={
        "email": "k@iu.tr", "password": TEST_PASSWORD, "role": "editor"})
    uid = r.json()["id"]
    c = login("k@iu.tr", TEST_PASSWORD)
    assert c.get("/api/qna").status_code == 200

    assert sup.put(f"/api/settings/users/{uid}", json={"password": TEST_PASSWORD_ALT}).status_code == 200
    assert c.get("/api/qna").status_code == 401                     # eski oturum öldü
    assert login("k@iu.tr", TEST_PASSWORD_ALT).get("/api/qna").status_code == 200


def test_llm_settings_roundtrip_and_masking(sup):
    # Başlangıç: enabled false (system_config boş), anahtar kaynağı env ya da yok
    r = sup.get("/api/settings/llm")
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # Aç + anahtar gir
    r = sup.put("/api/settings/llm", json={
        "enabled": True, "openrouter_api_key": "sk-or-v1-0123456789abcdef0123"})
    j = r.json()
    assert r.status_code == 200
    assert j["enabled"] is True and j["key_source"] == "db"
    assert j["openrouter_key_masked"].startswith("sk-or-v1-")
    assert "0123456789abcdef" not in j["openrouter_key_masked"]     # tam anahtar asla dönmez

    # Geçersiz anahtar reddedilir
    assert sup.put("/api/settings/llm", json={"openrouter_api_key": "kisa key"}).status_code == 400

    # Boş string = DB anahtarını sil (env'e/none'a dön)
    r = sup.put("/api/settings/llm", json={"openrouter_api_key": ""})
    assert r.json()["key_source"] in ("env", "none")


def test_db_key_overrides_env_for_llm_provider(sup, db, monkeypatch):
    """get_llm_provider: DB anahtarı .env'i ezer ve değişince istemci yenilenir."""
    import core.deps as deps
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-envkey-0123456789")

    p_env = deps.get_llm_provider(db)
    assert p_env is not None

    sup.put("/api/settings/llm", json={"openrouter_api_key": "sk-or-v1-dbkey-0123456789xx"})
    p_db = deps.get_llm_provider(db)
    assert p_db is not None and p_db is not p_env      # anahtar değişti → yeni istemci

    # DB anahtarı silinince env'e dönülür
    sup.put("/api/settings/llm", json={"openrouter_api_key": ""})
    p_back = deps.get_llm_provider(db)
    assert p_back is not None and p_back is not p_db
