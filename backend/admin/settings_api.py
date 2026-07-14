"""Ayarlar sayfası API'si — yalnızca super_admin erişir (AdminAuthMiddleware
/api/settings yolunu super_admin'e kilitler; bkz. auth.py _ROLE_RULES).

İçerik:
- Kullanıcı yönetimi: listele / oluştur / güncelle (rol, ad, aktiflik, parola)
- LLM ayarları: aç-kapa + OpenRouter API anahtarı (DB'de tutulur, .env'i ezer)

Kilitlenme korumaları:
- Kullanıcı KENDİ rolünü/aktifliğini değiştiremez.
- Son aktif super_admin pasifleştirilemez / rolü düşürülemez.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import AdminSession, AdminUser, SystemConfig
from core.deps import OPENROUTER_KEY_CONFIG, get_db
from admin.auth import (
    VALID_ROLES,
    current_user as _current_user,
    hash_password,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# _current_user: istek sahibi (self-lockout korumaları için) — auth.current_user.


# ─────────────────────────────────────────────
#  Kullanıcı Yönetimi
# ─────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(min_length=10, max_length=200)
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: str = "editor"


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: Optional[str] = None
    is_active: Optional[bool] = None
    # Dolu gelirse parola sıfırlanır (ve kullanıcının açık oturumları kapatılır)
    password: Optional[str] = Field(default=None, min_length=10, max_length=200)


def _user_row(u: AdminUser) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "is_active": u.is_active == 1,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def _active_super_admin_count(db: Session, exclude_id: Optional[int] = None) -> int:
    q = db.query(AdminUser).filter(AdminUser.role == "super_admin", AdminUser.is_active == 1)
    if exclude_id is not None:
        q = q.filter(AdminUser.id != exclude_id)
    return q.count()


def _kill_sessions(db: Session, user_id: int) -> None:
    db.query(AdminSession).filter(AdminSession.user_id == user_id).delete()


@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    rows = db.query(AdminUser).order_by(AdminUser.id).all()
    return {"users": [_user_row(u) for u in rows]}


@router.post("/users", status_code=201)
def create_user(body: UserCreateRequest, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Geçerli bir e-posta girin.")
    if body.role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Geçersiz rol.")
    if db.query(AdminUser).filter(AdminUser.email == email).first():
        raise HTTPException(status_code=409, detail="Bu e-posta ile bir kullanıcı zaten var.")

    user = AdminUser(
        email=email,
        password_hash=hash_password(body.password),
        full_name=(body.full_name or "").strip() or None,
        role=body.role,
        is_active=1,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_row(user)


@router.put("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdateRequest,
    db: Session = Depends(get_db),
    me: Optional[AdminUser] = Depends(_current_user),
):
    user = db.query(AdminUser).filter(AdminUser.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Kullanıcı bulunamadı.")

    is_self = me is not None and me.id == user.id

    if body.role is not None and body.role != user.role:
        if body.role not in VALID_ROLES:
            raise HTTPException(status_code=400, detail="Geçersiz rol.")
        if is_self:
            # Kendi rolünü düşürüp ayarlar sayfasından kilitlenmeyi engeller.
            raise HTTPException(status_code=400, detail="Kendi rolünüzü değiştiremezsiniz.")
        if user.role == "super_admin" and _active_super_admin_count(db, exclude_id=user.id) == 0:
            raise HTTPException(status_code=400, detail="Son super admin'in rolü düşürülemez.")
        user.role = body.role
        # Rol değişti → açık oturumlar eski yetkiyle devam etmesin.
        _kill_sessions(db, user.id)

    if body.is_active is not None and (user.is_active == 1) != body.is_active:
        if is_self:
            raise HTTPException(status_code=400, detail="Kendi hesabınızı pasifleştiremezsiniz.")
        if not body.is_active and user.role == "super_admin" and _active_super_admin_count(db, exclude_id=user.id) == 0:
            raise HTTPException(status_code=400, detail="Son super admin pasifleştirilemez.")
        user.is_active = 1 if body.is_active else 0
        if not body.is_active:
            _kill_sessions(db, user.id)

    if body.full_name is not None:
        user.full_name = body.full_name.strip() or None

    if body.password:
        user.password_hash = hash_password(body.password)
        # Parola değişti → güvenlik gereği tüm oturumları kapat (kendi
        # oturumu dahil; kullanıcı yeni parolayla tekrar girer).
        _kill_sessions(db, user.id)

    db.commit()
    db.refresh(user)
    return _user_row(user)


# ─────────────────────────────────────────────
#  LLM Ayarları (aç-kapa + OpenRouter anahtarı)
# ─────────────────────────────────────────────

class LLMSettingsRequest(BaseModel):
    enabled: Optional[bool] = None
    # "" (boş string) = DB'deki anahtarı sil, .env'e geri dön
    openrouter_api_key: Optional[str] = Field(default=None, max_length=500)


def _get_config(db: Session, key: str) -> Optional[str]:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    return row.value if row else None


def _set_config(db: Session, key: str, value: Optional[str]) -> None:
    row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
    if value is None or value == "":
        if row:
            db.delete(row)
        return
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=key, value=value))


def mask_key(key: Optional[str]) -> Optional[str]:
    """Anahtarın tamamı asla geri dönmez; yalnızca tanınabilir uçları döner."""
    if not key:
        return None
    if len(key) <= 12:
        return "••••"
    return f"{key[:9]}…{key[-4:]}"


@router.get("/llm")
def get_llm_settings(db: Session = Depends(get_db)):
    import os
    enabled = (_get_config(db, "LLM_ENABLED") or "false").lower() == "true"
    db_key = _get_config(db, OPENROUTER_KEY_CONFIG)
    env_key = os.getenv("OPENROUTER_API_KEY") or None
    effective = db_key or env_key
    return {
        "enabled": enabled,
        "openrouter_key_masked": mask_key(effective),
        # UI "anahtar nereden geliyor" gösterebilsin: db (panelden girildi),
        # env (sunucu .env'i), none (anahtar yok)
        "key_source": "db" if db_key else ("env" if env_key else "none"),
    }


@router.put("/llm")
def update_llm_settings(body: LLMSettingsRequest, db: Session = Depends(get_db)):
    if body.enabled is not None:
        _set_config(db, "LLM_ENABLED", "true" if body.enabled else "false")
    if body.openrouter_api_key is not None:
        key = body.openrouter_api_key.strip()
        if key and (len(key) < 20 or " " in key):
            raise HTTPException(status_code=400, detail="Anahtar geçersiz görünüyor (en az 20 karakter, boşluksuz).")
        _set_config(db, OPENROUTER_KEY_CONFIG, key)  # "" → sil, .env'e dön
    db.commit()
    return get_llm_settings(db)
