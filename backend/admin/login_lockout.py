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
    """Best-effort: temizlik hatası girişi engellemeli değil."""
    try:
        db.execute(_CLEANUP_SQL, {"cutoff": utcnow() - timedelta(hours=_RETENTION_HOURS)})
    except Exception as exc:  # pragma: no cover - savunma amaçlı
        logger.warning("Admin login sayaç temizliği başarısız: %s", exc)


def is_locked(db: Session, email: str, ip: Optional[str]) -> bool:
    """Salt-okunur kontrol — sayacı ARTIRMAZ.

    login() bunu bcrypt'ten ÖNCE çağırır: kilitli durumda pahalı hash
    hesaplaması hiç yapılmasın (bcrypt bilinçli olarak yavaştır)."""
    window_min = _window_minutes()
    if _current_count(db, SCOPE_EMAIL, email, window_min) >= _max_attempts():
        return True
    if ip and _current_count(db, SCOPE_IP, ip, window_min) >= _ip_max_attempts():
        return True
    return False


def register_failure(db: Session, email: str, ip: Optional[str]) -> bool:
    """Başarısız denemeyi işler; bu istek bir eşiği YENİ AŞTIYSA True döner.

    Hem bilinmeyen e-posta hem yanlış parola denemesi burada sayılır: ip
    kapsamı sahte e-postalarla bile dolmalı (spray koruması) — rate_limit.py'nin
    "başarısız TC denemesi de sayılır" ilkesiyle aynı mantık.
    """
    window_min = _window_minutes()
    email_count = _consume(db, SCOPE_EMAIL, email, window_min)
    ip_count = _consume(db, SCOPE_IP, ip, window_min) if ip else 0
    _cleanup(db)
    db.commit()

    exceeded = email_count >= _max_attempts() or ip_count >= _ip_max_attempts()
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
