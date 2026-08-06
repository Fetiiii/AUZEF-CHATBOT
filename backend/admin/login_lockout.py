"""Admin girişi kaba kuvvet koruması (ANALIZ.md P0-3).

Sorun: ``/api/auth/login`` IP başına saniyede 10 istekle sınırlıydı (chat_limit —
sohbet trafiği için ayarlanmış bir bölge, parola koruması için fiilen etkisizdi)
ve backend'de deneme sayacı/hesap kilidi hiç yoktu.

İki AYRI saldırı şekli var, korumaları farklı:

1. **Hedefli tahmin** — bilinen tek bir e-postaya art arda parola denemesi.
   ``email`` kapsamı bunu bütçeler.
2. **Spray** — tek kaynaktan (bir IP) birçok farklı hesabı denemek. ``email``
   kapsamı bunu DURDURMAZ (her hesap yalnızca bir-iki kez denenir); ``ip``
   kapsamı bunun içindir.

Bu yüzden iki kapsam da gereklidir; biri diğerinin yerini tutmaz.

Tasarım notları
---------------
- **Durum DB'de, sabit pencere, atomik UPSERT artırma.** Aynı prensip
  ``integrations/solution_center/rate_limit.py`` (P0-1) ile — oradaki gerekçeler
  (2 worker, yarış koşulu, sabit pencerenin yeterliliği) burada da geçerli.
- **HASH'LEME YOK.** rate_limit.py TC'yi (hassas kimlik verisi) hash'liyordu; e-posta
  ve IP burada zaten sistemde açık metin saklanıyor (``AdminUser.email``,
  ``Conversation.ip_address``) — gizlenecek ek bir şey yok.
- **Config env'den ÇAĞRI ANINDA okunur** (``os.getenv``), modül yüklenirken
  donmaz — ``admin/settings_api.py``'deki ``_maintenance_flag()`` kalıbıyla aynı.
  Testler ``monkeypatch.setenv(...)`` ile eşikleri değiştirebilir; solution_center'ın
  FastAPI ``Depends`` tabanlı DI'ına burada gerek yok çünkü mocklanacak bir dış
  HTTP istemcisi yok.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import SessionLocal, utcnow

logger = logging.getLogger("auzef")

SCOPE_EMAIL = "email"
SCOPE_IP = "ip"

# Sayaç satırlarının saklanma süresi; bundan eskiler fırsattan istifade silinir.
_RETENTION_HOURS = 24

# rate_limit.py'deki _CONSUME_SQL ile aynı kalıp: pencere tabanından eskiyse
# sayaç 1'e ve pencere şimdiye çekilir; değilse count bir artar. RETURNING
# artırılmış değeri döner → ayrı bir SELECT gerekmez (yarış yok).
_CONSUME_SQL = text("""
    INSERT INTO admin_login_attempts (scope, identifier, count, window_started_at)
    VALUES (:scope, :identifier, 1, :now)
    ON CONFLICT (scope, identifier) DO UPDATE SET
        count = CASE
            WHEN admin_login_attempts.window_started_at < :window_floor THEN 1
            ELSE admin_login_attempts.count + 1
        END,
        window_started_at = CASE
            WHEN admin_login_attempts.window_started_at < :window_floor THEN :now
            ELSE admin_login_attempts.window_started_at
        END
    RETURNING count
""")

_CURRENT_SQL = text("""
    SELECT count FROM admin_login_attempts
    WHERE scope = :scope AND identifier = :identifier AND window_started_at >= :window_floor
