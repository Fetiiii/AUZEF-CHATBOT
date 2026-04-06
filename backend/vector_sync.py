import pandas as pd
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from sqlalchemy import text
from database import SessionLocal

# 1. Bağlantıları Kur
qdrant_client = QdrantClient(host="localhost", port=6333)
db = SessionLocal()

# 2. Embedding Modelini Yükle
print("⏳ Model yükleniyor (Bu ilk seferde biraz sürebilir)...")
model = SentenceTransformer("nezahatkorkmaz/turkce-embedding-bge-m3")
vector_size = model.get_sentence_embedding_dimension() 

COLLECTION_NAME = "auzef_qna_vectors"

def setup_qdrant():
    """Qdrant'ta Collection (Klasör) oluşturur."""
    collections = qdrant_client.get_collections().collections
    exists = any(c.name == COLLECTION_NAME for c in collections)
    
    if not exists:
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        print(f"✅ Qdrant Collection '{COLLECTION_NAME}' oluşturuldu.")
    else:
        print(f"ℹ️ Collection '{COLLECTION_NAME}' zaten var.")

def sync_postgres_to_qdrant():
    """PostgreSQL'deki soruları vektöre çevirip Qdrant'a yükler."""
    print("🚀 Vektör senkronizasyonu başlıyor...")
    
    # 1. PostgreSQL'den verileri çek
    view_data = db.execute(text("SELECT * FROM qna_search_view")).mappings().all()
    
    if not view_data:
        print("ℹ️ Veri tabanında işlenecek veri bulunamadı.")
        return

    points = []
    
    # 2. Her satırı vektöre çevir
    for row in view_data:
        qna_id = row['id']
        main_question = row['question']
        answer_text = row['answer']
        
        text_to_embed = main_question
        
        print(f"⏳ İşleniyor (ID: {qna_id}): {main_question[:50]}...")
        
        vector = model.encode(text_to_embed).tolist()
        
        point = PointStruct(
            id=qna_id, 
            vector=vector,
            payload={
                "question": main_question,
                "answer": answer_text,
            }
        )
        points.append(point)

# 3. Vektörleri Qdrant'a yükle
    if points:
        qdrant_client.upsert(
            collection_name=COLLECTION_NAME,
            points=points
        )
        print(f"✅ Qdrant'a {len(points)} vektör başarıyla yüklendi.")
    
    db.close()

if __name__ == "__main__":
    setup_qdrant()
    sync_postgres_to_qdrant()