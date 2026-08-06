"""SMS gönderim hız sınırı (ANALIZ.md P0-1).

Sorun: ``/api/solution-center/send-sms`` ucunun tek gereksinimi geçerli bir
``conversation_token``, o da tek bir ``/widget-chat`` çağrısıyla bedava alınıyor.
Bu, iki AYRI saldırıya kapı açıyordu:

1. **SMS bombardımanı** — aynı TC'ye tekrar tekrar SMS gönderilmesi (kuruma
   maliyet, vatandaşa taciz). ``tc`` kapsamı bunu bütçeler.
2. **TC numaralandırma** — her seferinde FARKLI bir TC denenerek "bu kişi AUZEF
   öğrencisi mi?" sorusunun cevaplanması (kayıtlı TC maskeli telefon döner,
   kayıtsız 404). ``tc`` kapsamı bunu DURDURMAZ, çünkü her TC yalnızca bir kez
   görülür; ``conv`` ve ``ip`` kapsamları bunun içindir.

Bu yüzden üç kapsam da gereklidir; biri diğerinin yerini tutmaz.

Tasarım notları
---------------
- **Durum DB'de.** uvicorn 2 worker ile çalışıyor (entrypoint.sh); süreç-içi bir
  sayaç her worker'da ayrı yaşar ve gerçek limiti ikiye katlardı.
- **Artırma tek atomik ifade.** İki worker aynı satıra aynı anda yazabilir;
  SELECT-sonra-UPDATE yarışa açıktır. ``INSERT ... ON CONFLICT DO UPDATE ...
  RETURNING`` tek turda artırır ve artırılmış değeri döner.
- **Sabit pencere (fixed window).** Pencere dolduğunda sayaç 1'e döner. Kayan
  pencereye göre kaba ama tek satırla ifade edilebiliyor ve bu tehdit için
  fazlasıyla yeterli.
- **TC ASLA saklanmaz.** Anahtar HMAC-SHA256'dır; sistemin "TC hiçbir yerde
  saklanmaz" ilkesi (bkz. core/database.py SolutionCenterSession) korunur.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import SessionLocal, utcnow

from .base_client import SolutionCenterConfig
from .constants import SMS_COUNTER_RETENTION_HOURS
from .exceptions import RateLimitExceededException

logger = logging.getLogger("auzef.sc")

SCOPE_CONVERSATION = "conv"
SCOPE_TC = "tc"
SCOPE_IP = "ip"

# Artırma + pencere sıfırlama TEK ifadede. window_started_at pencere tabanından
# eskiyse sayaç 1'e ve pencere şimdiye çekilir; değilse count bir artar.
# RETURNING artırılmış değeri döner → çağıran ayrıca SELECT yapmaz (yarış yok).
_CONSUME_SQL = text("""
    INSERT INTO sc_rate_limits (scope, key_hash, count, window_started_at)
    VALUES (:scope, :key_hash, 1, :now)
    ON CONFLICT (scope, key_hash) DO UPDATE SET
        count = CASE
            WHEN sc_rate_limits.window_started_at < :window_floor THEN 1
            ELSE sc_rate_limits.count + 1
        END,
        window_started_at = CASE
            WHEN sc_rate_limits.window_started_at < :window_floor THEN :now
            ELSE sc_rate_limits.window_started_at
        END
    RETURNING count
