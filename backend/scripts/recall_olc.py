"""Getirme kalitesini Qdrant'a DOGRUDAN sorarak olcer (LLM devre disi).

NEDEN AYRI BIR OLCUM: widget uzerinden olcmek getirmeyi degil, getirme +
LLM havuz-secimini birlikte olcer. Bir kaydin bulunamamasi ile LLM'in baska
bir kaydi tercih etmesi disaridan ayirt edilemez. Bu script araya hicbir sey
koymadan "dogru kayit kacinci sirada geldi" sorusunu cevaplar.

Olculen: her QnA kaydinin alias'lari sorgu olarak gonderilir, kaydin kendi
id'si sonuclarda kacinci sirada cikiyor diye bakilir. Alias'lar vektorlenmis
olmasaydi recall@1 %44 civarinda kalirdi (olculdu, 2026-09-10).

Kullanim (sunucuda):
    docker exec -e PYTHONPATH=/app -w /app auzef_backend \
        python scripts/recall_olc.py

Ornek sayisini degistirmek icin:  ORNEK=200 python scripts/recall_olc.py
"""

import os
import random
import sys

sys.path.insert(0, "/app")

from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer
from sqlalchemy import text

from core.database import SessionLocal
from services.providers import _usable_aliases

COLLECTION = "auzef_qna_vectors"
SAMPLE = int(os.getenv("ORNEK", "300"))
SEED = 20260911  # sabit tohum: iki kosu ayni ornegi kullansin


def main() -> None:
    db = SessionLocal()
    rows = db.execute(text("SELECT * FROM qna_search_view WHERE status = 1")).mappings().all()

    # (sorgu, beklenen qna_id) ciftleri — yalniz alias'lar; kanonik soruyu
    # sormak indeksin kendisini sormak olurdu, olcmek istedigimiz o degil.
    pairs = [(alias, row["id"]) for row in rows for alias in _usable_aliases(row.get("queries"))]
    if not pairs:
        print("Alias bulunamadi — kayitlarda query_ alani yok.")
        return

    random.Random(SEED).shuffle(pairs)
    pairs = pairs[:SAMPLE]
    print(f"{len(rows)} kayit, {len(pairs)} alias sorgusu olculuyor...\n")

    model = SentenceTransformer("nezahatkorkmaz/turkce-embedding-bge-m3")
    client = QdrantClient(
        host=os.getenv("QDRANT_HOST", "qdrant"),
        port=int(os.getenv("QDRANT_PORT", "6333")),
        timeout=120,
    )

    at1 = at5 = at24 = 0
    scores = []
    for index, (query, expected_id) in enumerate(pairs, start=1):
        hits = client.query_points(
            collection_name=COLLECTION,
            query=model.encode(query).tolist(),
            limit=24,
        ).points
        # Alias noktalari ayri id tasir; kaydi payload'daki qna_id belirler.
        found = [hit.payload.get("qna_id") for hit in hits]
        if found and found[0] == expected_id:
            at1 += 1
        if expected_id in found[:5]:
            at5 += 1
        if expected_id in found:
            at24 += 1
        if hits:
            scores.append(hits[0].score)
        if index % 50 == 0:
            print(f"  {index}/{len(pairs)}")

    total = len(pairs)
    print(f"\n{'=' * 46}")
    print(f"recall@1    {at1 / total:6.1%}   ({at1}/{total})")
    print(f"recall@5    {at5 / total:6.1%}")
    print(f"recall@24   {at24 / total:6.1%}")
    print(f"ort. skor   {sum(scores) / len(scores):6.3f}")
    print(f"\nKarsilastirma (2026-09-10, lokal, alias'siz kod): recall@1 %43,8")
    print(f"Karsilastirma (2026-09-10, lokal, alias'li kod):  recall@1 %100,0")
    db.close()


if __name__ == "__main__":
    main()
