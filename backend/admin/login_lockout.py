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

from core.database import utcnow

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


def _max_attempts() -> int:
    return int(os.getenv("ADMIN_LOGIN_MAX_ATTEMPTS", "").strip() or "5")


def _ip_max_attempts() -> int:
    return int(os.getenv("ADMIN_LOGIN_IP_MAX_ATTEMPTS", "").strip() or "20")


def _window_minutes() -> int:
    return int(os.getenv("ADMIN_LOGIN_WINDOW_MIN", "").strip() or "15")


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


def _cleanup(db: Session) -> None:
    """Eski sayaç satırlarını fırsattan istifade siler.

    TAM İZOLE: kendi commit'ini yapar, hatada rollback eder. Çağıran, sayaç
    artışlarını bu çağrıdan ÖNCE commit etmiş olmalıdır.

    NEDEN İZOLASYON ŞART: "best-effort" olmak için except ile yutmak YETMEZ.
    DELETE bir DBAPI hatası verdiğinde (kilit çakışması, deadlock, statement
    timeout) transaction ABORT olur ve aynı transaction'da bekleyen sayaç
    artışları da onunla geri alınır. Sonuç: giriş denemesi sayılmaz, yani kilit
    SESSİZCE FAIL-OPEN olur — üstelik deadlock olasılığı tam da yük altında,
    korumanın en çok gerektiği anda yükselir.
    (Aynı hata rate_limit.py'de ölçülerek doğrulandı; bkz. o dosyadaki not.)
    """
    try:
        db.execute(_CLEANUP_SQL, {"cutoff": utcnow() - timedelta(hours=_RETENTION_HOURS)})
        db.commit()
    except Exception as exc:  # pragma: no cover - savunma amaçlı
        logger.warning("Admin login sayaç temizliği başarısız: %s", exc)
        try:
            # Abort olmuş transaction'ı temizle ki isteğin geri kalanı
            # (oturum oluşturma vb.) çalışabilsin.
            db.rollback()
        except Exception:
            pass


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
    # Sayaçlar ÖNCE kalıcı olur; temizlik bundan SONRA ve kendi
    # transaction'ında çalışır (hatası artışları geri alamaz — bkz. _cleanup).
    db.commit()
    _cleanup(db)

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
