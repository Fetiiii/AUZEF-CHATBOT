"""PostgreSQL-backed read model for the Solution Center QnA API."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, case, exists, func, literal, or_, select, text, union_all
from sqlalchemy.orm import Session, selectinload

from core.database import QnA, QnAIntegrationDeletion, admin_engine, execute_admin_sql

from .schemas import (
    ChangesResponse,
    DeleteChange,
    MetaResponse,
    Pagination,
    QnAItem,
    QnAResponse,
    UpsertChange,
)


class InvalidCursor(ValueError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_db_datetime(value: datetime) -> datetime:
    return _as_utc(value).replace(tzinfo=None)


def _iso_z(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def _encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str, expected_kind: str) -> dict[str, Any]:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError
        if payload.get("kind") != expected_kind:
            raise ValueError
        return payload
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursor("Invalid pagination cursor.") from exc


def _parse_cursor_datetime(payload: dict[str, Any], field: str) -> datetime:
    try:
        value = str(payload[field]).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return _as_utc(parsed)
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursor("Invalid pagination cursor.") from exc


class QnAIntegrationService:
    """Queries existing QnA models directly; no duplicate repository layer."""

    def __init__(self, db: Session):
        self.db = db

    def _database_now(self) -> datetime:
        # Wait for QnA/tombstone writers that started before this watermark.
        # SHARE conflicts with their ROW EXCLUSIVE table locks. Acquire the
        # tombstone table first: the admin hard-delete path writes it before
        # deleting QnA, so this order also avoids a lock-order deadlock.
        # The transaction ends before any page query; there is no long-lived
        # snapshot or lock between cursor requests.
        with admin_engine.begin() as connection:
            connection.execute(
                text("LOCK TABLE qna_integration_deletions, qna IN SHARE MODE")
            )
            value = connection.execute(select(func.clock_timestamp())).scalar_one()
        return _as_utc(value)

    @staticmethod
    def _item(row: QnA) -> QnAItem:
        changed_at = row.updated_at or row.created_at
        if changed_at is None:  # Existing schema supplies both server defaults.
            raise RuntimeError(f"QnA {row.id} has no timestamp")
        return QnAItem(
            id=row.id,
            question=row.question_text,
            answer=row.answer_text,
            categories=sorted({tag.name for tag in row.tags}),
            updated_at=_as_utc(changed_at),
        )

    def list_qna(self, *, limit: int, cursor: str | None) -> QnAResponse:
        last_id = 0
        if cursor:
            payload = _decode_cursor(cursor, "qna")
            try:
                last_id = int(payload["last_id"])
                if last_id < 0:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor("Invalid pagination cursor.") from exc
            until = _parse_cursor_datetime(payload, "until")
        else:
            until = self._database_now()

        rows = (
            self.db.query(QnA)
            .options(selectinload(QnA.tags))
            .filter(QnA.status == 1, QnA.id > last_id)
            .order_by(QnA.id.asc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more:
            next_cursor = _encode_cursor(
                {
                    "v": 1,
                    "kind": "qna",
                    "last_id": page[-1].id,
                    "until": _iso_z(until),
                }
            )
        return QnAResponse(
            until=until,
            items=[self._item(row) for row in page],
            pagination=Pagination(next_cursor=next_cursor, has_more=has_more),
        )

    @staticmethod
    def _change_source():
        qna_changed_at = func.coalesce(QnA.updated_at, QnA.created_at)
        qna_changes = select(
            QnA.id.label("qna_id"),
            case((QnA.status == 1, literal("upsert")), else_=literal("delete")).label(
                "operation"
            ),
            qna_changed_at.label("changed_at"),
        ).where(qna_changed_at.is_not(None))

        # A restored/re-created id wins over an older tombstone.
        deletion_changes = select(
            QnAIntegrationDeletion.qna_id.label("qna_id"),
            literal("delete").label("operation"),
            QnAIntegrationDeletion.deleted_at.label("changed_at"),
        ).where(~exists(select(1).where(QnA.id == QnAIntegrationDeletion.qna_id)))
        return union_all(qna_changes, deletion_changes).subquery(
            "integration_qna_changes"
        )

    def list_changes(
        self,
        *,
        since: datetime,
        limit: int,
        cursor: str | None,
    ) -> ChangesResponse:
        since_utc = _as_utc(since)
        source = self._change_source()
        last_changed_at: datetime | None = None
        last_id = 0
        last_operation = ""

        if cursor:
            payload = _decode_cursor(cursor, "changes")
            cursor_since = _parse_cursor_datetime(payload, "since")
            if cursor_since != since_utc:
                raise InvalidCursor(
                    "Cursor does not belong to the supplied since value."
                )
            until = _parse_cursor_datetime(payload, "until")
            last_changed_at = _parse_cursor_datetime(payload, "last_changed_at")
            try:
                last_id = int(payload["last_id"])
                last_operation = str(payload["last_operation"])
                if last_id < 0 or last_operation not in {"delete", "upsert"}:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursor("Invalid pagination cursor.") from exc
        else:
            until = self._database_now()

        conditions = [
            # Inclusive boundary tolerates two writes sharing one timestamp
            # tick; consumers already apply upsert/delete idempotently.
            source.c.changed_at >= _as_db_datetime(since_utc),
            source.c.changed_at <= _as_db_datetime(until),
        ]
        if last_changed_at is not None:
            last_db = _as_db_datetime(last_changed_at)
            conditions.append(
                or_(
                    source.c.changed_at > last_db,
                    and_(source.c.changed_at == last_db, source.c.qna_id > last_id),
                    and_(
                        source.c.changed_at == last_db,
                        source.c.qna_id == last_id,
                        source.c.operation > last_operation,
                    ),
                )
            )

        statement = (
            select(source.c.qna_id, source.c.operation, source.c.changed_at)
            .where(*conditions)
            .order_by(
                source.c.changed_at.asc(),
                source.c.qna_id.asc(),
                source.c.operation.asc(),
            )
            .limit(limit + 1)
        )
        rows = execute_admin_sql(self.db, statement).mappings().all()
        has_more = len(rows) > limit
        page = rows[:limit]

        upsert_ids = [row["qna_id"] for row in page if row["operation"] == "upsert"]
        qnas = {}
        if upsert_ids:
            qnas = {
                row.id: row
                for row in self.db.query(QnA)
                .options(selectinload(QnA.tags))
                .filter(QnA.id.in_(upsert_ids), QnA.status == 1)
                .all()
            }

        items = []
        for row in page:
            qna = qnas.get(row["qna_id"])
            if row["operation"] == "upsert" and qna is not None:
                item = self._item(qna)
                items.append(UpsertChange(**item.model_dump()))
            else:
                items.append(
                    DeleteChange(
                        id=row["qna_id"],
                        updated_at=_as_utc(row["changed_at"]),
                    )
                )

        next_cursor = None
        if has_more:
            last = page[-1]
            next_cursor = _encode_cursor(
                {
                    "v": 1,
                    "kind": "changes",
                    "since": _iso_z(since_utc),
                    "until": _iso_z(until),
                    "last_changed_at": _iso_z(last["changed_at"]),
                    "last_id": last["qna_id"],
                    "last_operation": last["operation"],
                }
            )
        return ChangesResponse(
            since=since_utc,
            until=until,
            items=items,
            pagination=Pagination(next_cursor=next_cursor, has_more=has_more),
        )

    def meta(self) -> MetaResponse:
        total = self.db.query(func.count(QnA.id)).filter(QnA.status == 1).scalar() or 0
        source = self._change_source()
        latest = (
            execute_admin_sql(
                self.db,
                select(source.c.qna_id, source.c.operation, source.c.changed_at)
                .order_by(
                    source.c.changed_at.desc(),
                    source.c.qna_id.desc(),
                    source.c.operation.desc(),
                )
                .limit(1),
            )
            .mappings()
            .first()
        )
        if latest is None:
            return MetaResponse(
                dataset_version=None,
                total_active_records=total,
                last_updated_at=None,
            )
        last_updated_at = _as_utc(latest["changed_at"])
        version = f"{_iso_z(last_updated_at)}|{latest['operation']}|{latest['qna_id']}"
        return MetaResponse(
            dataset_version=version,
            total_active_records=total,
            last_updated_at=last_updated_at,
        )
