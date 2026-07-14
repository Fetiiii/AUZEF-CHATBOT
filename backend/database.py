import os
from datetime import datetime, timezone
from sqlalchemy import Column, BigInteger, Text, SmallInteger, DateTime, ForeignKey, String, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()


def utcnow() -> datetime:
    """NAIVE UTC 'şimdi'. DB kolonları TIMESTAMP WITHOUT TIME ZONE olduğundan
    tz-aware datetime karışıklık yaratır (karşılaştırma/saklama). datetime.utcnow()
    Python'da deprecated olduğu için merkezî yardımcı: aware üret, tz'i düşür."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://admin:password123@localhost:5432/auzef_bot")

# pool_pre_ping: havuzdaki bağlantı kopmuşsa (ör. Postgres yeniden başladı)
# sorgudan önce test edilip tazelenir — yoksa ilk istekler OperationalError alır.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
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

class AcademicCalendar(Base):
    __tablename__ = "academic_calendar"
    id = Column(BigInteger, primary_key=True, index=True)
    period = Column(String(100), nullable=False)       # Güz Dönemi, Bahar Dönemi, etc.
    event = Column(Text, nullable=False)               # Ara Sınav (Vize), Bütünleme, etc.
    start_date = Column(String(50), nullable=False)    # 08.11.2025
    end_date = Column(String(50), nullable=False)      # 09.11.2025
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

# Veritabanı tablolarını oluştur
def init_db():
    Base.metadata.create_all(bind=engine)

    # View oluşturma SQL'i (PostgreSQL specific)
    # NOT: status sütunu eklendi (pasif kayıtların indekslerden düşülmesi için).
    # CREATE OR REPLACE VIEW mevcut sütunların yerini değiştiremediğinden
    # önce DROP edilir (boot sırasında, uvicorn başlamadan çalışır — güvenli).
    view_sql = """
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

    # Var olan (create_all'un dokunmadığı) tablolara da index'leri uygula.
    # Alembic olmadığı için "IF NOT EXISTS" ile her boot'ta idempotent çalışır.
    # İsimler SQLAlchemy'nin index=True adlandırmasıyla (ix_<tablo>_<sütun>)
    # aynı tutuldu ki taze kurulumda çift index oluşmasın.
    index_sql = [
        "CREATE INDEX IF NOT EXISTS ix_conversations_started_at ON conversations (started_at)",
        "CREATE INDEX IF NOT EXISTS ix_conversation_messages_created_at ON conversation_messages (created_at)",
        # Rol sistemi migration'ı: kolon create_all ile YENİ kurulumlarda gelir,
        # var olan tabloya boot'ta eklenir. Mevcut kullanıcılar 'admin' olur
        # (bugüne kadar sahip olmadıkları TEK şey yeni ayarlar sayfasıdır);
        # ilk super_admin CLI ile atanır: create_admin.py <email> --role super_admin
        "ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'admin'",
        # Konuşma sahiplik token'ı (S5): eski satırlar NULL kalır → onlara
        # yazma fail-closed reddedilir (tarihsel veri okunabilir, değiştirilemez).
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS client_token VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_client_token ON conversations (client_token)",
        # qna.status: ham INSERT (CSV import) için DB default'u ata ve eski
        # NULL kalmış (bu yüzden aranamaz olmuş) kayıtları aktife çek.
        "ALTER TABLE qna ALTER COLUMN status SET DEFAULT 1",
        "UPDATE qna SET status = 1 WHERE status IS NULL",
    ]

    with engine.connect() as conn:
        from sqlalchemy import text
        conn.execute(text("DROP VIEW IF EXISTS qna_search_view"))
        conn.execute(text(view_sql))
        for stmt in index_sql:
            conn.execute(text(stmt))
        conn.commit()
        print("✅ Veritabanı tabloları, index'ler ve 'qna_search_view' oluşturuldu.")
   