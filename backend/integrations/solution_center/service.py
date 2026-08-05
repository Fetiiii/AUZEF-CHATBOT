"""Çözüm Merkezi iş mantığı (SPEC "service.py").

Chatbot YALNIZCA bu katmanı kullanır. HTTP çağrıları client.py'de; burada
akışın kuralları, state machine geçişleri ve oturum kalıcılığı yönetilir.

State machine (SPEC):
    SC_WAIT_TC → SC_WAIT_OTP → SC_WAIT_STUDENT_SELECTION →
    SC_WAIT_CATEGORY_SELECTION → SC_WAIT_DESCRIPTION →
    SC_CREATING_TICKET → SC_FINISHED
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import SolutionCenterSession as SCSessionRow, utcnow

from . import mapper
from .base_client import SolutionCenterConfig
from .category_cache import CategoryCache
from .client import SolutionCenterClient
from .exceptions import (
    CategoryNotFoundException,
    InvalidOtpException,
    InvalidStateException,
    OtpAttemptsExceededException,
    SolutionCenterException,
    StudentNotFoundException,
    TicketCreateException,
    VerificationExpiredException,
)
from .models import Category, CategoryNode, SCState, Student, TicketResult
from .rate_limit import SmsRateLimiter
from .schemas import CreateTicketRequest

logger = logging.getLogger("auzef.sc")


def _ok(payload: Any) -> bool:
    """CM yanıtı başarılı mı? isSuccess varsa ona bakar, yoksa data varlığına."""
    if isinstance(payload, dict):
        if "isSuccess" in payload:
            return bool(payload["isSuccess"])
        return "data" in payload
    return isinstance(payload, list)


def _data(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("data")
    return payload


class SolutionCenterService:
    """Talep akışının iş mantığı. Her istek için ucuzca kurulur (DI);
    paylaşılan client + cache'i referanslar."""

    def __init__(
        self,
        db: Session,
        client: SolutionCenterClient,
        cache: CategoryCache,
        config: SolutionCenterConfig,
        limiter: SmsRateLimiter,
    ) -> None:
        self._db = db
        self._client = client
        self._cache = cache
        self._config = config
        self._limiter = limiter

    # ── Oturum satırı yardımcıları ───────────────────────────────────────────

    def _load(self, conversation_id: int) -> Optional[SCSessionRow]:
        return (
            self._db.query(SCSessionRow)
            .filter(SCSessionRow.conversation_id == conversation_id)
            .first()
        )

    def _load_or_400(self, conversation_id: int) -> SCSessionRow:
        row = self._load(conversation_id)
        if row is None:
            raise InvalidStateException("SC oturumu yok")
        return row

    @staticmethod
    def _ensure_not_expired(row: SCSessionRow) -> None:
        if row.expires_at is not None and utcnow() > row.expires_at:
            raise VerificationExpiredException("verificationToken süresi doldu")

    def _students(self, row: SCSessionRow) -> List[Student]:
        if not row.students_json:
            return []
        try:
            return [Student.model_validate(s) for s in json.loads(row.students_json)]
        except (ValueError, TypeError):
            return []

    # ── 1) TC → telefon göster + SMS gönder ──────────────────────────────────

    async def start_verification(
        self, conversation_id: int, kimlik_no: str, ip: Optional[str] = None
    ) -> str:
        """TC'yi doğrular (telefon getir) + SMS gönderir. Maskeli telefonu döner.

        verificationToken oturum satırına yazılır, ÇAĞIRANA DÖNMEZ.

        İlk iş hız sınırı tüketilir (ANALIZ.md P0-1): CM API'sine çağrı YAPILMADAN
        önce, çünkü (a) kuruma gereksiz SMS maliyeti çıkmasın, (b) başarısız TC
        denemeleri de sayılsın — numaralandırma saldırısı zaten başarısız
        aramalarla yapılıyor."""
        self._limiter.check_and_consume(
            self._db, conversation_id=conversation_id, kimlik_no=kimlik_no, ip=ip)

        phone_payload = await self._client.get_phone(kimlik_no)
        phone_data = _data(phone_payload)
        masked = phone_data.get("maskedPhoneNumber") if isinstance(phone_data, dict) else None
        if not _ok(phone_payload) or not masked:
            # Geçersiz TC (kayıt yok) — API mesajını sızdırma, anlamlı hataya çevir.
            raise StudentNotFoundException(
                "TC için telefon bulunamadı",
                user_message="Bu kimlik numarasına ait kayıt bulunamadı. Lütfen kontrol edin.",
            )

        sms_payload = await self._client.send_sms(kimlik_no)
        sms_data = _data(sms_payload)
        token = sms_data.get("verificationToken") if isinstance(sms_data, dict) else None
        if not _ok(sms_payload) or not token:
            raise SolutionCenterException(
                "SMS gönderilemedi",
                user_message="Doğrulama kodu gönderilemedi. Lütfen birazdan tekrar deneyin.",
            )

        row = self._load(conversation_id)
        if row is None:
            row = SCSessionRow(conversation_id=conversation_id)
            self._db.add(row)
        row.state = SCState.SC_WAIT_OTP.value
        row.verification_token = token
        row.masked_phone = masked
        row.students_json = None
        row.selected_student_id = None
        row.category_short_code = None
        # Yeni kod = yeni deneme hakkı. Aksi hâlde SMS'i geç gelen kullanıcı
        # yeni kod istediğinde tükenmiş bir sayaçla karşılaşırdı.
        #
        # DİKKAT — P0-1 BAĞIMLILIĞI: bu sıfırlama, SMS hız sınırı OLMASAYDI
        # OTP sayacını sınırsız sıfırlamanın yolu olur ve P0-2'yi tümüyle
        # delerdi. Yukarıdaki check_and_consume SMS'i bütçelediği için gerçek
        # koruma "SMS limiti × deneme eşiği" çarpımıdır (bkz. constants.py
        # DEFAULT_OTP_MAX_ATTEMPTS notu). Biri kaldırılırsa diğeri zayıflar.
        row.otp_attempts = 0
        row.expires_at = utcnow() + timedelta(minutes=self._config.verification_ttl_min)
        self._db.commit()
        logger.info("SC start_verification tamam conversation_id=%s state=%s",
                    conversation_id, row.state)
        return masked

    # ── 2) OTP → öğrenci listesi ─────────────────────────────────────────────

    def _consume_otp_attempt(self, conversation_id: int, token: str) -> Optional[int]:
        """Yanlış deneme sayacını ATOMİK artırır ve yeni değeri döner.

        uvicorn 2 worker ile çalıştığı için oku-sonra-yaz yarışa açıktır: iki
        eşzamanlı istek `attempts=4` okuyup ikisi de geçebilirdi. Tek UPDATE ...
        RETURNING bunu kapatır (rate_limit.py'deki kalıbın aynısı).

        TOKEN'A KOŞULLU — sayaç verificationToken'ın ÖMRÜNE bağlıdır: CM çağrısı
        beklenirken (await) kullanıcı yeni SMS istemiş, token yenilenmiş ve sayaç
        sıfırlanmış olabilir. Yalnızca conversation_id ile artırmak, ESKİ oturuma
        ait bu denemeyi YENİ token'ın sayacına yazardı; meşru kullanıcı yeni kod
        aldıktan sonra hakkını eksik bulurdu. Token değişmişse None döner ve
        deneme sayılmaz."""
        new_value = self._db.execute(
            text(
                "UPDATE solution_center_sessions SET otp_attempts = otp_attempts + 1 "
                "WHERE conversation_id = :cid AND verification_token = :token "
                "RETURNING otp_attempts"
            ),
            {"cid": conversation_id, "token": token},
        ).scalar_one_or_none()
        self._db.commit()
        return new_value

    def _lock_verification(self, conversation_id: int, token: str) -> None:
        """Doğrulama oturumunu geri dönülemez biçimde iptal eder.

        Token silinir ki elde kalan bir kopyayla devam edilemesin; state
        SC_WAIT_TC'ye çekilir ki kullanıcı akışı baştan başlatsın (yeni TC →
        yeni SMS, ki o da P0-1 bütçesinden düşer).

        TOKEN'A KOŞULLU ve ORM satırı üzerinden DEĞİL: satır await'ten önce
        okunduğu için bayat olabilir. Koşulsuz yazmak, araya giren yeni SMS'in
        TAZE token'ını silip meşru kullanıcının oturumunu iptal ederdi — yani
        kilit, korumayı hedeflediği kullanıcıyı cezalandırırdı."""
        self._db.execute(
            text(
                "UPDATE solution_center_sessions SET "
                "verification_token = NULL, state = :state, students_json = NULL, "
                "selected_student_id = NULL, category_short_code = NULL "
                "WHERE conversation_id = :cid AND verification_token = :token"
            ),
            {
                "cid": conversation_id,
                "state": SCState.SC_WAIT_TC.value,
                "token": token,
            },
        )
        self._db.commit()

    async def verify_otp(self, conversation_id: int, code: str) -> dict:
        """OTP doğrular, öğrenci(ler)i getirir. Tek öğrenci ise otomatik seçilir.

        Yanlış denemeler sayılır (ANALIZ.md P0-2); eşiğe ulaşınca oturum
        kilitlenir. Sayaç YALNIZCA kod reddedildiğinde artar — CM timeout'u ya
        da 5xx bir tahmin değildir, meşru kullanıcının hakkı ağ hatası yüzünden
        yanmamalı."""
        row = self._load_or_400(conversation_id)
        if row.state not in (SCState.SC_WAIT_OTP.value, SCState.SC_WAIT_STUDENT_SELECTION.value):
            raise InvalidStateException(f"OTP beklenmiyor (state={row.state})")
        self._ensure_not_expired(row)
        # Token yerel değişkende SABİTLENİR: aşağıdaki await sırasında oturum
        # satırı başka bir istek tarafından (yeni SMS) değiştirilebilir. Bundan
        # sonraki tüm yazmalar BU token'a koşulludur, böylece geç gelen bu istek
        # araya giren yeni oturuma dokunamaz.
        token = row.verification_token
        if not token:
            raise VerificationExpiredException("verificationToken yok")

        # Ön kontrol: kilitli oturumda CM'ye HİÇ gitme (kurumun API'sine
        # gereksiz yük). Savunma amaçlı — normalde token zaten NULL'dur.
        if row.otp_attempts >= self._config.otp_max_attempts:
            self._lock_verification(conversation_id, token)
            raise OtpAttemptsExceededException("OTP deneme hakkı zaten tükenmiş")

        payload = await self._client.verify_code(code, token)
        if not _ok(payload):
            attempts = self._consume_otp_attempt(conversation_id, token)
            if attempts is None:
                # Token bu arada yenilendi (kullanıcı yeni SMS istedi). Bu deneme
                # ARTIK GEÇERSİZ bir oturuma ait; yeni oturumun sayacı kirletilmez
                # ve kilit tetiklenmez.
                raise InvalidOtpException("OTP reddedildi (oturum bu arada yenilendi)")
            if attempts >= self._config.otp_max_attempts:
                self._lock_verification(conversation_id, token)
                # Log'a yalnızca sayı yazılır — kod, token, TC ASLA.
                logger.warning(
                    "SC OTP deneme hakki tukendi conversation_id=%s deneme=%d",
                    conversation_id, attempts,
                )
                raise OtpAttemptsExceededException("OTP deneme hakkı tükendi")
            raise InvalidOtpException("OTP reddedildi")

        raw = _data(payload)
        raw_list = raw if isinstance(raw, list) else ([raw] if raw else [])
        students = [Student.model_validate(s) for s in raw_list]
        if not students:
            raise StudentNotFoundException("Öğrenci kaydı yok")

        # Doğru kod geldi → sayaç sıfırlanır. Akış zaten ilerliyor ama state
        # SC_WAIT_STUDENT_SELECTION'a düşerse bu uca tekrar gelinebiliyor
        # (bkz. yukarıdaki state kontrolü); bayat bir sayaç taşınmamalı.
        row.otp_attempts = 0
        row.students_json = json.dumps(
            [s.model_dump(by_alias=True) for s in students], ensure_ascii=False
        )
        if len(students) == 1:
            row.selected_student_id = students[0].ogrenci_id
            row.state = SCState.SC_WAIT_CATEGORY_SELECTION.value
            auto = True
        else:
            row.selected_student_id = None
            row.state = SCState.SC_WAIT_STUDENT_SELECTION.value
            auto = False
        self._db.commit()
        logger.info("SC verify_otp tamam conversation_id=%s ogrenci_sayisi=%d auto=%s",
                    conversation_id, len(students), auto)
        return {
            "state": row.state,
            "auto_selected": auto,
            "students": [s.model_dump(by_alias=True) for s in students],
        }

    # ── 3) Öğrenci seçimi ────────────────────────────────────────────────────

    def select_student(self, conversation_id: int, ogrenci_id: int) -> dict:
        row = self._load_or_400(conversation_id)
        if row.state not in (
            SCState.SC_WAIT_STUDENT_SELECTION.value,
            SCState.SC_WAIT_CATEGORY_SELECTION.value,
        ):
            raise InvalidStateException(f"Öğrenci seçimi beklenmiyor (state={row.state})")
        self._ensure_not_expired(row)
        valid_ids = {s.ogrenci_id for s in self._students(row)}
        if ogrenci_id not in valid_ids:
            raise StudentNotFoundException("Seçilen öğrenci listede yok")
        row.selected_student_id = ogrenci_id
        row.state = SCState.SC_WAIT_CATEGORY_SELECTION.value
        self._db.commit()
        return {"state": row.state, "selected_student_id": ogrenci_id}

    # ── 4) Kategoriler (cache'li) ────────────────────────────────────────────

    async def _fetch_categories(self) -> List[Category]:
        raw = await self._client.get_categories()
        return mapper.parse_categories(raw)

    async def get_categories_flat(self) -> List[Category]:
        return await self._cache.get_or_fetch(self._fetch_categories)

    async def get_category_tree(self) -> List[CategoryNode]:
        flat = await self.get_categories_flat()
        return mapper.build_tree(flat)

    # ── 5) Kategori seçimi ───────────────────────────────────────────────────

    async def select_category(self, conversation_id: int, short_code: str) -> dict:
        row = self._load_or_400(conversation_id)
        if row.state not in (
            SCState.SC_WAIT_CATEGORY_SELECTION.value,
            SCState.SC_WAIT_DESCRIPTION.value,
        ):
            raise InvalidStateException(f"Kategori seçimi beklenmiyor (state={row.state})")
        self._ensure_not_expired(row)
        flat = await self.get_categories_flat()
        if not mapper.is_selectable(flat, short_code):
            # Yok ya da yaprak değil → seçilemez.
            raise CategoryNotFoundException(f"Kategori seçilemez: {short_code}")
        row.category_short_code = short_code
        row.state = SCState.SC_WAIT_DESCRIPTION.value
        self._db.commit()
        return {"state": row.state, "category_short_code": short_code}

    # ── 6) Talep oluştur ─────────────────────────────────────────────────────

    async def create_ticket(self, conversation_id: int, description: str) -> TicketResult:
        row = self._load_or_400(conversation_id)
        if row.state not in (
            SCState.SC_WAIT_DESCRIPTION.value,
            SCState.SC_CREATING_TICKET.value,
        ):
            raise InvalidStateException(f"Talep oluşturma beklenmiyor (state={row.state})")
        self._ensure_not_expired(row)

        description = (description or "").strip()
        if not description:
            raise SolutionCenterException(
                "Boş açıklama",
                user_message="Lütfen talebinizi kısaca açıklayın.",
            )
        if not row.verification_token:
            raise VerificationExpiredException("verificationToken yok")
        if not row.selected_student_id:
            raise InvalidStateException("Öğrenci seçilmemiş")
        if not row.category_short_code:
            raise InvalidStateException("Kategori seçilmemiş")

        row.state = SCState.SC_CREATING_TICKET.value
        self._db.commit()

        request = CreateTicketRequest.build(
            verification_token=row.verification_token,
            channel_short_code=self._config.channel_short_code,
            category_short_code=row.category_short_code,
            ogrenci_id=row.selected_student_id,
            description=description,
        )
        payload = await self._client.create_ticket(request)
        if not _ok(payload):
            # Başarısızsa açıklama state'ine geri dön ki kullanıcı yeniden denesin.
            row.state = SCState.SC_WAIT_DESCRIPTION.value
            self._db.commit()
            raise TicketCreateException("CM talep oluşturmadı")

        data = _data(payload)
        try:
            result = TicketResult.model_validate(data)
        except Exception as exc:
            row.state = SCState.SC_WAIT_DESCRIPTION.value
            self._db.commit()
            raise TicketCreateException("CM talep yanıtı çözümlenemedi") from exc

        row.state = SCState.SC_FINISHED.value
        row.verification_token = None  # kullanıldı — sil (savunma)
        self._db.commit()
        logger.info("SC talep olusturuldu conversation_id=%s ticket_uuid=%s",
                    conversation_id, result.uuid)
        return result
