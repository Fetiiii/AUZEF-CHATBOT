"""Oturum sistemi: login/logout/me, cookie bayrakları, oturum ölümü."""


def test_login_wrong_password_unknown_user_inactive_all_same_401(client, make_user):
    make_user("aktif@iu.tr")
    make_user("pasif@iu.tr", active=False)

    for payload in [
        {"email": "aktif@iu.tr", "password": "yanlis-parola"},
        {"email": "bilinmeyen@iu.tr", "password": "parola-1234"},
        {"email": "pasif@iu.tr", "password": "parola-1234"},
    ]:
        r = client.post("/api/auth/login", json=payload)
        # Hepsi aynı mesaj: hesap varlığı/pasifliği bilgisi sızdırılmaz
        assert r.status_code == 401
        assert r.json()["detail"] == "E-posta ya da parola hatalı."


def test_login_success_sets_httponly_lax_cookie(client, make_user):
    make_user("a@iu.tr", name="Ad Soyad")
    r = client.post("/api/auth/login", json={"email": " A@iu.tr ", "password": "parola-1234"})
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "a@iu.tr"
    assert r.json()["user"]["full_name"] == "Ad Soyad"
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/api" in set_cookie


def test_me_and_logout_cycle(login, make_user):
    make_user("a@iu.tr", role="editor")
    c = login("a@iu.tr")
    me = c.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"]["role"] == "editor"

    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401


def test_fake_token_rejected(client, make_user):
    make_user("a@iu.tr")
    client.cookies.set("auzef_admin_session", "sahte-token")
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/qna").status_code == 401


def test_deactivation_kills_live_session(login, make_user, db):
    from database import AdminUser, AdminSession
    u = make_user("a@iu.tr", role="editor")
    c = login("a@iu.tr")
    assert c.get("/api/qna").status_code == 200

    # create_admin --deactivate ile aynı işlem
    u = db.query(AdminUser).filter(AdminUser.id == u.id).first()
    u.is_active = 0
    db.query(AdminSession).filter(AdminSession.user_id == u.id).delete()
    db.commit()

    assert c.get("/api/qna").status_code == 401
