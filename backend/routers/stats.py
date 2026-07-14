"""Izleme istatistikleri (admin): ozet + konusma bazli dagilimlar."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import Conversation, utcnow
from core.deps import get_db

router = APIRouter()


@router.get("/api/stats")
def get_stats(db: Session = Depends(get_db)):
    # Turkey is UTC+3 with no DST (since 2016)
    ISTANBUL_OFFSET = timedelta(hours=3)
    now_utc = utcnow()
    now_istanbul = now_utc + ISTANBUL_OFFSET

    # Midnight in Istanbul time, converted back to UTC for DB comparison
    today_start_utc = (now_istanbul.replace(hour=0, minute=0, second=0, microsecond=0)) - ISTANBUL_OFFSET
    five_min_ago = now_utc - timedelta(minutes=5)

    # "Aktif kullanıcı" = son 5 dakikada mesajı olan FARKLI sohbet (oturum) sayısı.
    # IP kullanılamaz: tüm trafik nginx proxy'sinin IP'siyle geldiği için (172.18.0.7)
    # her kullanıcı tek IP'ye çöker. conversation_id her oturuma özel olduğundan
    # proxy arkasında bile kullanıcıları doğru ayrıştırır.
    active_users = db.execute(
        text("SELECT COUNT(DISTINCT conversation_id)::int FROM conversation_messages WHERE created_at >= :since"),
        {"since": five_min_ago}
    ).scalar() or 0

    total_today = db.execute(
        text("SELECT COUNT(*)::int FROM query_logs WHERE created_at >= :today"),
        {"today": today_start_utc}
    ).scalar() or 0

    # generate_series and labels in Istanbul time; created_at stored as UTC so cast accordingly
    hourly_rows = db.execute(text("""
        SELECT
            to_char(gs.hour, 'HH24:00') AS label,
            COALESCE(COUNT(ql.id), 0)::int AS count
        FROM generate_series(
            date_trunc('hour', NOW() AT TIME ZONE 'Europe/Istanbul') - INTERVAL '23 hours',
            date_trunc('hour', NOW() AT TIME ZONE 'Europe/Istanbul'),
            INTERVAL '1 hour'
        ) AS gs(hour)
        LEFT JOIN query_logs ql
            ON date_trunc('hour', (ql.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Istanbul') = gs.hour
        GROUP BY gs.hour
        ORDER BY gs.hour
    """)).fetchall()

    source_rows = db.execute(
        text("""
            SELECT source, COUNT(*)::int AS cnt
            FROM query_logs
            WHERE created_at >= :today
            GROUP BY source
        """),
        {"today": today_start_utc}
    ).fetchall()

    sources = {"meilisearch": 0, "qdrant_vector": 0, "llm": 0, "academic_calendar": 0, "none": 0}
    for row in source_rows:
        if row.source in sources:
            sources[row.source] = row.cnt

    return {
        "active_users": int(active_users),
        "total_queries_today": int(total_today),
        "hourly": [{"label": row.label, "count": row.count} for row in hourly_rows],
        "sources": sources
    }


@router.get("/api/stats/conversations")
def get_conversation_stats(start: str = "", end: str = "", db: Session = Depends(get_db)):
    """Konuşma bazlı istatistikler (tarih filtreli, conversations.started_at üzerinden).

    - rating_distribution: her cevaba verilen puanların 1..5 dağılımı
    - outcome: sohbetin SON puanına göre olumlu(>=4)/olumsuz(<=3)/puansız
    - talep: redirected (yönlendirilen) / declined (oluşturmadan giden) / not_offered

    Tüm sayımlar SQL'de yapılır — Python'a hiç satır çekilmez. (Eski sürüm
    aralıktaki TÜM konuşma ve puanlı mesaj nesnelerini belleğe yüklüyordu;
    100k konuşmada yüzlerce MB ve saniyeler sürüyordu.)
    """
    ISTANBUL_OFFSET = timedelta(hours=3)
    params = {}
    conds = []
    if start:
        try:
            params["start"] = datetime.strptime(start, "%Y-%m-%d") - ISTANBUL_OFFSET
            conds.append("c.started_at >= :start")
        except ValueError:
            raise HTTPException(status_code=400, detail="Geçersiz başlangıç tarihi (YYYY-MM-DD).")
    if end:
        try:
            # bitiş günü dahil olsun diye +1 gün
            params["end"] = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1) - ISTANBUL_OFFSET
            conds.append("c.started_at < :end")
        except ValueError:
            raise HTTPException(status_code=400, detail="Geçersiz bitiş tarihi (YYYY-MM-DD).")
    # Tarih koşulları sabit metinlerdir (kullanıcı girdisi yalnızca bound
    # parametre olarak geçer) — f-string ile eklenmesi güvenlidir.
    where = (" AND " + " AND ".join(conds)) if conds else ""

    # 1) Toplam + talep dağılımı (tek sorgu)
    talep = {"redirected": 0, "declined": 0, "not_offered": 0}
    total = 0
    for row in db.execute(text(f"""
        SELECT c.talep_status, COUNT(*)::int AS cnt
        FROM conversations c
        WHERE TRUE{where}
        GROUP BY c.talep_status
    """), params):
        total += row.cnt
        if row.talep_status in talep:
            talep[row.talep_status] = row.cnt

    # 2) Puan dağılımı (her puanlı cevap sayılır)
    rating_distribution = {str(i): 0 for i in range(1, 6)}
    for row in db.execute(text(f"""
        SELECT cm.rating, COUNT(*)::int AS cnt
        FROM conversation_messages cm
        JOIN conversations c ON c.id = cm.conversation_id
        WHERE cm.rating BETWEEN 1 AND 5{where}
        GROUP BY cm.rating
    """), params):
        rating_distribution[str(row.rating)] = row.cnt

    # 3) Sonuç: her konuşmanın SON puanı (DISTINCT ON + DESC = en son yazılan)
    outcome_row = db.execute(text(f"""
        SELECT
            COUNT(*) FILTER (WHERE last_rating >= 4)::int AS olumlu,
            COUNT(*) FILTER (WHERE last_rating <= 3)::int AS olumsuz,
            COUNT(*)::int AS rated_convs
        FROM (
            SELECT DISTINCT ON (cm.conversation_id) cm.rating AS last_rating
            FROM conversation_messages cm
            JOIN conversations c ON c.id = cm.conversation_id
            WHERE cm.rating IS NOT NULL{where}
            ORDER BY cm.conversation_id, cm.created_at DESC, cm.id DESC
        ) t
    """), params).first()

    return {
        "total_conversations": total,
        "rating_distribution": rating_distribution,
        "outcome": {
            "olumlu": outcome_row.olumlu,
            "olumsuz": outcome_row.olumsuz,
            "puansiz": total - outcome_row.rated_convs,
        },
        "talep": talep,
    }


# ─────────────────────────────────────────────
#  Akademik Takvim CRUD
