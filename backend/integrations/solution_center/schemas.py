"""Çözüm Merkezi API wire-format modelleri (request/response gövdeleri).

Domain modelleri (models.py) uygulama içi; bunlar API ile KABLO üzerinde
alışverişi yapılan şekiller. Ayrım SPEC gereği (models.py vs schemas.py).
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Giden (request) gövdeleri ────────────────────────────────────────────────

class KimlikRequest(BaseModel):
    """kimlik-ile-telefon-al ve telefona-dogrulama-kodu-gonder ortak gövdesi."""
    kimlik_no: str = Field(serialization_alias="kimlikNo")


class VerifyCodeRequest(BaseModel):
    verification_code: str = Field(serialization_alias="verificationCode")
    verification_token: str = Field(serialization_alias="verificationToken")


class _ShortCodeRef(BaseModel):
    short_code: str = Field(serialization_alias="shortCode")


class CreateTicketRequest(BaseModel):
    """talep-olustur gövdesi (SPEC "Talep Oluştur")."""
    verification_token: str = Field(serialization_alias="verificationToken")
    channel: _ShortCodeRef
    category: _ShortCodeRef
    ogrenci_id: int = Field(serialization_alias="ogrenciId")
    description: str

    @classmethod
    def build(cls, *, verification_token: str, channel_short_code: str,
              category_short_code: str, ogrenci_id: int, description: str) -> "CreateTicketRequest":
        return cls(
            verification_token=verification_token,
            channel=_ShortCodeRef(short_code=channel_short_code),
            category=_ShortCodeRef(short_code=category_short_code),
            ogrenci_id=ogrenci_id,
            description=description,
        )

    def to_wire(self) -> dict:
        """API'nin beklediği alias'lı JSON (nested dahil)."""
        return self.model_dump(by_alias=True)


# ── Gelen (response) zarfı ───────────────────────────────────────────────────

class ApiEnvelope(BaseModel):
    """CM standart yanıt zarfı: {isSuccess, code, data, message}.

    Bazı uçlar (kategoriler) düz liste de dönebildiğinden client tarafında
    zarf yoksa ham gövdeyle çalışılır (bkz. base_client._extract_data)."""
    is_success: Optional[bool] = Field(default=None, alias="isSuccess")
    code: Optional[int] = None
    data: Any = None
    message: Optional[str] = None
