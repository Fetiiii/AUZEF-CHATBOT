"""Akademik takvim CRUD + CSV import/export."""
import csv
import io
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.database import AcademicCalendar
from core.deps import get_db, actor_email as _actor_email, MAX_IMPORT_BYTES
from services.csv_utils import stream_csv as _stream_csv
from admin.auth import current_user
from services.calendar_retrieval import (
    CalendarTerm,
    normalize_academic_year,
    parse_aliases,
    parse_calendar_term,
    resolve_calendar_runtime_config,
    serialize_aliases,
)

logger = logging.getLogger("auzef")
router = APIRouter()


class AcademicCalendarCreateRequest(BaseModel):
    period: str
    event: str
    start_date: str
    end_date: str
    academic_year: Optional[str] = None
    term: Optional[str] = None
    aliases: List[str] = Field(default_factory=list)


class AcademicCalendarUpdateRequest(BaseModel):
    period: Optional[str] = None
    event: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    academic_year: Optional[str] = None
    term: Optional[str] = None
    aliases: Optional[List[str]] = None


class AcademicCalendarBulkUpdateItem(BaseModel):
    id: int
    period: Optional[str] = None
    event: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    academic_year: Optional[str] = None
    term: Optional[str] = None
    aliases: Optional[List[str]] = None


# ─────────────────────────────────────────────
#  Conversation (sohbet) kalıcılığı


def _calendar_dict(r: AcademicCalendar) -> dict:
    return {
        "id": r.id,
        "period": r.period,
        "event": r.event,
        "start_date": r.start_date,
        "end_date": r.end_date,
        "academic_year": r.academic_year,
        "term": r.term,
        "aliases": list(parse_aliases(r.aliases)),
        "updated_by": r.updated_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _validated_year(value: Optional[str]) -> Optional[str]:
    if value is None or not value.strip():
        return None
    normalized = normalize_academic_year(value)
    if normalized is None:
        raise HTTPException(
            status_code=400,
            detail="Akademik yıl YYYY-YYYY biçiminde ve ardışık olmalıdır.",
        )
    return normalized


def _validated_term(value: Optional[str], period: str) -> str:
    if value is not None and value.strip():
        parsed = parse_calendar_term(value)
        if parsed is None:
            raise HTTPException(
                status_code=400,
                detail="Dönem GUZ, BAHAR veya GENERAL olmalıdır.",
            )
        return parsed.value
    period_term = parse_calendar_term(period)
    return (period_term or CalendarTerm.GENERAL).value


@router.get("/api/academic-calendar")
def list_calendar(skip: int = 0, limit: int = 500, db: Session = Depends(get_db)):
    """Tüm akademik takvim kayıtlarını döner."""
    rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).offset(skip).limit(limit).all()
    return [_calendar_dict(r) for r in rows]


