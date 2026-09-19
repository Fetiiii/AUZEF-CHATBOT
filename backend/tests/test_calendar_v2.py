"""Phase 3: routed, deterministic and bounded Academic Calendar retrieval."""
from __future__ import annotations

import json

from sqlalchemy import inspect

from core.database import AcademicCalendar, SystemConfig, admin_engine
from services.calendar_utils import format_calendar_answer
from services.calendar_retrieval import (
    CURRENT_TERM_CONFIG_KEY,
    CURRENT_YEAR_CONFIG_KEY,
    CalendarNoMatchReason,
    CalendarTerm,
    extract_explicit_academic_year,
    extract_explicit_term,
    retrieve_calendar_candidates,
    resolve_calendar_runtime_config,
    serialize_aliases,
)
from services.decision_trace import DecisionTrace
from services.llm_config import resolve_llm_config_set
from services.llm_types import (
    IntentAnalysis,
    IntentAnalyzerResult,
    IntentItem,
    LLMOutcomeStatus,
    LLMParseStatus,
    SelectorResult,
)


def _configure(db, *, year="2026-2027", term="BAHAR"):
    db.add_all([
        SystemConfig(key=CURRENT_YEAR_CONFIG_KEY, value=year),
        SystemConfig(key=CURRENT_TERM_CONFIG_KEY, value=term),
    ])
    db.commit()


def _calendar(
    event,
    *,
    term="BAHAR",
    year="2026-2027",
    period=None,
    aliases=(),
    start="01.02.2027",
):
    return AcademicCalendar(
        period=period or term,
        event=event,
        start_date=start,
        end_date=start,
        academic_year=year,
        term=term,
        aliases=serialize_aliases(aliases),
    )


def test_explicit_year_parser_is_conservative():
    assert extract_explicit_academic_year("2026-2027 bütünleme") == ("2026-2027", False)
    assert extract_explicit_academic_year("2026/2027 bütünleme") == ("2026-2027", False)
    assert extract_explicit_academic_year("2026 2027 bütünleme") == ("2026-2027", False)
    assert extract_explicit_academic_year("bütünleme ne zaman") == (None, False)
    assert extract_explicit_academic_year("2026-2028 bütünleme") == (None, True)
    assert extract_explicit_academic_year("2025 bütünleme") == (None, True)


def test_explicit_term_parser_handles_turkish_variants_and_ambiguity():
    assert extract_explicit_term("Güz bütünleme") == (CalendarTerm.GUZ, False)
    assert extract_explicit_term("guz bütünleme") == (CalendarTerm.GUZ, False)
    assert extract_explicit_term("Bahar bütünleme") == (CalendarTerm.BAHAR, False)
    assert extract_explicit_term("Güz veya bahar bütünleme") == (None, True)


def test_current_term_is_preferred_but_explicit_term_overrides(db):
    _configure(db, term="BAHAR")
    db.add_all([
        _calendar("Bütünleme Sınavları", term="GUZ", start="10.01.2027"),
        _calendar("Bütünleme Sınavları", term="BAHAR", start="10.06.2027"),
    ])
    db.commit()

    implicit = retrieve_calendar_candidates("Bütünleme ne zaman?", db)
    assert implicit.candidates[0].term == "BAHAR"
    assert len(implicit.candidates) == 2

    explicit_guz = retrieve_calendar_candidates("Güz bütünleme ne zaman?", db)
    assert [row.term for row in explicit_guz.candidates] == ["GUZ"]
    explicit_bahar = retrieve_calendar_candidates("Bahar bütünleme ne zaman?", db)
    assert [row.term for row in explicit_bahar.candidates] == ["BAHAR"]


def test_general_is_eligible_but_unrelated_general_event_is_not(db):
    _configure(db, term="BAHAR")
    db.add_all([
        _calendar("Kayıt Yenileme", term="GENERAL"),
        _calendar("Mezuniyet Töreni", term="GENERAL"),
    ])
    db.commit()
    result = retrieve_calendar_candidates("Güz kayıt yenileme ne zaman?", db)
    assert [row.event for row in result.candidates] == ["Kayıt Yenileme"]


def test_alias_match_and_no_random_nearest_event(db):
    _configure(db)
    db.add_all([
        _calendar("Bütünleme Sınavları", aliases=("büt",)),
        _calendar("Kayıt Yenileme", aliases=("dönem kaydı",)),
    ])
    db.commit()
    alias = retrieve_calendar_candidates("Büt ne zaman?", db)
    assert [row.event for row in alias.candidates] == ["Bütünleme Sınavları"]

    none = retrieve_calendar_candidates("Öğrenci kulüplerinin toplantısı ne zaman?", db)
    assert none.candidates == ()
    assert none.trace_snapshot["calendar_no_match_reason"] == CalendarNoMatchReason.NO_EVENT_MATCH.value


