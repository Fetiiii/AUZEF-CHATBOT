"""Etiya chatbot geçmişi (data/EtiyaChatbot.csv) → conversations/conversation_messages.

AMAÇ: Tek seferlik hacim/performans testi verisi üretmek — üretimdeki
gerçek konuşma verisi DEĞİL. Bu yüzden içerikteki isim/telefon/e-posta/TC
kimlik no gibi bilinen kalıplar best-effort regex ile maskelenir (tam bir
PII taraması değildir — sadece bilinen kalıplar). Yalnızca yerel/staging
bir veritabanında çalıştırılmalıdır.

Kullanım (backend container'ında). `core` paketinin bulunması için `-m`
(modül) formuyla çalıştır — `python scripts/...` çalışmaz, çünkü /app
sys.path'e girmez ("ModuleNotFoundError: No module named 'core'").
    docker exec auzef_backend python -m scripts.import_etiya_history --csv /app/data/EtiyaChatbot.csv --limit-sessions 500 --tag etiya_test
    docker exec auzef_backend python -m scripts.import_etiya_history --tag etiya_2026_06
    docker exec auzef_backend python -m scripts.import_etiya_history --delete-tag etiya_test
"""
import argparse
import random
import re
import sys
import time

import pandas as pd
from sqlalchemy import text

from core.database import SessionLocal, engine

# import_tag kolonu yalnızca bu script için var — kalıcı şemaya (database.py/
# init_db) eklenmiyor çünkü prod boot'unda taşınmasına gerek yok, sadece bu
# test verisini iz sürüp sonradan tek sorguyla silebilmek için.
_ENSURE_TAG_COLUMN = """
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS import_tag VARCHAR(40)
"""
_ENSURE_TAG_INDEX = """
CREATE INDEX IF NOT EXISTS ix_conversations_import_tag ON conversations (import_tag)
"""

FAKE_NAMES = [
    "AYŞE YILMAZ", "MEHMET KAYA", "ZEYNEP DEMİR", "ALİ ÇELİK", "FATMA ŞAHİN",
    "EMRE ARSLAN", "ELİF DOĞAN", "CAN AYDIN", "MERVE KILIÇ", "BURAK ÖZTÜRK",
]


def mask_pii(value: str) -> str:
    """Bilinen kalıpları maskeler: karşılama mesajındaki ad-soyad, TC kimlik no,
    e-posta, telefon. Tam bir PII taraması değildir — bkz. modül docstring."""
    if not value:
        return value
    text_ = value

    def _welcome_name(m):
        return m.group(1) + random.choice(FAKE_NAMES)

    text_ = re.sub(
        r"(hoş\s*geldin[iz]?\s+)([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ\s]{3,40})",
        _welcome_name, text_,
    )
    # TC kimlik no: standalone 11 haneli sayı
    text_ = re.sub(r"(?<!\d)\d{11}(?!\d)", lambda m: "".join(random.choice("0123456789") for _ in range(11)), text_)
    # e-posta
    text_ = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "ornek@ornek.com", text_)
    # telefon (05xx xxx xx xx varyasyonları, boşluksuz/boşluklu)
    text_ = re.sub(r"(?<!\d)0?5\d{2}[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)", "05XXXXXXXXX", text_)
    return text_


def pick_content(row) -> str:
    if row["message_type"] == "quick_reply":
        label = row.get("quick_reply_label")
        if isinstance(label, str) and label.strip():
            return label
    val = row.get("message_text_clean")
    return val if isinstance(val, str) else ""


