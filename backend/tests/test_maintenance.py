"""Bakım modu API'si: bayrak dosyası yaşam döngüsü + rol matrisi.

Bayrağın nginx tarafı (503 dönmesi) container testidir, burada test edilmez;
buradaki testler backend'in dosyayı doğru yazıp sildiğini doğrular.
"""
import pytest


@pytest.fixture()
def sup(make_user, login):
    make_user("super@iu.tr", role="super_admin")
    return login("super@iu.tr")


@pytest.fixture(autouse=True)
def flag_dir(tmp_path, monkeypatch):
    """Bayrak dizinini testin geçici dizinine yönlendirir (/app/flags yerine)."""
    monkeypatch.setenv("MAINTENANCE_FLAG_DIR", str(tmp_path))
    return tmp_path


def test_maintenance_roundtrip(sup, flag_dir):
    # Başlangıç: bayrak yok → kapalı
    r = sup.get("/api/settings/maintenance")
    assert r.status_code == 200 and r.json()["on"] is False

    # Aç → dosya oluşur, içinde denetim izi (kim açtı) vardır
    r = sup.put("/api/settings/maintenance", json={"on": True})
    assert r.status_code == 200 and r.json()["on"] is True
    flag = flag_dir / "maintenance.flag"
    assert flag.exists()
    assert "super@iu.tr" in flag.read_text(encoding="utf-8")
    assert sup.get("/api/settings/maintenance").json()["on"] is True

    # Tekrar açmak idempotent
    assert sup.put("/api/settings/maintenance", json={"on": True}).json()["on"] is True

    # Kapat → dosya silinir; tekrar kapatmak da idempotent
    r = sup.put("/api/settings/maintenance", json={"on": False})
    assert r.status_code == 200 and r.json()["on"] is False
    assert not flag.exists()
    assert sup.put("/api/settings/maintenance", json={"on": False}).status_code == 200


def test_maintenance_requires_super_admin(client, make_user, login):
    # Anonim → 401
    assert client.get("/api/settings/maintenance").status_code == 401
    assert client.put("/api/settings/maintenance", json={"on": True}).status_code == 401
    # admin rolü yetmez → 403 (yalnızca super_admin)
    make_user("a@iu.tr", role="admin")
    c = login("a@iu.tr")
    assert c.get("/api/settings/maintenance").status_code == 403
    assert c.put("/api/settings/maintenance", json={"on": True}).status_code == 403