def test_generic_date_words_are_not_event_evidence(db):
    _configure(db)
    db.add(_calendar("Bütünleme Sınavları"))
    db.commit()
    result = retrieve_calendar_candidates("Sınav hangi tarihte?", db)
    assert result.candidates == ()


def test_semester_start_wording_matches_start_event_without_keyword_flood(db):
    _configure(db)
    db.add_all([
        _calendar("Eğitim Öğretim Başlangıcı", term="BAHAR"),
        _calendar("Bütünleme Sınavları", term="BAHAR"),
    ])
    db.commit()
    result = retrieve_calendar_candidates("Bahar dönemi ne zaman başlıyor?", db)
    assert [row.event for row in result.candidates] == ["Eğitim Öğretim Başlangıcı"]


def test_historical_year_is_rejected_before_calendar_rows_are_used(db):
    _configure(db, year="2026-2027")
    db.add(_calendar("Bütünleme Sınavları"))
    db.commit()
    result = retrieve_calendar_candidates("2025-2026 bütünleme ne zamandı?", db)
    assert result.candidates == ()
    assert result.trace_snapshot["historical_year_rejected"] is True
    assert result.trace_snapshot["calendar_total_rows"] == 0
    assert result.trace_snapshot["calendar_no_match_reason"] == "explicit_historical_year"

    ambiguous = retrieve_calendar_candidates("2025 bütünleme ne zamandı?", db)
    assert ambiguous.candidates == ()
    assert ambiguous.trace_snapshot["calendar_no_match_reason"] == "invalid_explicit_year"


def test_explicit_term_can_route_without_current_term_config(db, monkeypatch):
    monkeypatch.delenv(CURRENT_TERM_CONFIG_KEY, raising=False)
    db.add(SystemConfig(key=CURRENT_YEAR_CONFIG_KEY, value="2026-2027"))
    db.add(_calendar("Bütünleme Sınavları", term="GUZ"))
    db.commit()
    result = retrieve_calendar_candidates("Güz bütünleme ne zaman?", db)
    assert [row.term for row in result.candidates] == ["GUZ"]


def test_missing_config_fails_closed_without_guessing(db, monkeypatch):
    monkeypatch.delenv(CURRENT_YEAR_CONFIG_KEY, raising=False)
    monkeypatch.delenv(CURRENT_TERM_CONFIG_KEY, raising=False)
    db.add(_calendar("Bütünleme Sınavları"))
    db.commit()
    result = retrieve_calendar_candidates("Bütünleme ne zaman?", db)
    assert result.candidates == ()
    assert result.trace_snapshot["calendar_no_match_reason"] == "no_current_year_config"


def test_calendar_config_uses_db_then_environment_fallback(db, monkeypatch):
    monkeypatch.setenv(CURRENT_YEAR_CONFIG_KEY, "2025-2026")
    monkeypatch.setenv(CURRENT_TERM_CONFIG_KEY, "GUZ")
    env_config = resolve_calendar_runtime_config(db)
    assert (env_config.current_academic_year, env_config.current_term) == (
        "2025-2026", CalendarTerm.GUZ
    )
    assert (env_config.year_source, env_config.term_source) == ("env", "env")

    _configure(db, year="2026-2027", term="BAHAR")
    db_config = resolve_calendar_runtime_config(db)
    assert (db_config.current_academic_year, db_config.current_term) == (
        "2026-2027", CalendarTerm.BAHAR
    )
    assert (db_config.year_source, db_config.term_source) == ("db", "db")


def test_legacy_null_year_remains_current_dataset_visible_and_traced(db):
    _configure(db)
    db.add(_calendar("Kayıt Yenileme", year=None))
    db.commit()
    result = retrieve_calendar_candidates("Kayıt yenileme ne zaman?", db)
    assert len(result.candidates) == 1
    assert result.trace_snapshot["legacy_year_assumed_current_count"] == 1


def test_candidate_limit_and_order_are_stable(db):
    _configure(db)
    db.add_all([
        _calendar("Bütünleme A", aliases=("büt",)),
        _calendar("Bütünleme B", aliases=("büt",)),
        _calendar("Bütünleme C", aliases=("büt",)),
    ])
    db.commit()
    first = retrieve_calendar_candidates("Büt ne zaman?", db)
    second = retrieve_calendar_candidates("Büt ne zaman?", db)
    assert len(first.candidates) == 2
    assert [row.id for row in first.candidates] == [row.id for row in second.candidates]
    assert first.trace_snapshot["calendar_event_match_count"] == 3


