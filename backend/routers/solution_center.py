"""Çözüm Merkezi talep akışı uçları (public — widget kullanır).

Tüm uçlar async (SPEC: HTTP istekleri async). İş mantığı yok; her uç ilgili
SolutionCenterService metodunu çağırır. Sahiplik conversations.client_token
ile doğrulanır (chat.py ile aynı model). verificationToken/OTP/TC ASLA yanıtta
dönmez ve loglanmaz.

Not: /api/solution-center/* yolları AdminAuthMiddleware'in korunan desenlerine
(settings|conversations|stats|qna|academic-calendar) uymadığından PUBLIC'tir.
"""
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import Conversation
from core.deps import get_db, MAX_MESSAGE_LEN
from integrations.solution_center import (
    SolutionCenterConfig,
    SolutionCenterService,
    get_config,
    get_sc_service,
)

logger = logging.getLogger("auzef.sc")
router = APIRouter(prefix="/api/solution-center", tags=["solution-center"])


# ── İstek gövdeleri ──────────────────────────────────────────────────────────

class _Owned(BaseModel):
    """Sahiplik zorunlu tüm istekler için ortak alanlar."""
    conversation_id: int
    conversation_token: str


class SendSmsRequest(_Owned):
    kimlik_no: str = Field(min_length=11, max_length=11)


class VerifyOtpRequest(_Owned):
    code: str = Field(min_length=4, max_length=8)


class SelectStudentRequest(_Owned):
    ogrenci_id: int


class SelectCategoryRequest(_Owned):
    category_short_code: str = Field(min_length=1, max_length=120)


class CreateTicketRequest(_Owned):
    description: str = Field(min_length=1, max_length=MAX_MESSAGE_LEN)


class CategoriesRequest(_Owned):
    pass


# ── Ortak doğrulamalar ───────────────────────────────────────────────────────

def require_enabled(config: SolutionCenterConfig = Depends(get_config)) -> SolutionCenterConfig:
    """Config eksikse (base_url/service_token yok) akış nazikçe kapalıdır."""
    if not config.enabled:
        raise HTTPException(
            status_code=503,
            detail="Talep sistemi şu anda kullanılamıyor. Lütfen daha sonra tekrar deneyin.",
        )
    return config


def _verify_owner(db: Session, conversation_id: int, token: Optional[str]) -> Conversation:
    """Konuşmayı getirir ve sahiplik token'ını doğrular (chat.py ile aynı model)."""
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if (
        conv is None
        or not conv.client_token
        or not token
        or not hmac.compare_digest(conv.client_token, token)
    ):
        raise HTTPException(status_code=403, detail="Bu konuşma için yetkiniz yok.")
    return conv


# ── Uçlar ────────────────────────────────────────────────────────────────────

@router.post("/send-sms")
async def send_sms(
    body: SendSmsRequest,
    db: Session = Depends(get_db),
    service: SolutionCenterService = Depends(get_sc_service),
    _config: SolutionCenterConfig = Depends(require_enabled),
):
    """TC doğrula → maskeli telefonu göster + SMS gönder. Token yanıtta DÖNMEZ."""
    _verify_owner(db, body.conversation_id, body.conversation_token)
    masked = await service.start_verification(body.conversation_id, body.kimlik_no)
    return {"masked_phone": masked, "state": "SC_WAIT_OTP"}


@router.post("/verify-otp")
async def verify_otp(
    body: VerifyOtpRequest,
    db: Session = Depends(get_db),
    service: SolutionCenterService = Depends(get_sc_service),
    _config: SolutionCenterConfig = Depends(require_enabled),
):
    """OTP doğrula → öğrenci(ler). Tek öğrenci otomatik seçilir."""
    _verify_owner(db, body.conversation_id, body.conversation_token)
    return await service.verify_otp(body.conversation_id, body.code)


@router.post("/select-student")
async def select_student(
    body: SelectStudentRequest,
    db: Session = Depends(get_db),
    service: SolutionCenterService = Depends(get_sc_service),
    _config: SolutionCenterConfig = Depends(require_enabled),
):
    _verify_owner(db, body.conversation_id, body.conversation_token)
    return service.select_student(body.conversation_id, body.ogrenci_id)


@router.post("/categories")
async def categories(
    body: CategoriesRequest,
    db: Session = Depends(get_db),
    service: SolutionCenterService = Depends(get_sc_service),
    _config: SolutionCenterConfig = Depends(require_enabled),
):
    """Kategori ağacını döner (cache'li). Yalnızca isLeaf==true seçilebilir."""
    _verify_owner(db, body.conversation_id, body.conversation_token)
    tree = await service.get_category_tree()
    return {"categories": [n.model_dump(by_alias=True) for n in tree]}


@router.post("/select-category")
async def select_category(
    body: SelectCategoryRequest,
    db: Session = Depends(get_db),
    service: SolutionCenterService = Depends(get_sc_service),
    _config: SolutionCenterConfig = Depends(require_enabled),
):
    _verify_owner(db, body.conversation_id, body.conversation_token)
    return await service.select_category(body.conversation_id, body.category_short_code)


@router.post("/create-ticket")
async def create_ticket(
    body: CreateTicketRequest,
    db: Session = Depends(get_db),
    service: SolutionCenterService = Depends(get_sc_service),
    _config: SolutionCenterConfig = Depends(require_enabled),
):
    """Talebi oluşturur; kullanıcıya ticket UUID döner."""
    _verify_owner(db, body.conversation_id, body.conversation_token)
    result = await service.create_ticket(body.conversation_id, body.description)
    return {"state": "SC_FINISHED", "ticket": {"id": result.id, "uuid": result.uuid}}
