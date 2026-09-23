"""Deterministic, bounded Academic Calendar V2 retrieval.

This module deliberately has no LLM dependency.  It treats the configured
academic year as the authoritative dataset boundary, applies explicit
year/term constraints, and only returns rows with event-specific lexical or
curated-alias evidence.
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from core.database import AcademicCalendar, SystemConfig


CURRENT_YEAR_CONFIG_KEY = "ACADEMIC_CALENDAR_CURRENT_YEAR"
CURRENT_TERM_CONFIG_KEY = "ACADEMIC_CALENDAR_CURRENT_TERM"
DEFAULT_CALENDAR_CANDIDATE_LIMIT = 2
MAX_CALENDAR_CANDIDATE_LIMIT = 3


class CalendarTerm(str, Enum):
    GUZ = "GUZ"
    BAHAR = "BAHAR"
    GENERAL = "GENERAL"


class CalendarNoMatchReason(str, Enum):
    NOT_RELEVANT = "not_relevant"
    NO_CURRENT_YEAR_CONFIG = "no_current_year_config"
    NO_CURRENT_TERM_CONFIG = "no_current_term_config"
    INVALID_EXPLICIT_YEAR = "invalid_explicit_year"
    EXPLICIT_HISTORICAL_YEAR = "explicit_historical_year"
    AMBIGUOUS_EXPLICIT_TERM = "ambiguous_explicit_term"
    NO_TERM_ELIGIBLE_EVENT = "no_term_eligible_event"
    NO_EVENT_MATCH = "no_event_match"
    RETRIEVAL_ERROR = "retrieval_error"


@dataclass(frozen=True)
class CalendarRuntimeConfig:
    current_academic_year: Optional[str]
    current_term: Optional[CalendarTerm]
    year_source: str
    term_source: str


@dataclass(frozen=True)
class CalendarRetrievalResult:
    candidates: tuple[AcademicCalendar | CalendarRowSnapshot, ...]
    trace_snapshot: dict


@dataclass(frozen=True)
class CalendarRowSnapshot:
    id: int
    period: str
    event: str
    start_date: str
    end_date: str
    academic_year: Optional[str]
    term: Optional[str]
    aliases: str


@dataclass(frozen=True)
class CalendarDataSnapshot:
    config: CalendarRuntimeConfig
    rows: tuple[CalendarRowSnapshot, ...]


def load_calendar_snapshot(db: Session) -> CalendarDataSnapshot:
    """Copy all fields used by calendar matching while the DB phase is open."""
    config = resolve_calendar_runtime_config(db)
    if not config.current_academic_year:
        return CalendarDataSnapshot(config, ())
    rows = db.query(AcademicCalendar).order_by(AcademicCalendar.id).all()
    return CalendarDataSnapshot(config, tuple(
        CalendarRowSnapshot(
            id=int(row.id), period=row.period, event=row.event,
            start_date=row.start_date, end_date=row.end_date,
            academic_year=row.academic_year, term=row.term, aliases=row.aliases,
        ) for row in rows
    ))


_ACADEMIC_YEAR_RE = re.compile(
    r"(?<!\d)(20\d{2})\s*[-/]\s*(20\d{2})(?!\d)|"
    r"(?<!\d)(20\d{2})\s+(20\d{2})(?!\d)"
)
_SINGLE_YEAR_RE = re.compile(r"(?<!\d)20\d{2}(?!\d)")

_GENERIC_TOKENS = {
    "acaba", "akademik", "ayin",
    "bitiyor", "donem", "gun", "gunu", "hangi", "icin", "kayit", "ne",
    "nedir", "olacak", "olur", "sinav", "sinavi", "tarih", "tarihi",
    "tarihleri", "takvim", "vakit", "zaman",
}
_TERM_TOKENS = {"guz", "bahar", "general", "genel"}
_LEXICAL_NORMALIZATION = {
    "baslangic": "basla",
    "baslangici": "basla",
    "basliyor": "basla",
    "baslayacak": "basla",
}


def normalize_calendar_text(value: str) -> str:
    """Lowercase/transliterate text into stable ASCII lexical form."""
    translated = str(value or "").translate(str.maketrans({
        "ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c",
    }))
    decomposed = unicodedata.normalize("NFKD", translated.casefold())
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(
        _LEXICAL_NORMALIZATION.get(token, token)
        for token in re.findall(r"[a-z0-9]+", ascii_text)
    )


def normalize_academic_year(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = _ACADEMIC_YEAR_RE.fullmatch(str(value).strip())
    if not match:
        return None
    first, second = (match.group(1), match.group(2)) if match.group(1) else (
        match.group(3), match.group(4)
    )
    if int(second) != int(first) + 1:
        return None
    return f"{first}-{second}"


def extract_explicit_academic_year(query: str) -> tuple[Optional[str], bool]:
    """Return (canonical year, invalid/ambiguous year evidence)."""
    raw_matches = list(_ACADEMIC_YEAR_RE.finditer(query or ""))
    if not raw_matches:
        # A lone year is not enough to identify an academic year, but ignoring
        # it could make a current-year event masquerade as a historical answer.
        return None, bool(_SINGLE_YEAR_RE.search(query or ""))
    values = set()
    invalid = False
    for match in raw_matches:
        first, second = (match.group(1), match.group(2)) if match.group(1) else (
            match.group(3), match.group(4)
        )
        if int(second) != int(first) + 1:
            invalid = True
        else:
            values.add(f"{first}-{second}")
    if invalid or len(values) != 1:
        return None, True
    return values.pop(), False


def parse_calendar_term(value: Optional[str]) -> Optional[CalendarTerm]:
    normalized = normalize_calendar_text(value or "").upper()
    aliases = {
        "GUZ": CalendarTerm.GUZ,
        "GUZ DONEMI": CalendarTerm.GUZ,
        "BAHAR": CalendarTerm.BAHAR,
        "BAHAR DONEMI": CalendarTerm.BAHAR,
        "GENERAL": CalendarTerm.GENERAL,
        "GENEL": CalendarTerm.GENERAL,
    }
    return aliases.get(normalized)


def extract_explicit_term(query: str) -> tuple[Optional[CalendarTerm], bool]:
    tokens = set(normalize_calendar_text(query).split())
    found = []
    if "guz" in tokens:
        found.append(CalendarTerm.GUZ)
    if "bahar" in tokens:
        found.append(CalendarTerm.BAHAR)
    if len(found) > 1:
        return None, True
    return (found[0] if found else None), False


def term_from_row(row: AcademicCalendar) -> CalendarTerm:
    explicit = parse_calendar_term(getattr(row, "term", None))
    if explicit is not None:
        return explicit
    period = normalize_calendar_text(getattr(row, "period", ""))
    if "guz" in period.split():
        return CalendarTerm.GUZ
    if "bahar" in period.split():
        return CalendarTerm.BAHAR
    return CalendarTerm.GENERAL


def parse_aliases(value) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip() for item in parsed if str(item).strip())


def serialize_aliases(values: Iterable[str]) -> str:
    cleaned = []
    seen = set()
    for value in values:
        item = str(value or "").strip()
        normalized = normalize_calendar_text(item)
        if not item or not normalized or normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(item)
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))


def resolve_calendar_runtime_config(db: Session) -> CalendarRuntimeConfig:
    keys = (CURRENT_YEAR_CONFIG_KEY, CURRENT_TERM_CONFIG_KEY)
    rows = db.query(SystemConfig).filter(SystemConfig.key.in_(keys)).all()
    values = {row.key: row.value for row in rows}

    year_raw = values.get(CURRENT_YEAR_CONFIG_KEY)
    year_source = "db" if year_raw else "none"
    if not year_raw:
        year_raw = os.getenv(CURRENT_YEAR_CONFIG_KEY)
        year_source = "env" if year_raw else "none"

    term_raw = values.get(CURRENT_TERM_CONFIG_KEY)
    term_source = "db" if term_raw else "none"
    if not term_raw:
        term_raw = os.getenv(CURRENT_TERM_CONFIG_KEY)
        term_source = "env" if term_raw else "none"

    return CalendarRuntimeConfig(
        current_academic_year=normalize_academic_year(year_raw),
        current_term=parse_calendar_term(term_raw),
        year_source=year_source,
        term_source=term_source,
    )


def _tokens(value: str) -> set[str]:
    return set(normalize_calendar_text(value).split())


def _specific_tokens(value: str) -> set[str]:
    return {
        token for token in _tokens(value)
        if len(token) >= 3 and token not in _GENERIC_TOKENS and token not in _TERM_TOKENS
    }


def _token_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    # Conservative inflection tolerance; short abbreviations must be exact.
    return min(len(left), len(right)) >= 5 and (
        left.startswith(right) or right.startswith(left)
    )


def _evidence_score(query: str, row: AcademicCalendar) -> tuple[int, int, int]:
    query_normalized = normalize_calendar_text(query)
    query_specific = _specific_tokens(query)
    event_specific = _specific_tokens(str(getattr(row, "event", "") or ""))

    lexical = sum(
        1 for event_token in event_specific
        if any(_token_matches(event_token, query_token) for query_token in query_specific)
    )

    alias_phrase = 0
    alias_token_hits = 0
    for alias in parse_aliases(getattr(row, "aliases", None)):
        normalized_alias = normalize_calendar_text(alias)
        alias_specific = _specific_tokens(alias)
        # Curated multiword aliases may contain a generic word (e.g. "dönem
        # kaydı"); exact phrase is still human-reviewed event evidence.
        if normalized_alias and re.search(
            rf"(?:^|\s){re.escape(normalized_alias)}(?:$|\s)", query_normalized
        ):
            if len(normalized_alias.split()) > 1 or normalized_alias not in _GENERIC_TOKENS:
                alias_phrase = max(alias_phrase, 2)
        alias_token_hits = max(
            alias_token_hits,
            sum(
                1 for alias_token in alias_specific
                if any(_token_matches(alias_token, query_token) for query_token in query_specific)
            ),
        )
    # First component controls eligibility; other components give stable ranking.
    evidence = max(lexical, alias_phrase, alias_token_hits)
    return evidence, alias_phrase, lexical


def _term_preference(term: CalendarTerm, current: CalendarTerm) -> int:
    if term == current:
        return 2
    if term == CalendarTerm.GENERAL:
        return 1
    return 0


def skipped_calendar_result(*, relevant: bool = False) -> CalendarRetrievalResult:
    return CalendarRetrievalResult(
        candidates=(),
        trace_snapshot={
            "calendar_relevant": relevant,
            "calendar_route_opened": False,
            "calendar_current_year": None,
            "calendar_current_term": None,
            "explicit_query_year": None,
            "explicit_query_term": None,
            "calendar_total_rows": 0,
            "calendar_year_eligible_count": 0,
            "calendar_term_eligible_count": 0,
            "calendar_event_match_count": 0,
            "calendar_candidates_returned": 0,
            "calendar_candidate_ids": [],
            "calendar_candidate_terms": [],
            "calendar_candidate_event_names": [],
            "calendar_no_match_reason": CalendarNoMatchReason.NOT_RELEVANT.value,
            "historical_year_rejected": False,
            "legacy_year_assumed_current_count": 0,
            "calendar_retrieval_latency_ms": 0.0,
        },
    )


def failed_calendar_result() -> CalendarRetrievalResult:
    snapshot = dict(skipped_calendar_result(relevant=True).trace_snapshot)
    snapshot.update({
        "calendar_route_opened": True,
        "calendar_no_match_reason": CalendarNoMatchReason.RETRIEVAL_ERROR.value,
        "calendar_retrieval_latency_ms": None,
    })
    return CalendarRetrievalResult(candidates=(), trace_snapshot=snapshot)


def retrieve_calendar_candidates(
    resolved_intent: str,
    db: Session | CalendarDataSnapshot,
    *,
    limit: int = DEFAULT_CALENDAR_CANDIDATE_LIMIT,
) -> CalendarRetrievalResult:
    """Retrieve small, deterministic current-calendar candidate set."""
    started = time.perf_counter()
    limit = max(1, min(int(limit), MAX_CALENDAR_CANDIDATE_LIMIT))
    snapshot_data = db if isinstance(db, CalendarDataSnapshot) else None
    config = snapshot_data.config if snapshot_data is not None else resolve_calendar_runtime_config(db)
    explicit_year, invalid_year = extract_explicit_academic_year(resolved_intent)
    explicit_term, ambiguous_term = extract_explicit_term(resolved_intent)

    snapshot = {
        "calendar_relevant": True,
        "calendar_route_opened": True,
        "calendar_current_year": config.current_academic_year,
        "calendar_current_term": config.current_term.value if config.current_term else None,
        "calendar_config_year_source": config.year_source,
        "calendar_config_term_source": config.term_source,
        "explicit_query_year": explicit_year,
        "explicit_query_term": explicit_term.value if explicit_term else None,
        "calendar_total_rows": 0,
        "calendar_year_eligible_count": 0,
        "calendar_term_eligible_count": 0,
        "calendar_event_match_count": 0,
        "calendar_candidates_returned": 0,
        "calendar_candidate_ids": [],
        "calendar_candidate_terms": [],
        "calendar_candidate_event_names": [],
        "calendar_no_match_reason": None,
        "historical_year_rejected": False,
        "legacy_year_assumed_current_count": 0,
        "calendar_retrieval_latency_ms": None,
    }

    def finish(reason: Optional[CalendarNoMatchReason], rows=()):
        chosen = tuple(rows)
        snapshot["calendar_no_match_reason"] = reason.value if reason else None
        snapshot["calendar_candidates_returned"] = len(chosen)
        snapshot["calendar_candidate_ids"] = [getattr(row, "id", None) for row in chosen]
        snapshot["calendar_candidate_terms"] = [term_from_row(row).value for row in chosen]
        snapshot["calendar_candidate_event_names"] = [str(row.event) for row in chosen]
        snapshot["calendar_retrieval_latency_ms"] = round(
            (time.perf_counter() - started) * 1000, 3
        )
        return CalendarRetrievalResult(chosen, snapshot)

    if not config.current_academic_year:
        return finish(CalendarNoMatchReason.NO_CURRENT_YEAR_CONFIG)
    if invalid_year:
        return finish(CalendarNoMatchReason.INVALID_EXPLICIT_YEAR)
    if explicit_year and explicit_year != config.current_academic_year:
        snapshot["historical_year_rejected"] = True
        return finish(CalendarNoMatchReason.EXPLICIT_HISTORICAL_YEAR)
    if ambiguous_term:
        return finish(CalendarNoMatchReason.AMBIGUOUS_EXPLICIT_TERM)
    if explicit_term is None and config.current_term is None:
        return finish(CalendarNoMatchReason.NO_CURRENT_TERM_CONFIG)

    rows = (snapshot_data.rows if snapshot_data is not None
            else db.query(AcademicCalendar).order_by(AcademicCalendar.id).all())
    snapshot["calendar_total_rows"] = len(rows)
    year_eligible = [
        row for row in rows
        if not getattr(row, "academic_year", None)
        or normalize_academic_year(row.academic_year) == config.current_academic_year
    ]
    snapshot["legacy_year_assumed_current_count"] = sum(
        1 for row in year_eligible if not getattr(row, "academic_year", None)
    )
    snapshot["calendar_year_eligible_count"] = len(year_eligible)

    if explicit_term is not None:
        term_eligible = [
            row for row in year_eligible
            if term_from_row(row) in (explicit_term, CalendarTerm.GENERAL)
        ]
    else:
        term_eligible = year_eligible
    snapshot["calendar_term_eligible_count"] = len(term_eligible)
    if not term_eligible:
        return finish(CalendarNoMatchReason.NO_TERM_ELIGIBLE_EVENT)

    scored = []
    current_term = explicit_term or config.current_term or CalendarTerm.GENERAL
    for row in term_eligible:
        evidence, alias_phrase, lexical = _evidence_score(resolved_intent, row)
        if evidence <= 0:
            continue
        scored.append((
            row,
            evidence,
            alias_phrase,
            lexical,
            _term_preference(term_from_row(row), current_term),
        ))
    snapshot["calendar_event_match_count"] = len(scored)
    if not scored:
        return finish(CalendarNoMatchReason.NO_EVENT_MATCH)

    scored.sort(key=lambda item: (
        -item[1], -item[2], -item[3], -item[4],
        int(getattr(item[0], "id", 0) or 0),
        normalize_calendar_text(str(item[0].event)),
        str(item[0].start_date or ""),
    ))
    return finish(None, [item[0] for item in scored[:limit]])