def delete_tag(tag: str):
    db = SessionLocal()
    try:
        res = db.execute(text("DELETE FROM conversations WHERE import_tag = :tag"), {"tag": tag})
        # query_logs'un import_tag kolonu yok; import edilen trafik satırları
        # source='etiya_import' ile işaretli — hepsini sil. (Gerçek/prod loglarının
        # source'u asla 'etiya_import' olmadığı için bu silme güvenlidir.)
        qres = db.execute(text("DELETE FROM query_logs WHERE source = 'etiya_import'"))
        db.commit()
        print(f"🗑️  Silindi: {res.rowcount} konuşma (+CASCADE mesajları), "
              f"{qres.rowcount} query_log satırı, tag={tag}")
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="/app/data/EtiyaChatbot.csv")
    parser.add_argument("--tag", default="etiya_2026_06", help="conversations.import_tag değeri (temizlik için)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Bir DB round-trip'inde işlenecek oturum sayısı")
    parser.add_argument("--limit-sessions", type=int, default=None, help="Yalnızca ilk N oturumu içe aktar (deneme)")
    parser.add_argument("--no-mask", action="store_true", help="PII maskelemeyi atla (yalnızca güvenli/yerel test ortamında)")
    parser.add_argument("--delete-tag", default=None, help="Verilen tag'e sahip tüm konuşmaları sil ve çık")
    args = parser.parse_args()

    if args.delete_tag:
        delete_tag(args.delete_tag)
        return

    t0 = time.time()
    print(f"📥 CSV okunuyor: {args.csv}")
    df = pd.read_csv(args.csv, encoding="utf-8")
    print(f"   {len(df):,} satır, {df['session_id'].nunique():,} oturum ({time.time()-t0:.1f}s)")

    df["session_start_tr"] = pd.to_datetime(df["session_start_tr"])
    df["session_end_tr"] = pd.to_datetime(df["session_end_tr"])
    df["message_time_tr"] = pd.to_datetime(df["message_time_tr"])
    df = df.sort_values(["session_id", "message_order"])

    session_ids = df["session_id"].drop_duplicates().tolist()
    if args.limit_sessions:
        session_ids = session_ids[: args.limit_sessions]
        df = df[df["session_id"].isin(session_ids)]
    print(f"   İşlenecek oturum: {len(session_ids):,}, mesaj: {len(df):,}")

    with engine.connect() as conn:
        conn.execute(text(_ENSURE_TAG_COLUMN))
        conn.execute(text(_ENSURE_TAG_INDEX))
        conn.commit()

    mask = (lambda s: s) if args.no_mask else mask_pii

    db = SessionLocal()
    total_convs = 0
    total_msgs = 0
    total_qlogs = 0
    grouped = df.groupby("session_id", sort=False)
    batch_sids = []
    try:
        for sid in session_ids:
            batch_sids.append(sid)
            if len(batch_sids) < args.batch_size and sid != session_ids[-1]:
                continue

            # 1) Bu batch'teki konuşmaları oluştur, sid -> yeni id eşlemesi çıkar
            conv_rows = []
            for s in batch_sids:
                g = grouped.get_group(s)
                first = g.iloc[0]
                conv_rows.append({
                    "sid": s,
                    "started_at": first["session_start_tr"].to_pydatetime(),
                    "updated_at": first["session_end_tr"].to_pydatetime(),
                    "tag": args.tag,
                })

            insert_conv_sql = text(
                "INSERT INTO conversations (talep_status, started_at, updated_at, import_tag) "
                "VALUES ('not_offered', :started_at, :updated_at, :tag) RETURNING id"
            )
            sid_to_convid = {}
            for row in conv_rows:
                new_id = db.execute(insert_conv_sql, row).scalar()
                sid_to_convid[row["sid"]] = new_id

            # 2) Bu batch'teki tüm mesajları topla, tek executemany ile ekle
            msg_rows = []
            qlog_rows = []      # Trafik grafiği query_logs'u sayar → her KULLANICI
                                # mesajı = bir sorgu (canlı davranışla aynı).
            for s in batch_sids:
                g = grouped.get_group(s)
                conv_id = sid_to_convid[s]
                for _, r in g.iterrows():
                    role = "user" if r["direction"] == "Kullanıcı" else "bot"
                    content = mask(pick_content(r)) or ""
                    created = r["message_time_tr"].to_pydatetime()
                    msg_rows.append({
                        "conversation_id": conv_id,
                        "role": role,
                        "content": content,
                        "source": "etiya_import",
                        "created_at": created,
                    })
                    if role == "user":
                        qlog_rows.append({"created_at": created})

            if msg_rows:
                db.execute(
                    text(
                        "INSERT INTO conversation_messages "
                        "(conversation_id, role, content, source, created_at) "
                        "VALUES (:conversation_id, :role, :content, :source, :created_at)"
                    ),
                    msg_rows,
                )
            if qlog_rows:
                db.execute(
                    text(
                        "INSERT INTO query_logs (source, status, ip_address, created_at) "
                        "VALUES ('etiya_import', 'success', NULL, :created_at)"
                    ),
                    qlog_rows,
                )
            db.commit()

            total_convs += len(batch_sids)
            total_msgs += len(msg_rows)
            total_qlogs += len(qlog_rows)
            print(f"   ... {total_convs:,}/{len(session_ids):,} oturum, {total_msgs:,} mesaj ({time.time()-t0:.1f}s)")
            batch_sids = []
    finally:
        db.close()

    print(f"✅ Tamamlandı: {total_convs:,} konuşma, {total_msgs:,} mesaj, "
          f"{total_qlogs:,} query_log (trafik) eklendi ({time.time()-t0:.1f}s), tag={args.tag}")
    print(f"   Temizlemek için: python scripts/import_etiya_history.py --delete-tag {args.tag}")


if __name__ == "__main__":
    main()
