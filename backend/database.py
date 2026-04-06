from sqlalchemy import Column, BigInteger, Text, SmallInteger, DateTime, ForeignKey, String, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy import create_engine

DATABASE_URL = "postgresql://admin:password123@localhost:5432/auzef_bot"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class QnA(Base):
    __tablename__ = "qna"
    id = Column(BigInteger, primary_key=True, index=True)
    question_text = Column(Text, nullable=False)
    answer_text = Column(Text, nullable=False)
    status = Column(SmallInteger, default=1)
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
                                                                                                                                           
# Veritabanı tablolarını oluştur
def init_db():
    Base.metadata.create_all(bind=engine)

    # View oluşturma SQL'i (PostgreSQL specific)
    view_sql = """
    CREATE OR REPLACE VIEW qna_search_view AS
    SELECT
        q.id,
        q.question_text AS question,
        q.answer_text AS answer,
        ARRAY_REMOVE(ARRAY_AGG(DISTINCT qq.query_text), NULL) AS queries,
        ARRAY_REMOVE(ARRAY_AGG(DISTINCT t.name), NULL) AS tags
    FROM qna q
    LEFT JOIN qna_queries qq ON qq.qna_id = q.id
    LEFT JOIN qna_tags qt ON qt.qna_id = q.id
    LEFT JOIN tags t ON t.id = qt.tag_id
    GROUP BY q.id;
    """

    with engine.connect() as conn:
        from sqlalchemy import text
        conn.execute(text(view_sql))
        conn.commit()
        print("✅ Veritabanı tabloları ve 'qna_search_view' oluşturuldu.")
   