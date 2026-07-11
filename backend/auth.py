"""Admin kullanıcı sistemi: oturum (session cookie) tabanlı kimlik doğrulama.

Tasarım kararları:
- JWT değil SESSION COOKIE: admin SPA'sı API ile aynı origin'de servis edildiği
  için cookie doğal çalışır; oturum DB'de olduğundan anında iptal edilebilir
  ("bu personelin erişimini şimdi kapat" = satır silmek). HttpOnly cookie
  JavaScript'e görünmez — olası bir XSS bile token'ı çalamaz.
- Token DB'de SHA-256 hash'iyle saklanır: DB sızıntısında ham token ele geçmez.
- Parolalar bcrypt (maliyetli hash) — brute-force'u doğal yavaşlatır.
- CSRF: SameSite=Lax cookie, cross-site POST'larda gönderilmez; admin uçları
  yalnızca unsafe metodlarla değiştirildiğinden bu koruma pratikte yeterlidir.

Devreye alma (geçiş planı):
- ADMIN_AUTH_ENFORCED=false (varsayılan) iken middleware hiçbir isteği
  engellemez; admin uçlarını bugün nginx Basic Auth koruyor.
- Angular login sayfası hazır olunca .env'de ADMIN_AUTH_ENFORCED=true yapılır
  ve nginx'teki geçici Basic Auth bloğu kaldırılır.
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from database import SessionLocal, AdminUser, AdminSession

COOKIE_NAME = "auzef_admin_session"
SESSION_HOURS = 12
# Varsayılan TRUE (güvenli varsayılan): env unutulursa sistem AÇIK değil KAPALI
# kalır — admin paneli login sayfası üzerinden her zaman erişilebilir.
# Yalnızca yerel geliştirmede bilinçli olarak false yapılır.
AUTH_ENFORCED = (os.getenv("ADMIN_AUTH_ENFORCED") or "true").strip().lower() == "true"
# Yerel geliştirme (http://localhost) için ADMIN_COOKIE_SECURE=false yapılabilir;
# production'da true kalmalı (cookie yalnızca HTTPS üzerinden taşınır).
COOKIE_SECURE = (os.getenv("ADMIN_COOKIE_SECURE") or "true").strip().lower() == "true"

# Kullanıcı bulunamadığında da bcrypt doğrulaması yapılır ki "e-posta var mı"
# bilgisi yanıt süresinden sızmasın (timing attack).
_DUMMY_HASH = bcrypt.hashpw(b"dummy-timing-guard", bcrypt.gensalt()).decode()


# ── Saf yardımcılar ───────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Oturum işlemleri ─────────────────────────────────────────────────────────

def create_session(db: Session, user: AdminUser) -> str:
    """Yeni oturum açar, ham token'ı döner (yalnızca cookie'ye yazılır)."""
    # Süresi geçmiş oturumları fırsattan istifade temizle (tablo şişmesin).
    db.query(AdminSession).filter(AdminSession.expires_at < datetime.utcnow()).delete()
    token = secrets.token_urlsafe(32)
    db.add(AdminSession(
        token_hash=_token_hash(token),
        user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(hours=SESSION_HOURS),
    ))
    db.commit()
    return token


def get_session_user(db: Session, token: str) -> Optional[AdminUser]:
    """Token geçerliyse (var + süresi dolmamış + kullanıcı aktif) kullanıcıyı döner."""
    if not token:
        return None
    sess = (
        db.query(AdminSession)
        .filter(
            AdminSession.token_hash == _token_hash(token),
            AdminSession.expires_at > datetime.utcnow(),
        )
        .first()
    )
    if sess is None or sess.user is None or sess.user.is_active != 1:
        return None
    return sess.user


def delete_session(db: Session, token: str) -> None:
    if token:
        db.query(AdminSession).filter(AdminSession.token_hash == _token_hash(token)).delete()
        db.commit()


# ── FastAPI router ────────────────────────────────────────────────────────────

def get_db():
    # main.get_db ile aynı; buradan import etmek döngüsel bağımlılık yaratırdı.
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=200)


def _user_dict(user: AdminUser) -> dict:
    return {"id": user.id, "email": user.email, "full_name": user.full_name}


# Sync def: bcrypt/DB bloklayıcıdır — threadpool'da çalışır (bkz. main.py notu).
@router.post("/login")
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    user = db.query(AdminUser).filter(AdminUser.email == email).first()

    if user is None:
        verify_password(body.password, _DUMMY_HASH)  # timing eşitleme
        raise HTTPException(status_code=401, detail="E-posta ya da parola hatalı.")
    if not verify_password(body.password, user.password_hash) or user.is_active != 1:
        # Pasif kullanıcıya da aynı mesaj: hesap varlığı bilgisi sızdırılmaz.
        raise HTTPException(status_code=401, detail="E-posta ya da parola hatalı.")

    token = create_session(db, user)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/api",
    )
    return {"ok": True, "user": _user_dict(user)}


@router.post("/logout")
def logout(response: Response, session_token: str = Cookie(default="", alias=COOKIE_NAME), db: Session = Depends(get_db)):
    delete_session(db, session_token)
    response.delete_cookie(COOKIE_NAME, path="/api")
    return {"ok": True}


@router.get("/me")
def me(session_token: str = Cookie(default="", alias=COOKIE_NAME), db: Session = Depends(get_db)):
    user = get_session_user(db, session_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Oturum gerekli.")
    return {"user": _user_dict(user)}


# ── Koruma middleware'i ──────────────────────────────────────────────────────
# nginx'teki geçici kalkanla AYNI yol deseni: yönetim uçları oturum ister,
# widget'ın kullandığı uçlar public kalır. Router refactor'ü yapıldığında bu
# middleware yerine APIRouter(dependencies=[...]) tercih edilebilir.

_PROTECTED_RE = re.compile(r"^/api/(qna|conversations|config|stats|academic-calendar)")
_PUBLIC_RE = re.compile(r"^/api/conversations/\d+/talep$")  # widget talep yanıtı


def _lookup_user(token: str) -> Optional[AdminUser]:
    db = SessionLocal()
    try:
        return get_session_user(db, token)
    finally:
        db.close()


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if AUTH_ENFORCED and _PROTECTED_RE.match(path) and not _PUBLIC_RE.match(path):
            token = request.cookies.get(COOKIE_NAME, "")
            # DB sorgusu bloklayıcı — event loop'u kilitlememek için threadpool'da.
            user = await run_in_threadpool(_lookup_user, token) if token else None
            if user is None:
                return JSONResponse({"detail": "Oturum gerekli."}, status_code=401)
        return await call_next(request)
