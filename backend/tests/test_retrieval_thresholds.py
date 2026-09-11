"""Getirme eşiklerinin ortam değişkeni doğrulaması.

NEDEN VAR: eşikler ortama taşındığında "0.90" yerine "90" yazmak mümkün
hâle geldi. Skorlar [0,1] aralığında olduğu için bu, hiçbir hit'in eşiği
geçememesi demek — bot her soruya "Bu konuda bilgim bulunmuyor." der ve
loga hiçbir şey düşmez. Bir kez canlı ortamda oldu; bu yüzden açılışta
düşüyoruz ve bu test o davranışı çiviliyor.
"""
import pytest

from services.answer_pipeline import _threshold


def test_varsayilanlar_gecerli():
    assert _threshold("MEILI_THERESHOLD_YOK", "0.90") == 0.90
    assert _threshold("QDRANT_THERESHOLD_YOK", "0.75") == 0.75


def test_ortamdaki_deger_varsayilani_ezer(monkeypatch):
    monkeypatch.setenv("TEST_ESIK", "0.55")
    assert _threshold("TEST_ESIK", "0.90") == 0.55


@pytest.mark.parametrize("kotu", ["90", "75", "1.5", "-0.1", "100"])
def test_aralik_disi_deger_reddedilir(monkeypatch, kotu):
    monkeypatch.setenv("TEST_ESIK", kotu)
    with pytest.raises(RuntimeError, match="0 ile 1 arasında"):
        _threshold("TEST_ESIK", "0.90")


def test_yuzde_yazan_kullaniciya_dogru_degeri_soyler(monkeypatch):
    monkeypatch.setenv("TEST_ESIK", "90")
    with pytest.raises(RuntimeError, match=r"0\.9 kullanın"):
        _threshold("TEST_ESIK", "0.90")


def test_sayi_olmayan_deger_reddedilir(monkeypatch):
    monkeypatch.setenv("TEST_ESIK", "yüksek")
    with pytest.raises(RuntimeError, match="sayı olmalı"):
        _threshold("TEST_ESIK", "0.90")


def test_sinir_degerleri_kabul_edilir(monkeypatch):
    for sinir in ("0", "0.0", "1", "1.0"):
        monkeypatch.setenv("TEST_ESIK", sinir)
        assert 0.0 <= _threshold("TEST_ESIK", "0.90") <= 1.0
