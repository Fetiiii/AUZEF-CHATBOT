"""Etiya chatbot geçmişi → conversations/conversation_messages.

İki kaynak modu var:
- --csv : küçük/orta CSV (pandas, tamamı belleğe alınır — bkz. eski
  EtiyaChatbot.csv, 505K satır, sorunsuz).
- --xlsx: 6M+ satırlık tam yıllık export (data/chatbot.xlsx). Excel'in
  sayfa başı ~1M satır sınırı yüzünden 7 sayfaya bölünmüş (workbook.xml'de
  "0M-1M".."6M-7M"), ve bir KONUŞMA sayfa sınırında bölünebiliyor (sheet
  N'in son satırı ile sheet N+1'in ilk satırı aynı SessionId olabilir —
  doğrulandı). Bu yüzden pandas.read_csv+groupby YOK: ham XML akış
  (zipfile + xml.etree.iterparse) ile satır satır okunur, mevcut oturum
  sayfa geçişlerinde de tamponda tutulur, farklı bir SessionId görülünce
  flush edilir. pandas ile tamamını belleğe almak 4GB'lık backend
  container'ında OOM'a yol açar. DB yazımı da satır-satır RETURNING yerine
  psycopg2 execute_values (toplu VALUES + toplu RETURNING) ile yapılır —
  bu ölçekte tek tek round-trip çok yavaş kalır.

AMAÇ: Tek seferlik hacim/performans testi verisi üretmek — üretimdeki
gerçek konuşma verisi DEĞİL. Bu yüzden içerikteki isim/telefon/e-posta/TC
kimlik no gibi bilinen kalıplar best-effort regex ile maskelenir (tam bir
PII taraması değildir — sadece bilinen kalıplar). Yalnızca yerel/staging
bir veritabanında çalıştırılmalıdır.

Kullanım (backend container'ında). `core` paketinin bulunması için `-m`
(modül) formuyla çalıştır — `python scripts/...` çalışmaz, çünkü /app
sys.path'e girmez ("ModuleNotFoundError: No module named 'core'").
    docker exec auzef_backend python -m scripts.import_etiya_history --csv /app/data/EtiyaChatbot.csv --limit-sessions 500 --tag etiya_test
    docker exec auzef_backend python -m scripts.import_etiya_history --xlsx /app/data/chatbot.xlsx --limit-sessions 2500 --tag xlsx_test
    docker exec auzef_backend python -m scripts.import_etiya_history --xlsx /app/data/chatbot.xlsx --tag etiya_full_2025_2026
    docker exec auzef_backend python -m scripts.import_etiya_history --delete-tag etiya_test
"""
import argparse
import random
import re
import time
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
from psycopg2.extras import execute_values
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
# query_logs'un da kendi import_tag'i olmalı: source='etiya_import' TÜM içe
# aktarma batch'lerinde ortak olduğu için (bug: daha önce --delete-tag X,
# başka bir batch olan Y'nin query_logs'unu da sildi — source eşleşmesi tag'e
# özgü değildi). Artık silme bu kolonla kapsamlanıyor.
_ENSURE_QLOG_TAG_COLUMN = """
ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS import_tag VARCHAR(40)
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
        db.execute(text(_ENSURE_QLOG_TAG_COLUMN))
        res = db.execute(text("DELETE FROM conversations WHERE import_tag = :tag"), {"tag": tag})
        qres = db.execute(text("DELETE FROM query_logs WHERE import_tag = :tag"), {"tag": tag})
        db.commit()
        print(f"🗑️  Silindi: {res.rowcount} konuşma (+CASCADE mesajları), "
              f"{qres.rowcount} query_log satırı, tag={tag}")
    finally:
        db.close()


def run_csv_import(args, mask):
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

    mask_ = mask
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
                    content = mask_(pick_content(r)) or ""
                    created = r["message_time_tr"].to_pydatetime()
                    msg_rows.append({
                        "conversation_id": conv_id,
                        "role": role,
                        "content": content,
                        "source": "etiya_import",
                        "created_at": created,
                    })
                    if role == "user":
                        qlog_rows.append({"created_at": created, "tag": args.tag})

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
                        "INSERT INTO query_logs (source, status, ip_address, created_at, import_tag) "
                        "VALUES ('etiya_import', 'success', NULL, :created_at, :tag)"
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
    print(f"   Temizlemek için: python -m scripts.import_etiya_history --delete-tag {args.tag}")


# ─── xlsx streaming modu ─────────────────────────────────────────────────────

# workbook.xml.rels sırasıyla doğrulandı: sheet1..7 == "0M-1M".."6M-7M" sırası.
_XLSX_SHEET_FILES = [f"xl/worksheets/sheet{i}.xml" for i in range(1, 8)]
_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XLSX_COLS = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L", "M", "N", "O", "P"]
_XLSX_FIELDS = [
    "SessionId", "Channel", "SessionStatus", "SessionStartTr", "SessionEndTr",
    "DurationMinutes", "MessageOrder", "MessageId", "Direction", "MessageType",
    "MessageTextClean", "QuickReplyLabel", "MessageFeedback", "MessageTimeTr",
    "BotName", "TenantName",
]


def _xlsx_row_cells(elem):
    cells = {}
    for c in elem:
        col = "".join(ch for ch in c.get("r", "") if ch.isalpha())
        v = c.find(_XLSX_NS + "v")
        cells[col] = v.text if v is not None else None
    return cells


def iter_xlsx_rows(path):
    """xlsx'i sabit bellekle satır satır tarar (zipfile + iterparse — pandas
    yok). Her sayfanın kendi başlık satırını atlar ve tüm sayfalarda aynı
    başlığın geldiğini doğrular (format sürprizini erken yakalamak için)."""
    z = zipfile.ZipFile(path)
    reference_header = None
    for sheet_file in _XLSX_SHEET_FILES:
        with z.open(sheet_file) as f:
            first = True
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag != _XLSX_NS + "row":
                    continue
                cells = _xlsx_row_cells(elem)
                elem.clear()
                if first:
                    first = False
                    header = [cells.get(col) for col in _XLSX_COLS]
                    if reference_header is None:
                        reference_header = header
                    elif header != reference_header:
                        raise RuntimeError(f"{sheet_file}: beklenmeyen başlık satırı: {header}")
                    continue
                yield {field: cells.get(col) for field, col in zip(_XLSX_FIELDS, _XLSX_COLS)}


def pick_content_xlsx(row) -> str:
    if row.get("MessageType") == "quick_reply":
        label = row.get("QuickReplyLabel")
        if label:
            return label
    return row.get("MessageTextClean") or ""


def run_xlsx_import(args, mask):
    t0 = time.time()
    print(f"📥 xlsx akış modu (streaming): {args.xlsx}")

    raw_conn = engine.raw_connection()
    cur = raw_conn.cursor()

    seen_sessions = set()
    conv_batch = []   # [(sid, [row, ...]), ...]
    current_sid = None
    current_rows = []
    total_convs = total_msgs = total_qlogs = total_rows = 0
    session_count = 0
    stop = False

    def flush_batch():
        nonlocal total_convs, total_msgs, total_qlogs, conv_batch
        if not conv_batch:
            return
        conv_params = [
            (rows[0]["SessionStartTr"], rows[-1]["SessionEndTr"] or rows[0]["SessionEndTr"], args.tag)
            for _, rows in conv_batch
        ]
        result = execute_values(
            cur,
            "INSERT INTO conversations (talep_status, started_at, updated_at, import_tag) VALUES %s RETURNING id",
            conv_params,
            template="('not_offered', %s, %s, %s)",
            fetch=True,
        )
        new_ids = [r[0] for r in result]

        msg_params = []
        qlog_params = []
        for (sid, rows), conv_id in zip(conv_batch, new_ids):
            for r in rows:
                role = "user" if r["Direction"] == "Kullanıcı" else "bot"
                content = mask(pick_content_xlsx(r)) or ""
                created = r["MessageTimeTr"]
                msg_params.append((conv_id, role, content, "etiya_import", created))
                if role == "user":
                    qlog_params.append((created, args.tag))

        if msg_params:
            execute_values(
                cur,
                "INSERT INTO conversation_messages (conversation_id, role, content, source, created_at) VALUES %s",
                msg_params,
            )
        if qlog_params:
            execute_values(
                cur,
                "INSERT INTO query_logs (source, status, ip_address, created_at, import_tag) VALUES %s",
                qlog_params,
                template="('etiya_import', 'success', NULL, %s, %s)",
            )
        raw_conn.commit()

        total_convs += len(conv_batch)
        total_msgs += len(msg_params)
        total_qlogs += len(qlog_params)
        conv_batch = []
        print(f"   ... {total_convs:,} oturum, {total_msgs:,} mesaj ({time.time()-t0:.1f}s)")

    def flush_session(sid, rows):
        nonlocal session_count, stop
        if sid in seen_sessions:
            raise RuntimeError(
                f"Oturum kaynakta ardışık değil / tekrar görüldü: {sid} — streaming "
                "varsayımı (bir oturumun tüm satırları art arda gelir) bozulmuş olabilir."
            )
        seen_sessions.add(sid)
        conv_batch.append((sid, rows))
        session_count += 1
        if len(conv_batch) >= args.batch_size:
            flush_batch()
        if args.limit_sessions and session_count >= args.limit_sessions:
            stop = True

    try:
        for row in iter_xlsx_rows(args.xlsx):
            total_rows += 1
            sid = row["SessionId"]
            if sid != current_sid:
                if current_sid is not None:
                    flush_session(current_sid, current_rows)
                    if stop:
                        break
                current_sid = sid
                current_rows = [row]
            else:
                current_rows.append(row)
        else:
            if current_sid is not None and not stop:
                flush_session(current_sid, current_rows)
        flush_batch()
    finally:
        cur.close()
        raw_conn.close()

    print(f"✅ Tamamlandı: {total_convs:,} konuşma, {total_msgs:,} mesaj, "
          f"{total_qlogs:,} query_log (trafik), {total_rows:,} satır tarandı "
          f"({time.time()-t0:.1f}s), tag={args.tag}")
    print(f"   Temizlemek için: python -m scripts.import_etiya_history --delete-tag {args.tag}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="/app/data/EtiyaChatbot.csv")
    parser.add_argument("--xlsx", default=None, help="xlsx dosya yolu (6M+ satır, streaming mod) — verilirse --csv yerine bu kullanılır")
    parser.add_argument("--tag", default="etiya_2026_06", help="conversations.import_tag değeri (temizlik için)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Bir DB round-trip'inde işlenecek oturum sayısı")
    parser.add_argument("--limit-sessions", type=int, default=None, help="Yalnızca ilk N oturumu içe aktar (deneme)")
    parser.add_argument("--no-mask", action="store_true", help="PII maskelemeyi atla (yalnızca güvenli/yerel test ortamında)")
    parser.add_argument("--delete-tag", default=None, help="Verilen tag'e sahip tüm konuşmaları sil ve çık")
    args = parser.parse_args()

    if args.delete_tag:
        delete_tag(args.delete_tag)
        return

    with engine.connect() as conn:
        conn.execute(text(_ENSURE_TAG_COLUMN))
        conn.execute(text(_ENSURE_TAG_INDEX))
        conn.execute(text(_ENSURE_QLOG_TAG_COLUMN))
        conn.commit()

    mask = (lambda s: s) if args.no_mask else mask_pii

    if args.xlsx:
        run_xlsx_import(args, mask)
    else:
        run_csv_import(args, mask)


if __name__ == "__main__":
    main()
