import os
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from sqlalchemy import text
from database import SessionLocal

load_dotenv()

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME = "auzef_qna_vectors"


def get_clients():
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    db = SessionLocal()
    return qdrant, db


def setup_qdrant(qdrant_client: QdrantClient, vector_size: int):
    """Qdrant'ta collection oluşturur."""
    collections = qdrant_client.get_collections().collections
    exists = any(c.name == COLLECTION_NAME for c in collections)

    if not exists:
        qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )
        print(f"✅ Qdrant collection '{COLLECTION_NAME}' oluşturuldu.")
    else:
        print(f"ℹ️ Collection '{COLLECTION_NAME}' zaten mevcut.")


def sync_postgres_to_qdrant():
    """PostgreSQL'deki soruları vektöre çevirip Qdrant'a yükler."""
    print("⏳ Embedding modeli yükleniyor (ilk çalıştırmada biraz sürebilir)...")
    model = SentenceTransformer("nezahatkorkmaz/turkce-embedding-bge-m3")
    vector_size = model.get_sentence_embedding_dimension()

    qdrant_client, db = get_clients()
    setup_qdrant(qdrant_client, vector_size)

    print("🚀 Vektör senkronizasyonu başlıyor...")

    # Yalnızca aktif kayıtlar (status = 1) vektörlenir; pasifler indekse girmez.
    view_data = db.execute(text("SELECT * FROM qna_search_view WHERE status = 1")).mappings().all()

    if not view_data:
        print("ℹ️ Veritabanında işlenecek veri yok.")
        db.close()
        return

    # Tüm soruları TEK batch'te encode et (satır satır encode'dan çok daha hızlı).
    questions = [row['question'] for row in view_data]
    print(f"⏳ {len(questions)} soru toplu encode ediliyor...")
    vectors = model.encode(questions)
    points = [
        PointStruct(
            id=row['id'],
            vector=vec.tolist() if hasattr(vec, "tolist") else list(vec),
            payload={"question": row['question'], "answer": row['answer']},
        )
        for row, vec in zip(view_data, vectors)
    ]

    if points:
        qdrant_client.upsert(collection_name=COLLECTION_NAME, points=points)
        print(f"✅ Qdrant'a {len(points)} vektör yüklendi.")

    db.close()


if __name__ == "__main__":
    sync_postgres_to_qdrant()