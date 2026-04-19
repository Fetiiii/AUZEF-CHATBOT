import os
import pandas as pd
from sqlalchemy import text
from database import SessionLocal
from dotenv import load_dotenv

load_dotenv()


def import_real_data(file_path: str):
    db = SessionLocal()
    df = pd.read_csv(file_path, delimiter=';')

    print(f"📊 {len(df)} satır veri işleniyor...")

    for _, row in df.iterrows():
        # 1. Ana Soru ve Cevabı Kaydet (QnA)
        qna_result = db.execute(
            text("INSERT INTO qna (question_text, answer_text) VALUES (:q, :a) RETURNING id"),
            {"q": row['question'], "a": row['answer']}
        ).fetchone()
        qna_id = qna_result[0]

        # 2. Tagleri İşle
        if pd.notna(row.get('tags')):
            tag_list = [t.strip() for t in str(row['tags']).split(',')]
            for tag_name in tag_list:
                tag_id_res = db.execute(
                    text("INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id"),
                    {"n": tag_name}
                ).fetchone()
                tag_id = tag_id_res[0]

                db.execute(
                    text("INSERT INTO qna_tags (qna_id, tag_id) VALUES (:q_id, :t_id) ON CONFLICT DO NOTHING"),
                    {"q_id": qna_id, "t_id": tag_id}
                )

        # 3. Query_1'den Query_20'ye kadar olanları işle
        for i in range(1, 21):
            col_name = f'query_{i}'
            if col_name in df.columns and pd.notna(row[col_name]):
                query_val = str(row[col_name]).strip()
                if query_val:
                    db.execute(
                        text("INSERT INTO qna_queries (qna_id, query_text) VALUES (:q_id, :qt)"),
                        {"q_id": qna_id, "qt": query_val}
                    )

    db.commit()
    print("✅ Tüm veriler PostgreSQL'e yüklendi.")

    # 4. MeiliSearch'ü Güncelle
    sync_meilisearch(db)
    db.close()


def sync_meilisearch(db):
    import meilisearch

    meili_url = os.getenv("MEILI_URL", "http://localhost:7700")
    meili_key = os.getenv("MEILI_MASTER_KEY", "masterKey123")

    client = meilisearch.Client(meili_url, meili_key)

    view_data = db.execute(text("SELECT * FROM qna_search_view")).mappings().all()
    documents = [dict(row) for row in view_data]

    index = client.index('auzef_qna_index')
    index.add_documents(documents)

    index.update_searchable_attributes(['question', 'queries', 'tags', 'answer'])
    print(f"✅ MeiliSearch güncellendi. Toplam döküman: {len(documents)}")


if __name__ == "__main__":
    import sys
    file_path = sys.argv[1] if len(sys.argv) > 1 else "data/module_479_Qna.csv"
    import_real_data(file_path)