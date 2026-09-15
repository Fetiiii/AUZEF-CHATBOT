import os
from datetime import datetime, timezone
from sqlalchemy import Column, BigInteger, Integer, Text, SmallInteger, DateTime, ForeignKey, String, func, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker, relationship
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()


def utcnow() -> datetime:
    """NAIVE UTC 'şimdi'. DB kolonları TIMESTAMP WITHOUT TIME ZONE olduğundan
    tz-aware datetime karışıklık yaratır (karşılaştırma/saklama). datetime.utcnow()
    Python'da deprecated olduğu için merkezî yardımcı: aware üret, tz'i düşür."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

DEFAULT_DATABASE_URL = "postgresql://admin:password123@localhost:5432/auzef_bot"


def resolve_database_urls(environ=None) -> tuple[str, str, str]:
    """DATABASE_URL ile admin/chat fallback sözleşmesini tek yerde uygula."""
    source = os.environ if environ is None else environ
    database_url = source.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    admin_database_url = source.get("ADMIN_DATABASE_URL") or database_url
    chat_database_url = source.get("CHAT_DATABASE_URL") or database_url
    return database_url, admin_database_url, chat_database_url


DATABASE_URL, ADMIN_DATABASE_URL, CHAT_DATABASE_URL = resolve_database_urls()
# Production iki fiziksel PostgreSQL kullanır. Development yalnız DATABASE_URL
# verdiğinde iki sahiplik alanı da aynı engine'e düşer; mevcut Compose akışı bu
# sayede ikinci bir PostgreSQL gerektirmez.


def _create_database_engine(url: str):
    # pool_pre_ping: havuzdaki bağlantı kopmuşsa (ör. Postgres yeniden başladı)
    # sorgudan önce test edilip tazelenir — yoksa ilk istekler OperationalError alır.
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


admin_engine = _create_database_engine(ADMIN_DATABASE_URL)
# Compatibility modunda tek engine/pool kullan. Ayrı URL'lerde ise transaction
# sınırları da fiziksel olarak ayrıdır; Session commit'i distributed/atomic bir
# transaction garantisi vermez ve two-phase commit bilinçli olarak kapalıdır.
chat_engine = (
    admin_engine
    if CHAT_DATABASE_URL == ADMIN_DATABASE_URL
    else _create_database_engine(CHAT_DATABASE_URL)
)
Base = declarative_base()

class QnA(Base):
    __tablename__ = "qna"
    id = Column(BigInteger, primary_key=True, index=True)
    question_text = Column(Text, nullable=False)
    answer_text = Column(Text, nullable=False)
    # server_default: ham SQL INSERT (CSV import) status vermezse DB 1 atar.
    # Eskiden yalnızca client-side default=1 vardı → raw INSERT status'u NULL
    # bırakıyor, status!=1 filtresi de bu kayıtları arama indeksinden DÜŞÜRÜYORDU
    # (CSV ile eklenen QnA'lar sessizce aranamaz oluyordu).
    status = Column(SmallInteger, nullable=False, server_default="1", default=1)
    updated_by = Column(String(255), nullable=True)   # son düzenleyen kullanıcının e-postası (denetim izi)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    queries = relationship("QnAQuery", back_populates="qna", cascade="all, delete-orphan")
    tags = relationship("Tag", secondary="qna_tags", back_populates="qnas")

class QnAQuery(Base):                                                                                                                      
    __tablename__ = "qna_queries"                                                                                                         
    id = Column(BigInteger, primary_key=True, index=True)                                                                                 
    qna_id = Column(BigInteger, ForeignKey("qna.id", ondelete="CASCADE"), nullable=False)                                                  
    query_text = Column(Text, nullable=False)                                                                                             
    query_type = Column(SmallInteger, default=1)                                                                                          
    created_at = Column(DateTime, server_default=func.now())      

    qna = relationship("QnA", back_populates="queries")         

class Tag(Base):
    __tablename__ = "tags"
    id = Column(BigInteger, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    qnas = relationship("QnA", secondary="qna_tags", back_populates="tags")

class QnATag(Base):
    __tablename__ = "qna_tags"
    qna_id = Column(BigInteger, ForeignKey("qna.id", ondelete="CASCADE"), primary_key=True)
    tag_id = Column(BigInteger, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True)

class SystemConfig(Base):
    __tablename__ = "system_config"
    key = Column(String(50), primary_key=True)
    value = Column(String(255), nullable=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

class QueryLog(Base):
    __tablename__ = "query_logs"
    id = Column(BigInteger, primary_key=True, index=True)
    source = Column(String(20), nullable=False)   # meilisearch | qdrant_vector | llm | none
    status = Column(String(20), nullable=False)   # success | suggest | error
    ip_address = Column(String(45), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(BigInteger, primary_key=True, index=True)
    # Sahiplik token'ı: konuşmayı BAŞLATAN tarayıcıya verilir; mesaj ekleme,
    # puanlama ve talep yazma bu token'ı ister. id'ler ardışık sayı olduğu
    # için token olmadan herkes başkasının konuşmasına yazabilir/puanlayabilirdi
    # (istatistikleri sessizce bozar). NULL = eski kayıt → yazma reddedilir.
    client_token = Column(String(64), nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    # not_offered = puan >=4 olduğu için talep hiç sorulmadı
    # declined    = kullanıcı "Hayır" dedi
    # redirected  = kullanıcı "Evet", talep sayfasına yönlendirildi
    talep_status = Column(String(20), default="not_offered")
    started_at = Column(DateTime, server_default=func.now(), index=True)  # liste sıralaması + tarih filtreleri
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    messages = relationship("ConversationMessage", back_populates="conversation", cascade="all, delete-orphan")

class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id = Column(BigInteger, primary_key=True, index=True)
    conversation_id = Column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(10), nullable=False)      # user | bot
    content = Column(Text, nullable=False)
    source = Column(String(20), nullable=True)     # bot: meilisearch|qdrant_vector|llm|academic_calendar|none
    rating = Column(SmallInteger, nullable=True)   # bot cevabına verilen puan (1-5); NULL = verilmedi
    rating_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)  # aktif kullanıcı sorgusu (>= :since)

    conversation = relationship("Conversation", back_populates="messages")

# ── Çözüm Merkezi (talep oluşturma) oturumu ──────────────────────────────────
# Çok-adımlı talep akışının (TC→SMS→OTP→öğrenci→kategori→talep) durumu burada
# tutulur. conversation'a 1:1 bağlıdır; sahiplik conversations.client_token ile
# doğrulanır (ayrı bir token icat edilmez).
#
# GÜVENLİK: verification_token CM oturumunu temsil eder ve ASLA frontend'e
# dönmez — yalnızca bu satırda saklanır, talep oluştururken sunucu içinde
# yeniden kullanılır. TC/OTP HİÇBİR ZAMAN saklanmaz (frontend TC'yi kendi
# belleğinde tutup SMS adımında tekrar gönderir).
class SolutionCenterSession(Base):
    __tablename__ = "solution_center_sessions"
    conversation_id = Column(BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True)
    state = Column(String(40), nullable=False, default="SC_WAIT_TC")
    verification_token = Column(String(255), nullable=True)   # CM oturum token'ı — frontend'e ASLA dönmez
    masked_phone = Column(String(30), nullable=True)          # *******208 (kullanıcıya gösterilebilir)
    students_json = Column(Text, nullable=True)               # JSON: [{ogrenciId,birimAdi,fakulteAdi}]
    selected_student_id = Column(BigInteger, nullable=True)
    category_short_code = Column(String(120), nullable=True)
    expires_at = Column(DateTime, nullable=True, index=True)  # verificationToken son kullanma
    # Yanlış OTP deneme sayacı (ANALIZ.md P0-2). Eşiğe ulaşınca verification_token
    # silinir ve state SC_WAIT_TC'ye döner — kullanıcı akışı baştan başlatmak zorunda.
    # Sayaç BİLİNÇLİ olarak burada, sc_rate_limits'te değil: bu bir zaman penceresi
    # sayacı değil, verificationToken'ın ÖMRÜNE bağlı bir sayaç. Token değişince
    # (yeni SMS) sıfırlanır, token ölünce anlamını yitirir — yani tam olarak bu
    # satırın yaşam döngüsü.
    otp_attempts = Column(Integer, nullable=False, server_default="0", default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ── Çözüm Merkezi SMS hız sınırı sayaçları ───────────────────────────────────
# send-sms ucu, geçerli bir conversation_token dışında hiçbir maliyet taşımıyordu:
# tek bir /widget-chat çağrısıyla token alınıp sınırsız SMS gönderilebiliyor ve
# istenen TC'nin sistemde kayıtlı olup olmadığı (maskeli telefon dönmesinden)
# öğrenilebiliyordu. Bu tablo o iki saldırıyı ayrı ayrı bütçeler.
#
# NEDEN DB, NEDEN BELLEK DEĞİL: uvicorn 2 worker ile çalışıyor (entrypoint.sh);
# süreç-içi bir sayaç her worker'da ayrı yaşar ve gerçek limiti ikiye katlar.
#
# NEDEN TEK TABLO/ÜÇ KAPSAM: tek temizlik rutini, tek atomik artırma sorgusu.
# Sayaç bilinçli olarak SolutionCenterSession'a KONMADI — start_verification o
# satırı her çağrıda sıfırlıyor, sayaç orada olsa limit resetlenebilirdi.
#
# GÜVENLİK: key_hash bir HMAC-SHA256'dır; TC ham hâliyle ASLA yazılmaz
# (bkz. SolutionCenterSession'daki aynı ilke). Secret olmadan hash'ten TC'ye
# dönülemez, secret ile de rainbow-table üretilemez.
class SCRateLimit(Base):
    __tablename__ = "sc_rate_limits"
    scope = Column(String(10), primary_key=True)      # 'conv' | 'tc' | 'ip'
    key_hash = Column(String(64), primary_key=True)   # HMAC-SHA256 hex (ham değer DEĞİL)
    count = Column(Integer, nullable=False, default=0)
    # Sabit pencere (fixed window): pencere dolduğunda count 1'e döner.
    window_started_at = Column(DateTime, nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


# ── Admin kullanıcı sistemi ──────────────────────────────────────────────────
# Sınırlı sayıda personel için oturum tabanlı yönetim erişimi.
# Parolalar bcrypt ile hash'lenir; oturum token'ları DB'de SHA-256 hash'iyle
# saklanır (DB sızsa bile ham token ele geçmez).

class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(BigInteger, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(100), nullable=False)   # bcrypt
    full_name = Column(String(100), nullable=True)
    # Yetki matrisi (auth.py ROLE_LEVEL):
    #   editor      → içerik (QnA, takvim, import/export)
    #   admin       → editor + konuşmalar + istatistikler
    #   super_admin → admin + ayarlar sayfası (kullanıcı yönetimi, LLM, API anahtarı)
    role = Column(String(20), nullable=False, default="admin")
    is_active = Column(SmallInteger, default=1)           # 0 = erişim kapalı (personel ayrıldı vb.)
    created_at = Column(DateTime, server_default=func.now())

    sessions = relationship("AdminSession", back_populates="user", cascade="all, delete-orphan")

class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id = Column(BigInteger, primary_key=True, index=True)
    token_hash = Column(String(64), unique=True, nullable=False)  # sha256(token) hex
    user_id = Column(BigInteger, ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("AdminUser", back_populates="sessions")


# ── Admin girişi kaba kuvvet sayaçları (ANALIZ.md P0-3) ──────────────────────
# /api/auth/login tek IP sınırlamasıyla korunuyordu (chat_limit, sohbet için
# ayarlanmış 10r/s — parola koruması için fiilen etkisiz). Bu tablo iki kapsamı
# ayrı ayrı bütçeler: e-posta (belirli bir hesabı hedefleyen deneme) ve IP
# (tek kaynaktan çok hesap denemesi/spray) — biri diğerinin yerini tutmaz.
#
# sc_rate_limits İLE AYNI KALIP (sabit pencere, atomik UPSERT artırma), ama
# HASH'LEME YOK: e-posta ve IP zaten sistemde açık metin saklanıyor
# (AdminUser.email, Conversation.ip_address) — TC'nin aksine gizlenmesi
# gereken bir veri değil.
class AdminLoginAttempt(Base):
    __tablename__ = "admin_login_attempts"
    scope = Column(String(10), primary_key=True)        # 'email' | 'ip'
    identifier = Column(String(255), primary_key=True)  # e-posta ya da IP, açık metin
    count = Column(Integer, nullable=False, default=0)
    window_started_at = Column(DateTime, nullable=False, index=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AcademicCalendar(Base):
    __tablename__ = "academic_calendar"
    id = Column(BigInteger, primary_key=True, index=True)
    period = Column(String(100), nullable=False)       # Güz Dönemi, Bahar Dönemi, etc.
    event = Column(Text, nullable=False)               # Ara Sınav (Vize), Bütünleme, etc.
    start_date = Column(String(50), nullable=False)    # 08.11.2025
    end_date = Column(String(50), nullable=False)      # 09.11.2025
    updated_by = Column(String(255), nullable=True)    # son düzenleyen kullanıcının e-postası (denetim izi)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

ADMIN_MODELS = (
    QnA,
    QnAQuery,
    Tag,
    QnATag,
    SystemConfig,
    AdminUser,
    AdminSession,
    AdminLoginAttempt,
    AcademicCalendar,
)

CHAT_MODELS = (
    QueryLog,
    Conversation,
    ConversationMessage,
    SolutionCenterSession,
    SCRateLimit,
)

# Açık ve denetlenebilir model -> engine sözleşmesi. Varsayılan Session bind'i
# özellikle YOKTUR: ORM işlemi modelden yönlenemiyorsa veya raw SQL hedef engine
# belirtmiyorsa sessizce yanlış veritabanına gitmek yerine hata vermelidir.
MODEL_BINDS = {
    **{model: admin_engine for model in ADMIN_MODELS},
    **{model: chat_engine for model in CHAT_MODELS},
}
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    binds=MODEL_BINDS,
)

ADMIN_TABLES = tuple(model.__table__ for model in ADMIN_MODELS)
CHAT_TABLES = tuple(model.__table__ for model in CHAT_MODELS)


def execute_admin_sql(db: Session, statement, params=None):
    """Ham SQL'i açıkça DB-ADMIN transaction'ına bağla."""
    return db.execute(
        statement,
        params,
        bind_arguments={"bind": admin_engine},
    )


def execute_chat_sql(db: Session, statement, params=None):
    """Ham SQL'i açıkça DB-CHAT transaction'ına bağla."""
    return db.execute(
        statement,
        params,
        bind_arguments={"bind": chat_engine},
    )


QNA_SEARCH_VIEW_SQL = """
    CREATE OR REPLACE VIEW qna_search_view AS
    SELECT
        q.id,
        q.question_text AS question,
        q.answer_text AS answer,
        ARRAY_REMOVE(ARRAY_AGG(DISTINCT qq.query_text), NULL) AS queries,
        ARRAY_REMOVE(ARRAY_AGG(DISTINCT t.name), NULL) AS tags,
        q.status
    FROM qna q
    LEFT JOIN qna_queries qq ON qq.qna_id = q.id
    LEFT JOIN qna_tags qt ON qt.qna_id = q.id
    LEFT JOIN tags t ON t.id = qt.tag_id
    GROUP BY q.id;