def test_calendar_candidate_flood_evaluation_matrix(db):
    """Controlled fixture for the Phase 3 candidate-count report."""
    _configure(db, term="BAHAR")
    db.add_all([
        _calendar("Bütünleme Sınavları", term="GUZ", aliases=("büt",)),
        _calendar("Bütünleme Sınavları", term="BAHAR", aliases=("büt",)),
        _calendar("Kayıt Yenileme", term="GENERAL"),
        _calendar("Eğitim Öğretim Başlangıcı", term="BAHAR"),
    ])
    db.commit()
    queries = (
        "Bütünleme ne zaman?",
        "Güz bütünleme ne zaman?",
        "Bahar bütünleme ne zaman?",
        "Kayıt yenileme ne zaman?",
        "Büt ne zaman?",
        "Öğrenci kulüplerinin toplantısı ne zaman?",
        "2025-2026 bütünleme ne zamandı?",
        "Bahar dönemi ne zaman başlıyor?",
    )
    counts = [len(retrieve_calendar_candidates(query, db).candidates) for query in queries]
    assert counts == [2, 1, 1, 1, 2, 0, 0, 1]
    assert sum(counts) / len(counts) == 1.0
    assert max(counts) == 2


class _Provider:
    def __init__(self, analysis):
        self.analysis = analysis
        self.configs = resolve_llm_config_set("openai", environ={})
        self.selector_candidates = []

    def effective_config(self, capability):
        return self.configs.for_capability(capability)

    def analyze_intents_with_result(self, _current, _previous=()):
        return IntentAnalyzerResult(analysis=self.analysis)

    def ask_with_result(self, _question, candidates):
        self.selector_candidates.append(candidates)
        selected = candidates[0]
        return SelectorResult(
            status=LLMOutcomeStatus.SUCCESS,
            parse_status=LLMParseStatus.SUCCESS,
            answer=selected.answer_text,
            decision="SELECT",
            selected_candidate_ref=selected.candidate_ref,
            selected_kind=selected.kind.value,
            selected_qna_id=selected.qna_id,
            selected_calendar_id=selected.calendar_id,
        )


def _intent(text, calendar):
    return IntentItem(
        source_text=text,
        normalized_text=text,
        resolved_text=text,
        context_used=False,
        calendar_relevant=calendar,
    )


def _qna_build(query, calendar_entries, policy=None, **_kwargs):
    from services.candidate_eligibility import build_candidate_set

    return build_candidate_set(
        calendar_entries=calendar_entries,
        qna_hits=[{
            "qna_id": 99, "question": query, "answer": "qna-answer",
            "score": 1.0, "source": "qdrant",
        }],
        routing_policy=policy,
        active_qna_lookup=lambda ids: set(ids),
        max_candidates=32,
        qdrant_candidate_count=1,
    )


def test_calendar_false_skips_retrieval_and_qna_still_runs(db, monkeypatch):
    from services import answer_pipeline

    analysis = IntentAnalysis(intent_count=1, intents=[_intent("Kayıt nasıl yapılır?", False)])
    provider = _Provider(analysis)
    monkeypatch.setattr(answer_pipeline, "get_llm_provider", lambda _db: provider)
    monkeypatch.setattr(answer_pipeline, "_build_candidate_pool_result", _qna_build)
    monkeypatch.setattr(
        answer_pipeline,
        "retrieve_calendar_candidates",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("Calendar route opened")),
    )
    trace = DecisionTrace(endpoint="test")
    result = answer_pipeline._llm_answer("Kayıt nasıl yapılır?", db, trace=trace)
    assert result.answer == "qna-answer"
    assert provider.selector_candidates[0][0].qna_id == 99
    route = trace.to_dict()["calendar_routes"][0]
    assert route["calendar_route_opened"] is False
    assert route["calendar_candidates_returned"] == 0