""")

_RESET_SQL = text("DELETE FROM admin_login_attempts WHERE scope = :scope AND identifier = :identifier")

_CLEANUP_SQL = text("DELETE FROM admin_login_attempts WHERE window_started_at < :cutoff")


def _env_int(key: str, default: int, minimum: Optional[int] = None) -> int:
    """Dayanıklı int env okuması (base_client._env_int / _env_int_min karşılığı).

    NEDEN try/except ŞART: bu üç değer `is_locked()` üzerinden HER giriş
    denemesinde okunuyor (bkz. auth.login). Çıplak `int()` ile
    `ADMIN_LOGIN_MAX_ATTEMPTS=bes` gibi TEK bir yazım hatası ValueError
    fırlatır, login() 500 döner ve kurum kendi yönetim paneline TAMAMEN
    kilitlenir. Geçersiz değerde sessizce varsayılana düşmek doğru davranış:
    yapılandırma hatası korumayı kapatmamalı ama erişimi de öldürmemeli.

    `minimum` YALNIZCA pencere değerleri için verilir — 0/negatif pencere
    `window_floor`u ileri kaydırır, `_CONSUME_SQL` her satırı "eski" sayar ve
    sayaç her istekte 1'e döner; yani kilit sessizce tamamen devre dışı kalır.
    LİMİT (adet) değerlerinde 0 = "kapsamı kapat" BİLİNÇLİ sözleşmedir
    (bkz. _scope_locked), o yüzden orada kelepçe YOK.
    """
    try:
        value = int(os.getenv(key, "").strip() or default)
    except ValueError:
        logger.warning("Geçersiz %s değeri — varsayılana (%s) düşülüyor.", key, default)
        value = default
    return max(value, minimum) if minimum is not None else value


def _max_attempts() -> int:
    return _env_int("ADMIN_LOGIN_MAX_ATTEMPTS", 5)


def _ip_max_attempts() -> int:
    return _env_int("ADMIN_LOGIN_IP_MAX_ATTEMPTS", 20)


def _window_minutes() -> int:
    return _env_int("ADMIN_LOGIN_WINDOW_MIN", 15, minimum=1)


def _current_count(db: Session, scope: str, identifier: str, window_min: int) -> int:
    row = db.execute(_CURRENT_SQL, {
        "scope": scope,
        "identifier": identifier,
        "window_floor": utcnow() - timedelta(minutes=window_min),
    }).first()
    return row[0] if row else 0


def _consume(db: Session, scope: str, identifier: str, window_min: int) -> int:
    now = utcnow()
    return db.execute(_CONSUME_SQL, {
        "scope": scope,
        "identifier": identifier,
        "now": now,
        "window_floor": now - timedelta(minutes=window_min),
    }).scalar_one()


def _cleanup_cutoff():
    """Temizlik eşiği: saklama süresi VE yapılandırılmış pencere.

    Eşik sabit _RETENTION_HOURS olsaydı, operatör pencereyi retention'dan uzun
    ayarladığında (ör. ADMIN_LOGIN_WINDOW_MIN=2880 → 48 saat) temizlik HÂLÂ
    GEÇERLİ bir sayacı 24. saatte siler, sayaç sıfırlanır ve kilit fail-open
    olurdu. Eşiği pencereye göre almak bunu yapısal olarak imkânsız kılar.
    (rate_limit._cleanup_cutoff ile aynı mantık.)
    """
    return utcnow() - max(
        timedelta(hours=_RETENTION_HOURS),
        timedelta(minutes=_window_minutes()),
    )


def _cleanup() -> None:
    """Eski sayaç satırlarını siler — KENDİ Session'ında.

    NEDEN AYRI SESSION: "best-effort" olmak için except ile yutmak YETMEZ.
    DELETE bir DBAPI hatası verdiğinde (kilit çakışması, deadlock, statement
    timeout) transaction ABORT olur; temizlik çağıranın Session'ını paylaşsaydı
    aynı transaction'daki sayaç artışları da onunla geri alınırdı → giriş
    denemesi sayılmaz, kilit SESSİZCE FAIL-OPEN olur. Üstelik deadlock
    olasılığı tam da yük altında, korumanın en çok gerektiği anda yükselir.

    Ayrı Session bunu MİMARİYLE çözer: çağıranın transaction'ı hiç görülmez,
    dolayısıyla ne DELETE hatası ne de rollback hatası ona ulaşabilir. Önceki
    tasarımda rollback'in KENDİSİ de patlarsa Session bozuk kalıyor ve isteğin
    sonraki adımları çok daha belirsiz bir hatayla patlıyordu.

    Açık rollback YOK: Session.close() bekleyen/abort olmuş transaction'ı zaten
    atar ve bu Session hemen çöpe gider.

    Kalıp: core/deps.log_query ve rate_limit.SmsRateLimiter._cleanup ile aynı.
    """
    db = SessionLocal()
    try:
        db.execute(_CLEANUP_SQL, {"cutoff": _cleanup_cutoff()})
        db.commit()
    except Exception as exc:
        # exc_info=True: hata yutulduğu için istek normal devam ediyor; geriye
        # tek iz bu satır kalıyor. Traceback olmadan "deadlock mı, statement
        # timeout mu, bağlantı mı koptu" ayırt edilemez. Sızıntı riski yok:
        # _CLEANUP_SQL'in tek parametresi bir zaman damgası.
        logger.warning("Admin login sayaç temizliği başarısız: %s", exc, exc_info=True)
    finally:
        db.close()


def _scope_locked(db: Session, scope: str, identifier: str,
                  limit: int, window_min: int) -> bool:
    """Kapsam dolmuş mu?

    limit <= 0 ise kapsam DEVRE DIŞIDIR — rate_limit.py'deki `_consume` ile aynı
    sözleşme. Bu kontrol olmadan `count >= 0` her zaman doğru olur, `is_locked()`
    daima True döner ve TÜM admin girişi kilitlenirdi: operatörün "bu kapsamı
    kapatayım" diye 0 yazması, kurumu kendi paneline kilitlemeye yeterdi."""
    if limit <= 0:
        return False
    return _current_count(db, scope, identifier, window_min) >= limit