"""

# Var olan (create_all'un dokunmadığı) tablolara da DDL uygula. Alembic henüz
# kullanılmadığı için ifadeler mevcut entrypoint davranışıyla uyumlu ve
# idempotent tutulur. Her liste yalnız kendi DB ownership alanındaki tablolara
# referans verir.
ADMIN_DDL = (
    # Rol sistemi migration'ı: mevcut kullanıcılar varsayılan admin olur.
    "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin'",
    # qna.status ham INSERT'lerde de aktif varsayılsın; eski NULL'lar düzeltilir.
    "ALTER TABLE qna ALTER COLUMN status SET DEFAULT 1",
    "UPDATE qna SET status = 1 WHERE status IS NULL",
    # İçerik değişikliklerinin denetim izi.
    "ALTER TABLE qna ADD COLUMN IF NOT EXISTS updated_by VARCHAR(255)",
    "ALTER TABLE academic_calendar ADD COLUMN IF NOT EXISTS updated_by VARCHAR(255)",
)

CHAT_DDL = (
    "CREATE INDEX IF NOT EXISTS ix_conversations_started_at ON conversations (started_at)",
    "CREATE INDEX IF NOT EXISTS ix_conversation_messages_created_at ON conversation_messages (created_at)",
    # Konuşma sahiplik token'ı: eski satırlar NULL kalır ve fail-closed davranır.
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS client_token VARCHAR(64)",
    "CREATE INDEX IF NOT EXISTS ix_conversations_client_token ON conversations (client_token)",
    # Konuşma araması için PostgreSQL trigram uzantısı ve kısmi GIN index'i.
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS ix_conversation_messages_content_trgm "
    "ON conversation_messages USING gin (content gin_trgm_ops) WHERE role = 'user'",
    # Puan istatistiklerinin yalnız puanlanmış mesajları taraması için kısmi index.
    "CREATE INDEX IF NOT EXISTS ix_conversation_messages_rating "
    "ON conversation_messages (rating) WHERE rating IS NOT NULL",
    # Çözüm Merkezi OTP deneme sayacı: mevcut tabloya idempotent eklenir.
    "ALTER TABLE solution_center_sessions "
    "ADD COLUMN IF NOT EXISTS otp_attempts INTEGER NOT NULL DEFAULT 0",
)


def init_admin_db():
    """Yalnız DB-ADMIN tablolarını, view'ını ve DDL'ini hazırla."""
    Base.metadata.create_all(bind=admin_engine, tables=ADMIN_TABLES)
    with admin_engine.begin() as conn:
        for statement in ADMIN_DDL:
            conn.execute(text(statement))
        # CREATE OR REPLACE VIEW mevcut kolon sırasını değiştiremediğinden önce
        # DROP edilir; qna_search_view yalnız DB-ADMIN üzerinde yaşar.
        conn.execute(text("DROP VIEW IF EXISTS qna_search_view"))
        conn.execute(text(QNA_SEARCH_VIEW_SQL))


def init_chat_db():
    """Yalnız DB-CHAT tablolarını ve DDL/index işlemlerini hazırla."""
    Base.metadata.create_all(bind=chat_engine, tables=CHAT_TABLES)
    with chat_engine.begin() as conn:
        for statement in CHAT_DDL:
            conn.execute(text(statement))


def init_db():
    """Admin ve chat persistence alanlarını yapılandırılmış engine'lerde kur."""
    init_admin_db()
    init_chat_db()
    print("✅ Veritabanı tabloları, index'ler ve 'qna_search_view' oluşturuldu.")