@router.post("/api/academic-calendar", status_code=201)
def create_calendar(body: AcademicCalendarCreateRequest, db: Session = Depends(get_db), me=Depends(current_user)):
    """Yeni takvim kaydı oluşturur."""
    configured_year = resolve_calendar_runtime_config(db).current_academic_year
    row = AcademicCalendar(
        period=body.period,
        event=body.event,
        start_date=body.start_date,
        end_date=body.end_date,
        academic_year=_validated_year(body.academic_year) or configured_year,
        term=_validated_term(body.term, body.period),
        aliases=serialize_aliases(body.aliases),
        updated_by=_actor_email(me),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _calendar_dict(row)


@router.put("/api/academic-calendar/bulk-update")
def bulk_update_calendar(items: List[AcademicCalendarBulkUpdateItem], db: Session = Depends(get_db), me=Depends(current_user)):
    """Birden fazla takvim kaydını tek seferde günceller."""
    actor = _actor_email(me)
    updated = []
    for item in items:
        row = db.query(AcademicCalendar).filter(AcademicCalendar.id == item.id).first()
        if not row:
            continue
        changed = False
        if item.period is not None:
            row.period = item.period
            changed = True
        if item.event is not None:
            row.event = item.event
            changed = True
        if item.start_date is not None:
            row.start_date = item.start_date
            changed = True
        if item.end_date is not None:
            row.end_date = item.end_date
            changed = True
        if item.academic_year is not None:
            row.academic_year = _validated_year(item.academic_year)
            changed = True
        if item.term is not None:
            row.term = _validated_term(item.term, row.period)
            changed = True
        if item.aliases is not None:
            row.aliases = serialize_aliases(item.aliases)
            changed = True
        if changed:
            row.updated_by = actor
        updated.append(row.id)
    db.commit()
    return {"updated_ids": updated, "count": len(updated)}


@router.put("/api/academic-calendar/{cal_id}")
def update_calendar(cal_id: int, body: AcademicCalendarUpdateRequest, db: Session = Depends(get_db), me=Depends(current_user)):
    """Tek bir takvim kaydını günceller."""
    row = db.query(AcademicCalendar).filter(AcademicCalendar.id == cal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Takvim kaydı bulunamadı.")
    if body.period is not None:
        row.period = body.period
    if body.event is not None:
        row.event = body.event
    if body.start_date is not None:
        row.start_date = body.start_date
    if body.end_date is not None:
        row.end_date = body.end_date
    if body.academic_year is not None:
        row.academic_year = _validated_year(body.academic_year)
    if body.term is not None:
        row.term = _validated_term(body.term, row.period)
    if body.aliases is not None:
        row.aliases = serialize_aliases(body.aliases)
    row.updated_by = _actor_email(me)
    db.commit()
    db.refresh(row)
    return _calendar_dict(row)


@router.delete("/api/academic-calendar/{cal_id}", status_code=204)
def delete_calendar(cal_id: int, db: Session = Depends(get_db)):
    """Takvim kaydını siler."""
    row = db.query(AcademicCalendar).filter(AcademicCalendar.id == cal_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Takvim kaydı bulunamadı.")
    db.delete(row)
    db.commit()
    return None


# Sync def: bloklayıcı DB işleri içerir (bkz. widget_chat notu).


@router.post("/api/academic-calendar/import")
def import_calendar_csv(file: UploadFile = File(...), db: Session = Depends(get_db), me=Depends(current_user)):
    """CSV dosyasından toplu takvim verisi içe aktarır.
    Kabul edilen formatlar (virgül veya noktalı virgül):
      Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi,Akademik_Yil,Term,Aliases
      period;event;start_date;end_date;academic_year;term;aliases
    """
    actor = _actor_email(me)
    content = file.file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Dosya çok büyük (limit 5 MB).")
    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text_content = content.decode("latin-1")

    # Otomatik delimiter algılama
    first_line = text_content.split("\n")[0]
    delimiter = ";" if ";" in first_line else ","

    reader = csv.DictReader(io.StringIO(text_content), delimiter=delimiter)
    inserted = 0
    configured_year = resolve_calendar_runtime_config(db).current_academic_year

    for row in reader:
        # Hem Türkçe hem İngilizce sütun isimlerini destekle
        period = (row.get("Donem") or row.get("period") or "").strip()
        event = (row.get("Etkinlik") or row.get("event") or "").strip()
        start = (row.get("Baslangic_Tarihi") or row.get("start_date") or "").strip()
        end = (row.get("Bitis_Tarihi") or row.get("end_date") or "").strip()
        academic_year = (row.get("Akademik_Yil") or row.get("academic_year") or "").strip()
        term = (row.get("Term") or row.get("term") or "").strip()
        aliases_text = (row.get("Aliases") or row.get("aliases") or "").strip()

        if not event or not start:
            continue

        db.add(AcademicCalendar(
            period=period,
            event=event,
            start_date=start,
            end_date=end or start,
            academic_year=_validated_year(academic_year) or configured_year,
            term=_validated_term(term or None, period),
            aliases=serialize_aliases(
                item.strip() for item in aliases_text.split("|") if item.strip()
            ),
            updated_by=actor,
        ))
        inserted += 1

    db.commit()
    logger.info(f"Takvim CSV import tamamlandı: {inserted} kayıt eklendi.")
    return {"imported": inserted}


def _calendar_rows(db: Session):
    CHUNK = 1000
    offset = 0
    while True:
        rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).offset(offset).limit(CHUNK).all()
        if not rows:
            break
        for r in rows:
            yield [
                r.period,
                r.event,
                r.start_date,
                r.end_date,
                r.academic_year or "",
                r.term or "",
                "|".join(parse_aliases(r.aliases)),
            ]
        offset += CHUNK


@router.get("/api/academic-calendar/export")
def export_calendar(db: Session = Depends(get_db)):
    """Takvim verisini içe aktarma (import) formatında CSV olarak streaming
    dışa aktarır. Sütunlar import ile birebir aynıdır
    (Donem,Etkinlik,Baslangic_Tarihi,Bitis_Tarihi,Akademik_Yil,Term,Aliases),
    round-trip korunur."""
    header = [
        "Donem", "Etkinlik", "Baslangic_Tarihi", "Bitis_Tarihi",
        "Akademik_Yil", "Term", "Aliases",
    ]
    logger.info("Takvim export (streaming) başladı.")
    return _stream_csv(header, _calendar_rows(db), "akademik_takvim_export.csv", delimiter=",")