def is_locked(db: Session, email: str, ip: Optional[str]) -> bool:
    """Salt-okunur kontrol — sayacı ARTIRMAZ.

    login() bunu bcrypt'ten ÖNCE çağırır: kilitli durumda pahalı hash
    hesaplaması hiç yapılmasın (bcrypt bilinçli olarak yavaştır)."""
    window_min = _window_minutes()
    if _scope_locked(db, SCOPE_EMAIL, email, _max_attempts(), window_min):
        return True
    if ip and _scope_locked(db, SCOPE_IP, ip, _ip_max_attempts(), window_min):
        return True
    return False


def register_failure(db: Session, email: str, ip: Optional[str]) -> bool:
    """Başarısız denemeyi işler; bu istek bir eşiği YENİ AŞTIYSA True döner.

    Hem bilinmeyen e-posta hem yanlış parola denemesi burada sayılır: ip
    kapsamı sahte e-postalarla bile dolmalı (spray koruması) — rate_limit.py'nin
    "başarısız TC denemesi de sayılır" ilkesiyle aynı mantık.
    """
    window_min = _window_minutes()
    email_max = _max_attempts()
    ip_max = _ip_max_attempts()

    # limit <= 0 → kapsam devre dışı: sayaç hiç artırılmaz (rate_limit._consume
    # ile aynı sözleşme; bkz. _scope_locked notu).
    email_count = _consume(db, SCOPE_EMAIL, email, window_min) if email_max > 0 else 0
    ip_count = _consume(db, SCOPE_IP, ip, window_min) if (ip and ip_max > 0) else 0
    # Sayaçları kalıcı yap, sonra temizliği tetikle.
    #
    # DİKKAT — garantiyi sağlayan şey bu SIRA DEĞİL, _cleanup'ın kendi
    # Session'ında çalışması. Sıra yalnızca netlik için burada. Temizlik bu
    # Session'a hiç dokunmadığı için sırayı bozmak da artışları geri alamaz.
    db.commit()
    _cleanup()

    exceeded = ((email_max > 0 and email_count >= email_max)
                or (ip_max > 0 and ip_count >= ip_max))
    if exceeded:
        # Log'a yalnızca kapsam bilgisi yazılır — parola ASLA, e-posta/IP'nin
        # kendisi de değil (denetim izi yeterli: "bir kilitlenme oldu").
        logger.warning("Admin login kilitlendi (e-posta ve/ya IP eşiği aşıldı)")
    return exceeded


def reset(db: Session, email: str, ip: Optional[str]) -> None:
    """Başarılı girişte iki kapsamın sayaçlarını temizler."""
    db.execute(_RESET_SQL, {"scope": SCOPE_EMAIL, "identifier": email})
    if ip:
        db.execute(_RESET_SQL, {"scope": SCOPE_IP, "identifier": ip})
    db.commit()
