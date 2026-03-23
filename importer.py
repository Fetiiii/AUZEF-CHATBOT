import pandas as pd
from sqlalchemy import text
from database import SessionLocal

def import_real_data(file_path):
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

        # 2. Tagleri İşle (Virgülle ayrılmış varsayıyoruz: "etiket1, etiket2")
        if pd.notna(row['tags']):
            tag_list = [t.strip() for t in str(row['tags']).split(',')]
            for tag_name in tag_list:
                # Önce tag var mı bak, yoksa ekle ve ID'sini al
                tag_id_res = db.execute(
                    text("INSERT INTO tags (name) VALUES (:n) ON CONFLICT (name) DO UPDATE SET name=EXCLUDED.name RETURNING id"),
                    {"n": tag_name}
                ).fetchone()
                tag_id = tag_id_res[0]
                
                # Ara tabloya (qna_tags) ekle
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
    print("✅ Tüm veriler PostgreSQL'e dağıtıldı.")

    # 4. MeiliSearch'ü Senin Yazdığın VIEW Üzerinden Güncelle
    sync_meilisearch(db)
    db.close()

def sync_meilisearch(db):
    import meilisearch
    client = meilisearch.Client('http://localhost:7700', 'masterKey123')
    
    # Senin hazırladığın VIEW'ı kullanıyoruz - Tüm JOIN'ler burada bitmiş geliyor!
    view_data = db.execute(text("SELECT * FROM qna_search_view")).mappings().all()
    documents = [dict(row) for row in view_data]
    
    index = client.index('auzef_qna_index')
    index.add_documents(documents)
    
    # Arama önceliği ayarları
    index.update_searchable_attributes(['question', 'queries', 'tags', 'answer'])
    print(f"✅ MeiliSearch VIEW üzerinden güncellendi. Toplam döküman: {len(documents)}")

# Çalıştır
import_real_data("data/module_479_Qna_Export_Data_2026-01-12 15_21(2).csv")