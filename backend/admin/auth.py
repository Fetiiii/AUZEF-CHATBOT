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
from datetime import timedelta
from typing import Optional

import bcrypt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from admin import login_lockout
from core.database import SessionLocal, AdminUser, AdminSession, utcnow
from core.deps import get_db  # tekil DB oturumu dependency'si (deps kanonik kaynak)

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

# ── Rol hiyerarşisi ──────────────────────────────────────────────────────────
# Sayı büyüdükçe yetki artar; bir uca erişim "en az şu seviye" ile tanımlanır.
ROLE_LEVEL = {"editor": 1, "admin": 2, "super_admin": 3}
VALID_ROLES = set(ROLE_LEVEL)


def role_level(role: Optional[str]) -> int:
    """Bilinmeyen/boş rol 0 döner → hiçbir korumalı uca erişemez (fail-closed)."""
    return ROLE_LEVEL.get((role or "").strip(), 0)


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
    db.query(AdminSession).filter(AdminSession.expires_at < utcnow()).delete()
    token = secrets.token_urlsafe(32)
    db.add(AdminSession(
        token_hash=_token_hash(token),
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=SESSION_HOURS),
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
            AdminSession.expires_at > utcnow(),
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

def current_user(
    session_token: str = Cookie(default="", alias=COOKIE_NAME),
    db: Session = Depends(get_db),
) -> Optional[AdminUser]:
    """İstek sahibi kullanıcı (oturum geçerliyse), yoksa None.

    Middleware erişimi zaten doğrular; bu dependency handler'a KULLANICIYI
    verir (denetim izi/self-lockout için). Enforcement kapalıyken (yerel
    geliştirme) None dönebilir."""
    return get_session_user(db, session_token)


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str = Field(max_length=255)
    password: str = Field(max_length=200)


def _user_dict(user: AdminUser) -> dict:
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role}


_LOCKOUT_MESSAGE = "Çok fazla başarısız deneme. Lütfen birkaç dakika sonra tekrar deneyin."


# Sync def: bcrypt/DB bloklayıcıdır — threadpool'da çalışır (bkz. main.py notu).
@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    ip = request.client.host if request.client else None

    # Ön kontrol (ANALIZ.md P0-3): kilitliyken bcrypt'e hiç girilmez — bcrypt
    # bilinçli olarak pahalıdır, kilitli bir denemeye o maliyeti ödetmeye gerek yok.
    if login_lockout.is_locked(db, email, ip):
        raise HTTPException(status_code=429, detail=_LOCKOUT_MESSAGE)

    user = db.query(AdminUser).filter(AdminUser.email == email).first()

    if user is None:
        verify_password(body.password, _DUMMY_HASH)  # timing eşitleme
        if login_lockout.register_failure(db, email, ip):
            raise HTTPException(status_code=429, detail=_LOCKOUT_MESSAGE)
        raise HTTPException(status_code=401, detail="E-posta ya da parola hatalı.")
    if not verify_password(body.password, user.password_hash) or user.is_active != 1:
        # Pasif kullanıcıya da aynı mesaj: hesap varlığı bilgisi sızdırılmaz.
        if login_lockout.register_failure(db, email, ip):
            raise HTTPException(status_code=429, detail=_LOCKOUT_MESSAGE)
        raise HTTPException(status_code=401, detail="E-posta ya da parola hatalı.")

    # Başarılı giriş → iki kapsamın sayaçları da temizlenir (OTP başarı
    # sıfırlamasıyla aynı ilke — P0-2).
    login_lockout.reset(db, email, ip)
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
# Yönetim uçları oturum + yeterli ROL ister; widget'ın kullandığı uçlar public
# kalır. Kurallar sırayla denenir, İLK eşleşen kural geçerlidir (en özel kural
# en üstte). Router refactor'ü yapıldığında bu middleware yerine
# APIRouter(dependencies=[...]) tercih edilebilir.

_PUBLIC_RE = re.compile(r"^/api/conversations/\d+/talep$")  # widget talep yanıtı

# (desen, gereken en düşük rol) — yetki matrisi:
#   editor      → içerik (QnA, akademik takvim, import/export)
#   admin       → + konuşmalar, istatistikler
#   super_admin → + ayarlar (kullanıcılar, LLM, API anahtarı)
_ROLE_RULES = [
    (re.compile(r"^/api/settings"), "super_admin"),
    (re.compile(r"^/api/(conversations|stats)"), "admin"),
    (re.compile(r"^/api/(qna|academic-calendar)"), "editor"),
]


def required_role_for(path: str) -> Optional[str]:
    """Yol için gereken en düşük rolü döner; korumasızsa None."""
    if _PUBLIC_RE.match(path):
        return None
    for pattern, role in _ROLE_RULES:
        if pattern.match(path):
            return role
    return None


def _lookup_user(token: str) -> Optional[AdminUser]:
    db = SessionLocal()
    try:
        return get_session_user(db, token)
    finally:
        db.close()


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        required = required_role_for(request.url.path) if AUTH_ENFORCED else None
        if required is not None:
            token = request.cookies.get(COOKIE_NAME, "")
            # DB sorgusu bloklayıcı — event loop'u kilitlememek için threadpool'da.
            user = await run_in_threadpool(_lookup_user, token) if token else None
            if user is None:
                return JSONResponse({"detail": "Oturum gerekli."}, status_code=401)
            if role_level(user.role) < role_level(required):
                return JSONResponse({"detail": "Bu işlem için yetkiniz yok."}, status_code=403)
        return await call_next(request)
