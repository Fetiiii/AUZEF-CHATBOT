"""Rol sistemi: yetki matrisi + kilitlenme korumaları."""
import pytest

from conftest import TEST_PASSWORD


@pytest.fixture()
def trio(make_user, login):
    make_user("super@iu.tr", role="super_admin")
    make_user("admin@iu.tr", role="admin")
    make_user("editor@iu.tr", role="editor")
    return login("super@iu.tr"), login("admin@iu.tr"), login("editor@iu.tr")


# (yol, editor, admin, super) — DEPLOY.md'deki "klasik ayrım" matrisi
MATRIX = [
    ("/api/qna",               200, 200, 200),
    ("/api/academic-calendar", 200, 200, 200),
    ("/api/conversations",     403, 200, 200),
    ("/api/stats",             403, 200, 200),
    ("/api/settings/users",    403, 403, 200),
    ("/api/settings/llm",      403, 403, 200),
]


@pytest.mark.parametrize("path,editor_exp,admin_exp,super_exp", MATRIX)
def test_permission_matrix(trio, client, path, editor_exp, admin_exp, super_exp):
    sup, adm, edi = trio
    assert client.get(path).status_code == 401          # oturumsuz
    assert edi.get(path).status_code == editor_exp
    assert adm.get(path).status_code == admin_exp
    assert sup.get(path).status_code == super_exp


def test_role_change_kills_sessions_and_new_login_gets_new_role(trio, login):
    sup, _, edi = trio
    users = sup.get("/api/settings/users").json()["users"]
    editor_id = next(u["id"] for u in users if u["email"] == "editor@iu.tr")

    assert edi.get("/api/qna").status_code == 200
    r = sup.put(f"/api/settings/users/{editor_id}", json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"

    # Eski oturum rol değişiminde ölür; yeni oturum yeni yetkiyle gelir
    assert edi.get("/api/qna").status_code == 401
    yeni = login("editor@iu.tr")
    assert yeni.get("/api/conversations").status_code == 200


def test_cannot_change_own_role_or_deactivate_self(trio):
    sup, _, _ = trio
    users = sup.get("/api/settings/users").json()["users"]
    my_id = next(u["id"] for u in users if u["email"] == "super@iu.tr")

    assert sup.put(f"/api/settings/users/{my_id}", json={"role": "editor"}).status_code == 400
    assert sup.put(f"/api/settings/users/{my_id}", json={"is_active": False}).status_code == 400


def test_last_super_admin_protected(trio, login):
    sup, _, _ = trio
    r = sup.post("/api/settings/users", json={
        "email": "super2@iu.tr", "password": TEST_PASSWORD, "role": "super_admin"})
    assert r.status_code == 201
    sup2 = login("super2@iu.tr", TEST_PASSWORD)

    users = sup.get("/api/settings/users").json()["users"]
    first_id = next(u["id"] for u in users if u["email"] == "super@iu.tr")
    second_id = next(u["id"] for u in users if u["email"] == "super2@iu.tr")

    # İki super varken biri düşürülebilir…
    assert sup2.put(f"/api/settings/users/{first_id}", json={"role": "admin"}).status_code == 200
    # …ama kalan SON super düşürülemez / pasifleştirilemez (başkası tarafından da)
    admin_client = login("super@iu.tr")  # artık admin rolünde
    assert admin_client.put(f"/api/settings/users/{second_id}", json={"role": "admin"}).status_code == 403  # admin settings'e giremez
    assert sup2.put(f"/api/settings/users/{second_id}", json={"role": "admin"}).status_code == 400


def test_unknown_role_in_db_is_fail_closed(make_user, login, db):
    from core.database import AdminUser
    u = make_user("garip@iu.tr", role="admin")
    c = login("garip@iu.tr")
    db.query(AdminUser).filter(AdminUser.id == u.id).update({"role": "bozuk_rol"})
    db.commit()
    # Bilinmeyen rol seviye 0 → hiçbir korumalı uca giremez
    assert c.get("/api/qna").status_code == 403
