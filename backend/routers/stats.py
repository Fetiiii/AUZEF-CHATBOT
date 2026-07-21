"""Izleme istatistikleri (admin): ozet + konusma bazli dagilimlar."""
from datetime import datetime, timedelta, date as date_cls

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.database import Conversation, utcnow
from core.deps import get_db

router = APIRouter()

# Türkiye UTC+3, DST yok (2016'dan beri). created_at kolonları NAIVE UTC saklanır.
ISTANBUL_OFFSET = timedelta(hours=3)

# ISO haftası: Pazartesi = index 0 (Python .weekday() ile uyumlu)
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_DAYS_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]


def _istanbul_midnight_utc(d: date_cls) -> datetime:
    """Verilen İstanbul-yerel tarihin gece yarısının NAIVE UTC karşılığı."""
    return datetime(d.year, d.month, d.day) - ISTANBUL_OFFSET


def _build_hourly(db: Session, day: date_cls | None) -> list[dict]:
    """Bir günün 24 saatlik (00:00–23:00) sorgu dağılımı.

    day=None → son 24 saatin kayan penceresi (canlı görünüm).
    day dolu → o günün 00:00–23:00 saatleri.
    """
    if day is None:
        # Kayan son 24 saat — generate_series NOW üzerinden İstanbul saatinde
        rows = db.execute(text("""
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
        weekday = None
        iso_date = None
    else:
        # Belirli gün: gs.hour naive İstanbul saati; created_at → İstanbul'a çevrilip eşleşir.
        # NOT (SQLAlchemy tuzağı): `:param::type` YAZMA — text()'in bind param
        # regex'i, isimden hemen sonra `::` geldiğinde parametreyi tanımıyor ve
        # ":param" harfiyen (substitue edilmeden) SQL'e sızıyor → Postgres "syntax
        # error at or near ':'" veriyor. CAST(:param AS type) kullan.
        rows = db.execute(text("""
            SELECT
                to_char(gs.hour, 'HH24:00') AS label,
                COALESCE(COUNT(ql.id), 0)::int AS count
            FROM generate_series(
                CAST(:day_start AS timestamp),
                CAST(:day_start AS timestamp) + INTERVAL '23 hours',
                INTERVAL '1 hour'
            ) AS gs(hour)
            LEFT JOIN query_logs ql
                ON ql.created_at >= :range_start AND ql.created_at < :range_end
                AND date_trunc('hour', (ql.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Istanbul') = gs.hour
            GROUP BY gs.hour
            ORDER BY gs.hour
        """), {
            "day_start": datetime(day.year, day.month, day.day),
            "range_start": _istanbul_midnight_utc(day),
            "range_end": _istanbul_midnight_utc(day + timedelta(days=1)),
        }).fetchall()
        weekday = TR_DAYS[day.weekday()]
        iso_date = day.isoformat()

    return [{
        "label": r.label,
        "count": r.count,
        "date": iso_date,
        "weekday": weekday,
    } for r in rows]


def _build_daily(db: Session, start: date_cls, end: date_cls, monthly: bool) -> list[dict]:
    """start–end (dahil) arası GÜNLÜK sorgu dağılımı (haftalık/aylık için)."""
    rows = db.execute(text("""
        SELECT
            gs.day::date AS day,
            COALESCE(COUNT(ql.id), 0)::int AS count
        FROM generate_series(CAST(:start AS date), CAST(:end AS date), INTERVAL '1 day') AS gs(day)
        LEFT JOIN query_logs ql
            ON ql.created_at >= :range_start AND ql.created_at < :range_end
            AND ((ql.created_at AT TIME ZONE 'UTC') AT TIME ZONE 'Europe/Istanbul')::date = gs.day::date
        GROUP BY gs.day
        ORDER BY gs.day
    """), {
        "start": start,
        "end": end,
        "range_start": _istanbul_midnight_utc(start),
        "range_end": _istanbul_midnight_utc(end + timedelta(days=1)),
    }).fetchall()

    out = []
    for r in rows:
        d: date_cls = r.day
        # Aylık: x-ekseni etiketi gün numarası. Haftalık: kısa gün adı.
        label = str(d.day) if monthly else TR_DAYS_SHORT[d.weekday()]
        out.append({
            "label": label,
            "count": r.count,
            "date": d.isoformat(),
            "weekday": TR_DAYS[d.weekday()],
        })
    return out


def _parse_day(date_str: str) -> date_cls:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Geçersiz tarih (YYYY-MM-DD bekleniyor).")


def _parse_month(date_str: str) -> date_cls:
    """YYYY-MM veya YYYY-MM-DD kabul eder; ayın ilk gününü döner."""
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str, fmt).date().replace(day=1)
        except ValueError:
            continue
    raise HTTPException(status_code=400, detail="Geçersiz ay (YYYY-MM bekleniyor).")


@router.get("/api/stats")
def get_stats(mode: str = "hourly", date: str = "", db: Session = Depends(get_db)):
    """Özet metrikler + seçilen moda göre trafik dağılımı.

    - mode=hourly: date boşsa son 24 saat, doluysa (YYYY-MM-DD) o günün 24 saati.
    - mode=weekly: date'in ait olduğu haftanın Pzt–Paz 7 günü.
    - mode=monthly: date'in ait olduğu ayın tüm günleri (+ ayın ilk gününün gün adı).

    active_users / total_queries_today / sources her zaman CANLI (bugün/şimdi) kalır;
    yalnızca `traffic` dizisi seçilen moda göre değişir.
    """
    now_utc = utcnow()
    now_istanbul = now_utc + ISTANBUL_OFFSET
    today = now_istanbul.date()

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

    # ── Seçilen moda göre trafik ──
    period_label = ""
    if mode == "weekly":
        anchor = _parse_day(date) if date else today
        monday = anchor - timedelta(days=anchor.weekday())
        sunday = monday + timedelta(days=6)
        traffic = _build_daily(db, monday, sunday, monthly=False)
        period_label = f"{monday.isoformat()} – {sunday.isoformat()}"
    elif mode == "monthly":
        first = _parse_month(date) if date else today.replace(day=1)
        # Bir sonraki ayın ilk günü - 1 = bu ayın son günü
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = next_month - timedelta(days=1)
        traffic = _build_daily(db, first, last, monthly=True)
        period_label = f"{first.strftime('%Y-%m')} · {TR_DAYS[first.weekday()]} başlıyor"
    else:  # hourly (varsayılan)
        mode = "hourly"
        day = _parse_day(date) if date else None
        traffic = _build_hourly(db, day)
        period_label = date if date else "Son 24 saat"

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
        "mode": mode,
        "period_label": period_label,
        "traffic": traffic,
        # geriye dönük uyum: eski istemciler `hourly` bekleyebilir
        "hourly": traffic,
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
