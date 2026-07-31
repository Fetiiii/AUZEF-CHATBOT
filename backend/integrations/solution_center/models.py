"""Çözüm Merkezi domain modelleri (Pydantic v2).

Bunlar uygulamanın İÇ modelleridir. API'nin ham request/response gövdeleri
schemas.py içindedir. Tüm modeller strongly typed (SPEC kuralı).
"""
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SCState(str, Enum):
    """Talep oluşturma akışının durum makinesi (SPEC "State Machine")."""
    IDLE = "IDLE"
    SC_WAIT_TC = "SC_WAIT_TC"
    SC_WAIT_OTP = "SC_WAIT_OTP"
    SC_WAIT_STUDENT_SELECTION = "SC_WAIT_STUDENT_SELECTION"
    SC_WAIT_CATEGORY_SELECTION = "SC_WAIT_CATEGORY_SELECTION"
    SC_WAIT_DESCRIPTION = "SC_WAIT_DESCRIPTION"
    SC_CREATING_TICKET = "SC_CREATING_TICKET"
    SC_FINISHED = "SC_FINISHED"


class Student(BaseModel):
    """Bir TC altındaki tek öğrenci kaydı. Aynı TC'de birden fazla olabilir."""
    model_config = ConfigDict(populate_by_name=True)

    ogrenci_id: int = Field(alias="ogrenciId")
    birim_adi: Optional[str] = Field(default=None, alias="birimAdi")
    fakulte_adi: Optional[str] = Field(default=None, alias="fakulteAdi")


class Category(BaseModel):
    """Çözüm Merkezi kategorisi (API flat liste döndürür)."""
    model_config = ConfigDict(populate_by_name=True)

    uuid: str
    short_code: str = Field(alias="shortCode")
    name: str
    is_leaf: bool = Field(alias="isLeaf")
    sort_order: int = Field(default=0, alias="sortOrder")
    parent_uuid: Optional[str] = Field(default=None, alias="parentUuid")


class CategoryNode(Category):
    """Mapper çıktısı: alt kategorileri (children) içeren ağaç düğümü."""
    children: List["CategoryNode"] = Field(default_factory=list)


class PhoneInfo(BaseModel):
    masked_phone_number: str = Field(alias="maskedPhoneNumber")


class TicketResult(BaseModel):
    """Talep oluşturma sonucu (kullanıcıya UUID gösterilir)."""
    id: int
    uuid: str


CategoryNode.model_rebuild()
