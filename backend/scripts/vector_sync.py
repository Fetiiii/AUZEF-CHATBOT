import os
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from sqlalchemy import text
from core.database import SessionLocal
from services.providers import (
    ALIAS_ID_OFFSET,
    MAX_ALIASES_PER_QNA,
    _usable_aliases,
)

load_dotenv()

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
COLLECTION_NAME = "auzef_qna_vectors"
#: Tek upsert isteğine giren azami nokta.
UPSERT_BATCH = 256


def get_clients():
    # Gömme uzun sürebiliyor; varsayılan istemci zaman aşımı bu iş için kısa.
    qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=120)
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

    # Kanonik soru + alternatif ifadeler (alias) TEK batch'te encode edilir.
    # Alias'lar da ayrı nokta olarak indekslenir: öğrenci "Sınav kitapçığı"
    # yazarken KB'de cilalı bir cümle duruyor ve yalnız kanonik cümle
    # vektörlenirse ikisi birbirine yeterince yakın düşmüyor.
    flat = []
    per_row = []
    for row in view_data:
        aliases = _usable_aliases(row.get('queries'))
        per_row.append(aliases)
        flat.append(row['question'])
        flat.extend(aliases)
    print(f"⏳ {len(view_data)} kayıt / {len(flat)} metin toplu encode ediliyor...")
    vectors = model.encode(flat)

    points = []
    cursor = 0
    for row, aliases in zip(view_data, per_row):
        payload = {"question": row['question'], "answer": row['answer'], "qna_id": row['id']}
        vec = vectors[cursor]
        points.append(PointStruct(
            id=row['id'],
            vector=vec.tolist() if hasattr(vec, "tolist") else list(vec),
            payload=payload,
        ))
        for position, (alias, avec) in enumerate(zip(aliases, vectors[cursor + 1:]), start=1):
            points.append(PointStruct(
                id=ALIAS_ID_OFFSET + row['id'] * MAX_ALIASES_PER_QNA + position,
                vector=avec.tolist() if hasattr(avec, "tolist") else list(avec),
                payload={**payload, "matched_query": alias},
            ))
        cursor += 1 + len(aliases)

    # PARÇALI UPSERT: alias'larla birlikte nokta sayısı 299'dan ~2.800'e
    # çıktı ve tek istekte gönderim Qdrant tarafında zaman aşımına düşüyordu
    # ("ResponseHandlingException: timed out"). Parçalamak hem o hatayı
    # kaldırıyor hem de ilerlemeyi görünür kılıyor — tek çağrıda hiçbir şey
    # yazılmadan dakikalarca beklemek, işin ilerlemediği izlenimi veriyordu.
    for start in range(0, len(points), UPSERT_BATCH):
        batch = points[start : start + UPSERT_BATCH]
        qdrant_client.upsert(collection_name=COLLECTION_NAME, points=batch)
        print(f"  ↑ {min(start + len(batch), len(points))}/{len(points)} nokta yüklendi.")
    if points:
        print(f"✅ Qdrant'a {len(points)} vektör yüklendi.")

    db.close()


if __name__ == "__main__":
    sync_postgres_to_qdrant()