""")

_CLEANUP_SQL = text("DELETE FROM sc_rate_limits WHERE window_started_at < :cutoff")


class SmsRateLimiter:
    """send-sms için konuşma / TC / IP kapsamlı sayaç.

    Her istek için ucuzca kurulur (DI); durum tamamen veritabanındadır.
    """

    def __init__(self, config: SolutionCenterConfig) -> None:
        self._config = config

    # ── Anahtar üretimi ──────────────────────────────────────────────────────

    def _secret(self) -> bytes:
        """HMAC anahtarı.

        Öncelik: SC_HASH_SECRET → CM_SERVICE_TOKEN'dan türetme.

        Rastgele üretim BİLİNÇLİ OLARAK YOK: her worker farklı secret üretir,
        aynı TC iki farklı hash'e düşer ve TC sayacı fiilen ikiye katlanırdı.
        CM_SERVICE_TOKEN entegrasyon açıkken her zaman mevcuttur (config.enabled),
        stabildir ve gizlidir — türetme için güvenli bir tabandır.
        """
        if self._config.hash_secret:
            return self._config.hash_secret.encode("utf-8")
        token = self._config.service_token or ""
        # Türetme: token'ı doğrudan kullanmak yerine ayrı bir amaç etiketiyle
        # karıştır — aynı sır iki farklı amaçla kullanılmasın.
        return hmac.new(token.encode("utf-8"), b"sc-tc-hash", hashlib.sha256).digest()

    def _key(self, value: str) -> str:
        return hmac.new(self._secret(), value.encode("utf-8"), hashlib.sha256).hexdigest()

    # ── Sayaç ────────────────────────────────────────────────────────────────

    def _consume(self, db: Session, scope: str, raw_key: str, limit: int, window_min: int) -> bool:
        """Kapsamı bir artırır. Limit AŞILDIYSA False döner.

        limit <= 0 ise kapsam devre dışıdır (operatör env ile kapatabilsin).
        """
        if limit <= 0:
            return True
        now = utcnow()
        count = db.execute(_CONSUME_SQL, {
            "scope": scope,
            "key_hash": self._key(raw_key),
            "now": now,
            "window_floor": now - timedelta(minutes=window_min),
        }).scalar_one()
        return count <= limit

    def _cleanup_cutoff(self):
        """Temizlik eşiği: saklama süresi VE yapılandırılmış EN UZUN pencere.

        Eşik sabit 24 saat olsaydı, operatör bir pencereyi retention'dan uzun
        ayarladığında (ör. CM_SMS_TC_WINDOW_MIN=2880 → 48 saat) temizlik HÂLÂ
        GEÇERLİ bir sayacı 24. saatte siler, sayaç sıfırlanır ve limit fail-open
        olurdu. Eşiği en uzun pencereye göre almak bunu yapısal olarak imkânsız
        kılar: pencere içindeki hiçbir satır silinemez.
        """
        cfg = self._config
        en_uzun_pencere_dk = max(
            cfg.sms_conversation_window_min,
            cfg.sms_tc_window_min,
            cfg.sms_ip_window_min,
            0,
        )
        return utcnow() - max(
            timedelta(hours=SMS_COUNTER_RETENTION_HOURS),
            timedelta(minutes=en_uzun_pencere_dk),
        )

    def _cleanup(self) -> None:
        """Eski sayaç satırlarını siler — KENDİ Session'ında.

        NEDEN AYRI SESSION: temizlik "best-effort" diye hatayı except ile yutmak
        YETMEZ. DELETE bir DBAPI hatası verdiğinde (kilit çakışması, deadlock,
        statement timeout) transaction ABORT olur; temizlik çağıranın Session'ını
        paylaşsaydı aynı transaction'daki sayaç artışları da onunla geri alınırdı.
        Ölçüldü: istek 200 dönüyor ama sayaçlar kayboluyor — hız sınırı SESSİZCE
        FAIL-OPEN oluyor. Deadlock olasılığı tam da yük altında, yani korumanın
        en çok gerektiği anda yükselir.

        Ayrı Session bunu MİMARİYLE çözer: çağıranın transaction'ı ve Session'ı
        hiç görülmez, dolayısıyla ne DELETE hatası ne de rollback hatası ona
        ulaşabilir. Önceki tasarımda rollback'in KENDİSİ de patlarsa Session
        bozuk kalıyordu ve akış CM'ye gidip SMS gönderdikten SONRA DB yazarken
        patlıyordu — kullanıcı hata görüyor ama SMS gitmiş oluyordu. O senaryo
        artık kurulamıyor.

        Açık rollback YOK: Session.close() bekleyen/abort olmuş transaction'ı
        zaten atar ve bu Session hemen çöpe gider.

        Kalıp: core/deps.log_query ile aynı (kendi Session, kendi commit,
        finally close).
        """
        db = SessionLocal()
        try:
            db.execute(_CLEANUP_SQL, {"cutoff": self._cleanup_cutoff()})
            db.commit()
        except Exception as exc:
            # exc_info=True: hata yutulduğu için istek normal devam ediyor —
            # geriye tek iz bu satır kalıyor. Traceback olmadan "deadlock mı,
            # statement timeout mu, bağlantı mı koptu" ayırt edilemez. Sızıntı
            # riski yok: _CLEANUP_SQL'in tek parametresi bir zaman damgası
            # (TC/hash/PII içermiyor).
            logger.warning("SC sayaç temizliği başarısız: %s", exc, exc_info=True)
        finally:
            db.close()

    # ── Public ───────────────────────────────────────────────────────────────

    def check_and_consume(
        self,
        db: Session,
        *,
        conversation_id: int,
        kimlik_no: str,
        ip: Optional[str],
    ) -> None:
        """Üç kapsamı da tüketir; herhangi biri dolduysa 429 yükseltir.

        ÖNEMLİ: bu çağrı CM API'sine gitmeden ÖNCE yapılır ve BAŞARISIZ
        denemeleri de sayar — numaralandırma zaten başarısız aramalarla
        yapıldığı için "yalnızca başarılıları say" yaklaşımı korumayı delerdi.
        """
        cfg = self._config
        checks = [
            (SCOPE_CONVERSATION, str(conversation_id),
             cfg.sms_per_conversation, cfg.sms_conversation_window_min),
            (SCOPE_TC, kimlik_no, cfg.sms_per_tc, cfg.sms_tc_window_min),
        ]
        if ip:
            checks.append((SCOPE_IP, ip, cfg.sms_per_ip, cfg.sms_ip_window_min))

        # TÜM kapsamlar tüketilir, ilk aşımda erken çıkılmaz. Engellenen deneme
        # de bir denemedir: kapsamlar arası sayaçlar tutarlı kalsın diye hepsi
        # artırılır (aksi hâlde konuşma limitine takılan bir saldırgan, TC ve IP
        # bütçelerini hiç harcamadan yeni konuşma açıp devam edebilirdi).
        exceeded: Optional[str] = None
        for scope, raw_key, limit, window_min in checks:
            if not self._consume(db, scope, raw_key, limit, window_min) and exceeded is None:
                exceeded = scope

        # Sayaçları kalıcı yap, sonra temizliği tetikle.
        #
        # DİKKAT — garantiyi sağlayan şey bu SIRA DEĞİL, _cleanup'ın kendi
        # Session'ında çalışması. Sıra yalnızca netlik için burada: çağrı
        # noktasında sayaçlar zaten yazılmış olsun. Temizlik bu Session'a hiç
        # dokunmadığı için sırayı bozmak da artışları geri alamaz — koruma
        # sıradan bağımsızdır (bkz. _cleanup docstring).
        db.commit()
        self._cleanup()

        if exceeded is not None:
            # Log'a yalnızca KAPSAM adı yazılır — TC, IP ya da hash ASLA.
            logger.warning("SC SMS hız sınırı aşıldı kapsam=%s conversation_id=%s",
                           exceeded, conversation_id)
            raise RateLimitExceededException(f"SMS hız sınırı aşıldı (kapsam={exceeded})")
