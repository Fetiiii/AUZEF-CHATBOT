"""Çözüm Merkezi entegrasyonu için sabitler.

Magic string YASAK (bkz. SPEC "Claude Code Kuralları"): tüm endpoint yolları,
header adları, kanal kodu ve config env anahtarları burada tek yerde tutulur.
"""

# ── API endpoint yolları (base_url'e eklenir) ────────────────────────────────
PATH_GET_PHONE = "/service/chatbotapi/v1/kimlik-ile-telefon-al"
PATH_SEND_SMS = "/service/chatbotapi/v1/telefona-dogrulama-kodu-gonder"
PATH_VERIFY_CODE = "/service/chatbotapi/v1/dogrulama-kodu-ile-ogrenci-bilgisi-al"
PATH_GET_CATEGORIES = "/service/chatbotapi/v1/tum-kategorileri-al"
PATH_CREATE_TICKET = "/service/chatbotapi/v1/talep-olustur"

# ── HTTP ─────────────────────────────────────────────────────────────────────
AUTH_HEADER = "Authorization"
# DİKKAT: Gerçek OpenAPI securitySchemes'i "Api-Key <KEY>" bekliyor
# (Authorization header, apiKey tipi). SPEC.md'deki "Bearer" YANLIŞ — canlı
# spec ile doğrulandı (2026-07-27). Şema env'den (CM_AUTH_SCHEME) override
# edilebilir; token'ın kendisi bu önekin ARDINA eklenir.
DEFAULT_AUTH_SCHEME = "Api-Key"

# ── Config env anahtarları ───────────────────────────────────────────────────
ENV_BASE_URL = "CM_BASE_URL"
ENV_SERVICE_TOKEN = "CM_SERVICE_TOKEN"
ENV_AUTH_SCHEME = "CM_AUTH_SCHEME"
ENV_CHANNEL_SHORTCODE = "CM_CHANNEL_SHORTCODE"
ENV_TIMEOUT = "CM_TIMEOUT"
ENV_MAX_RETRIES = "CM_MAX_RETRIES"
ENV_VERIFICATION_TTL_MIN = "CM_VERIFICATION_TTL_MIN"
ENV_CATEGORY_CACHE_TTL = "CM_CATEGORY_CACHE_TTL"

# ── Varsayılanlar ────────────────────────────────────────────────────────────
# channel.shortCode SPEC'te "örnek" olarak verilmiş; kesin değer netleşene
# kadar env'den (CM_CHANNEL_SHORTCODE) override edilebilir (kod değişmez).
DEFAULT_CHANNEL_SHORTCODE = "AUZEF_WEB_SAYFASI_CHATBOT"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 2
# verificationToken'ın kaç dakika geçerli sayılacağı (SPEC test senaryosu:
# "Süresi dolmuş verificationToken"). CM tarafı da süre uygular; bu yerel
# koruma kullanıcıya erken/anlamlı hata döndürmek içindir.
DEFAULT_VERIFICATION_TTL_MIN = 10
# Kategori cache TTL (SPEC: 15 dakika).
DEFAULT_CATEGORY_CACHE_TTL_SECONDS = 15 * 60