def test_calendar_true_coexists_with_qna_and_is_traced(db, monkeypatch):
    from services import answer_pipeline

    _configure(db)
    row = _calendar("Kayıt Yenileme")
    db.add(row)
    db.commit()
    analysis = IntentAnalysis(intent_count=1, intents=[_intent("Kayıt yenileme ne zaman?", True)])
    provider = _Provider(analysis)
    monkeypatch.setattr(answer_pipeline, "get_llm_provider", lambda _db: provider)
    monkeypatch.setattr(answer_pipeline, "_build_candidate_pool_result", _qna_build)
    trace = DecisionTrace(endpoint="test")
    result = answer_pipeline._llm_answer("Kayıt yenileme ne zaman?", db, trace=trace)
    # Calendar SELECT returns the deterministic stored-row answer.
    assert result.answer == format_calendar_answer(
        row.period, row.event, row.start_date, row.end_date
    )
    assert any(candidate.qna_id == 99 for candidate in provider.selector_candidates[0])
    route = trace.to_dict()["calendar_routes"][0]
    assert route["calendar_route_opened"] is True
    assert route["calendar_candidate_ids"] == [row.id]
    assert "Kayıt yenileme ne zaman?" not in json.dumps(trace.to_dict(), ensure_ascii=False)


def test_procedure_plus_date_routes_each_intent_independently(db, monkeypatch):
    from services import answer_pipeline

    _configure(db)
    db.add(_calendar("Kayıt Yenileme"))
    db.commit()
    analysis = IntentAnalysis(
        intent_count=2,
        intents=[
            _intent("Kayıt yenileme nasıl yapılır?", False),
            _intent("Kayıt yenileme ne zaman?", True),
        ],
    )
    provider = _Provider(analysis)
    monkeypatch.setattr(answer_pipeline, "get_llm_provider", lambda _db: provider)
    monkeypatch.setattr(answer_pipeline, "_build_candidate_pool_result", _qna_build)
    trace = DecisionTrace(endpoint="test")
    answer_pipeline._llm_answer(
        "Kayıt yenileme nasıl yapılır ve ne zaman?", db, trace=trace
    )
    routes = trace.to_dict()["calendar_routes"]
    assert [route["calendar_route_opened"] for route in routes] == [False, True]
    assert [len(call) for call in provider.selector_candidates] == [1, 2]


def test_llm_off_uses_deterministic_calendar_then_preserves_fallback_order(db, monkeypatch):
    from services import answer_pipeline

    _configure(db)
    db.add(_calendar("Bütünleme Sınavları", aliases=("büt",)))
    db.commit()
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda *_a, **_k: [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda *_a, **_k: [])
    trace = DecisionTrace(endpoint="test")
    answer, source = answer_pipeline._fallback_answer(
        "Büt ne zaman?", db, trace=trace, fallback_reason="llm_disabled"
    )
    assert source == "academic_calendar"
    assert "Bütünleme" in answer
    assert trace.to_dict()["calendar_routes"][0]["purpose"] == "degraded_request"


def test_llm_off_procedure_query_does_not_open_calendar(db, monkeypatch):
    from services import answer_pipeline

    monkeypatch.setattr(answer_pipeline, "search_calendar", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("procedure query opened Calendar")
    ))
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda *_a, **_k: [])
    monkeypatch.setattr(answer_pipeline.QDRANT_PROVIDER, "search", lambda *_a, **_k: [])
    assert answer_pipeline._fallback_answer("Kayıt yenileme nasıl yapılır?", db) == (None, "none")


def test_calendar_settings_api_and_crud_keep_new_fields_maintainable(make_user, login):
    make_user("super@iu.tr", role="super_admin")
    client = login("super@iu.tr")
    settings = client.put("/api/settings/calendar", json={
        "current_academic_year": "2026-2027",
        "current_term": "BAHAR",
    })
    assert settings.status_code == 200
    assert settings.json()["academic_year_source"] == "db"

    created = client.post("/api/academic-calendar", json={
        "period": "Bahar Dönemi",
        "event": "Bütünleme Sınavları",
        "start_date": "01.06.2027",
        "end_date": "02.06.2027",
        "aliases": ["büt", "bütünleme"],
    })
    assert created.status_code == 201
    body = created.json()
    assert body["academic_year"] == "2026-2027"
    assert body["term"] == "BAHAR"
    assert body["aliases"] == ["büt", "bütünleme"]


def test_calendar_v2_migration_is_reversible(db):
    from scripts.migrate_calendar_v2 import apply_calendar_v2_migration

    apply_calendar_v2_migration("downgrade")
    columns = {column["name"] for column in inspect(admin_engine).get_columns("academic_calendar")}
    assert not {"academic_year", "term", "aliases"} & columns
    apply_calendar_v2_migration("upgrade")
    columns = {column["name"] for column in inspect(admin_engine).get_columns("academic_calendar")}
    assert {"academic_year", "term", "aliases"} <= columns
