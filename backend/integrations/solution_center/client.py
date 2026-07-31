"""Çözüm Merkezi HTTP istemcisi — SADECE HTTP çağrıları, iş mantığı YOK.

Her metod ilgili endpoint'e POST atar ve zarfın 'data' kısmını döndürür.
isSuccess=false yorumlama, exception seçimi vb. service.py'nin işidir.
"""
from __future__ import annotations

from typing import Any, List

from .base_client import BaseClient
from .constants import (
    PATH_CREATE_TICKET,
    PATH_GET_CATEGORIES,
    PATH_GET_PHONE,
    PATH_SEND_SMS,
    PATH_VERIFY_CODE,
)
from .schemas import CreateTicketRequest, KimlikRequest, VerifyCodeRequest


class SolutionCenterClient(BaseClient):

    async def get_phone(self, kimlik_no: str) -> Any:
        """TC ile maskeli telefon bilgisini getirir."""
        body = KimlikRequest(kimlik_no=kimlik_no).model_dump(by_alias=True)
        return await self.post(PATH_GET_PHONE, body)

    async def send_sms(self, kimlik_no: str) -> Any:
        """TC'ye doğrulama kodu (SMS) gönderir; verificationToken döner."""
        body = KimlikRequest(kimlik_no=kimlik_no).model_dump(by_alias=True)
        return await self.post(PATH_SEND_SMS, body)

    async def verify_code(self, verification_code: str, verification_token: str) -> Any:
        """OTP + verificationToken ile öğrenci listesini getirir."""
        body = VerifyCodeRequest(
            verification_code=verification_code,
            verification_token=verification_token,
        ).model_dump(by_alias=True)
        return await self.post(PATH_VERIFY_CODE, body)

    async def get_categories(self) -> List[dict]:
        """Tüm kategorileri flat liste olarak getirir.

        Uç düz liste ya da {..., data:[...]} zarfı dönebilir — ikisini de kabul et."""
        payload = await self.post(PATH_GET_CATEGORIES, {})
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        return []

    async def create_ticket(self, request: CreateTicketRequest) -> Any:
        """Talep oluşturur; {id, uuid} döner."""
        return await self.post(PATH_CREATE_TICKET, request.to_wire())
