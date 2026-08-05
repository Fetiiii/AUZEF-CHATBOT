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

# ── SMS hız sınırı (P0-1) ────────────────────────────────────────────────────
ENV_SMS_PER_CONVERSATION = "CM_SMS_PER_CONVERSATION"
ENV_SMS_CONVERSATION_WINDOW_MIN = "CM_SMS_CONVERSATION_WINDOW_MIN"
ENV_SMS_PER_TC = "CM_SMS_PER_TC"
ENV_SMS_TC_WINDOW_MIN = "CM_SMS_TC_WINDOW_MIN"
ENV_SMS_PER_IP = "CM_SMS_PER_IP"
ENV_SMS_IP_WINDOW_MIN = "CM_SMS_IP_WINDOW_MIN"
# TC'yi hash'lemek için kullanılan secret. Boşsa CM_SERVICE_TOKEN'dan türetilir
# (bkz. rate_limit.py). RASTGELE ÜRETİLEMEZ: 2 worker farklı secret üretir ve
# TC sayacı worker başına ayrışıp işlevsiz kalırdı.
ENV_HASH_SECRET = "SC_HASH_SECRET"

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

# ── SMS hız sınırı varsayılanları ("Dengeli" profil) ─────────────────────────
# Meşru kullanıcı 1 SMS ister, SMS gelmezse tipik olarak 1-2 kez tekrar dener →
# konuşma limiti 3 onu rahatsız etmez. TC limiti aynı numaraya farklı
# konuşmalardan yapılan bombardımanı, IP limiti ise farklı TC'lerle yapılan
# numaralandırmayı bütçeler (TC limiti numaralandırmayı DURDURMAZ: saldırgan her
# TC'yi yalnızca bir kez dener).
DEFAULT_SMS_PER_CONVERSATION = 3
DEFAULT_SMS_CONVERSATION_WINDOW_MIN = 15
DEFAULT_SMS_PER_TC = 5
DEFAULT_SMS_TC_WINDOW_MIN = 60
DEFAULT_SMS_PER_IP = 30
DEFAULT_SMS_IP_WINDOW_MIN = 60
# Sayaç satırlarının saklanma süresi; bundan eskiler fırsattan istifade silinir.
SMS_COUNTER_RETENTION_HOURS = 24
