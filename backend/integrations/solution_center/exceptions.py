"""Çözüm Merkezi entegrasyonuna özel exception'lar.

SPEC kuralı: "API'den dönen hata mesajları doğrudan kullanıcıya gösterilmemeli,
uygulama içinde anlamlı exception'lara dönüştürülmelidir." Bu yüzden her
exception iki mesaj taşır:
  - message       → log/geliştirici için teknik açıklama (ASLA TC/OTP/token içermez)
  - user_message  → kullanıcıya gösterilecek nazik, güvenli Türkçe mesaj
"""
from typing import Optional


class SolutionCenterException(Exception):
    """Tüm Çözüm Merkezi hatalarının kök sınıfı."""

    # Alt sınıflar kendi varsayılan kullanıcı mesajını verir.
    default_user_message = "İşlem sırasında bir sorun oluştu. Lütfen tekrar deneyin."
    # Router bu exception'ı hangi HTTP durumuyla döndürsün.
    http_status = 400

    def __init__(self, message: str = "", user_message: Optional[str] = None):
        super().__init__(message or self.default_user_message)
        self.message = message or self.__class__.__name__
        self.user_message = user_message or self.default_user_message


class InvalidOtpException(SolutionCenterException):
    default_user_message = "Girdiğiniz doğrulama kodu hatalı. Lütfen kontrol edip tekrar deneyin."
    http_status = 400


class StudentNotFoundException(SolutionCenterException):
    default_user_message = "Bu kimlik numarasına bağlı öğrenci kaydı bulunamadı."
    http_status = 404


class TicketCreateException(SolutionCenterException):
    default_user_message = "Talebiniz oluşturulamadı. Lütfen daha sonra tekrar deneyin."
    http_status = 502


class ApiConnectionException(SolutionCenterException):
    default_user_message = "Talep sistemine şu anda ulaşılamıyor. Lütfen birazdan tekrar deneyin."
    http_status = 503


class UnauthorizedException(SolutionCenterException):
    """Service token geçersiz/eksik — kullanıcı hatası DEĞİL, yapılandırma hatası."""
    default_user_message = "Talep sistemi şu anda kullanılamıyor. Lütfen daha sonra tekrar deneyin."
    http_status = 503


class VerificationExpiredException(SolutionCenterException):
    default_user_message = "Doğrulama süreniz doldu. Lütfen talep işlemini yeniden başlatın."
    http_status = 400


class CategoryNotFoundException(SolutionCenterException):
    default_user_message = "Seçtiğiniz kategori geçersiz. Lütfen listeden bir kategori seçin."
    http_status = 400


class RateLimitExceededException(SolutionCenterException):
    """SMS hız sınırı aşıldı (konuşma / TC / IP kapsamlarından biri).

    Mesaj bilinçli olarak HANGİ kapsamın dolduğunu söylemez: "bu TC için limit
    doldu" demek, saldırgana TC'nin sistemde bilindiğini doğrulayan yeni bir
    oracle açardı."""
    default_user_message = (
        "Çok fazla doğrulama isteği gönderildi. "
        "Lütfen bir süre bekleyip tekrar deneyin."
    )
    http_status = 429


class InvalidStateException(SolutionCenterException):
    """Akış adımları sırayla ilerlemedi (ör. OTP'den önce talep oluşturma)."""
    default_user_message = "Talep adımları sırasıyla ilerlemelidir. Lütfen baştan başlayın."
    http_status = 409
