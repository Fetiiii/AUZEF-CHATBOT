"""Saf fonksiyonlar: DB/ağ gerektirmez."""
from types import SimpleNamespace

import services.calendar_utils as calendar_utils
from admin.settings_api import mask_key


def test_calendar_matching():
    class E:
        def __init__(self, period, event):
            self.period, self.event = period, event

    entries = [E("Güz", "Bütünleme Sınavı"), E("Bahar", "Final Sınavı")]
    best = calendar_utils.match_calendar_entry("bütünleme ne zaman", entries)
    assert best is not None and best.event == "Bütünleme Sınavı"
    # Takvimle ilgisiz "tarih sorusu" hiçbir kayda eşlenmemeli
    assert calendar_utils.match_calendar_entry("hava ne zaman güzel olur", entries) is None


def test_format_calendar_answer_single_vs_range():
    assert "tarihindedir" in calendar_utils.format_calendar_answer("Güz", "Vize", "01.11.2025", "01.11.2025")
    assert "arasındadır" in calendar_utils.format_calendar_answer("Güz", "Vize", "01.11.2025", "02.11.2025")


def test_mask_key_never_leaks_middle():
    assert mask_key(None) is None
    assert mask_key("kisa") == "••••"
    masked = mask_key("sk-or-v1-0123456789abcdefXYZ9")
    assert masked.startswith("sk-or-v1-") and masked.endswith("XYZ9")
    assert "0123456789abcdef" not in masked


def test_csv_safe_neutralizes_formulas():
    from services.csv_utils import csv_safe as _csv_safe
    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_safe("+SUM(A1)") == "'+SUM(A1)"
    assert _csv_safe("normal mesaj") == "normal mesaj"
    assert _csv_safe(None) == ""


def test_is_date_query():
    from services.answer_pipeline import is_date_query
    assert is_date_query("final sınavı ne zaman")
    assert is_date_query("bütünleme tarihi")
    assert not is_date_query("kayıt yenileme nasıl yapılır")
    assert not is_date_query("şifremi unuttum")


def test_intent_analyzer_context_uses_only_last_two_user_messages():
    from services.answer_pipeline import _previous_user_turns

    context = (
        {"role": "user", "content": "ilk kullanıcı mesajı"},
        {"role": "bot", "content": "uzun bot cevabı"},
        {"role": "user", "content": "ikinci kullanıcı mesajı"},
        {"role": "user", "content": "üçüncü kullanıcı mesajı"},
    )
    result = _previous_user_turns(context)
    assert result == ("ikinci kullanıcı mesajı", "üçüncü kullanıcı mesajı")


def test_candidate_pool_deduplicates_by_qna_id_and_has_stable_order(monkeypatch):
    from services import answer_pipeline

    class Qdrant:
        hits = [
            {
                "id": 2002, "qna_id": 2, "question": "q2",
                "answer": "ortak", "score": 0.900004, "source": "qdrant",
            },
            {
                "id": 1001, "qna_id": "1", "question": "q1",
                "answer": "a1", "score": 0.95, "source": "qdrant",
            },
            {
                "id": 1002, "qna_id": 1, "question": "q1",
                "answer": "a1", "score": 0.94, "source": "qdrant",
            },
            {
                "id": 3001, "qna_id": 3, "question": "q3",
                "answer": "ortak", "score": 0.900003, "source": "qdrant",
            },
        ]

        def search(self, _query, limit):
            return self.hits[:limit]

    meili_hits = [
        {
            "id": 1, "qna_id": 1, "question": "q1", "answer": "a1",
            "score": 0.99, "source": "meilisearch",
        },
        {
            "id": 4, "qna_id": 4, "question": "q4", "answer": "a4",
            "score": 0.8, "source": "meilisearch",
        },
    ]
    qdrant = Qdrant()
    monkeypatch.setattr(answer_pipeline, "QDRANT_PROVIDER", qdrant)
    monkeypatch.setattr(
        answer_pipeline,
        "meili_search_safe",
        lambda _query, limit: meili_hits[:limit],
    )

    def active(ids):
        return set(ids)

    first = answer_pipeline._build_candidate_pool("soru", [], active_qna_lookup=active)
    qdrant.hits.reverse()
    second = answer_pipeline._build_candidate_pool("soru", [], active_qna_lookup=active)

    assert first == second
    assert [candidate.qna_id for candidate in first] == [1, 2, 3, 4]
    assert [candidate.candidate_ref for candidate in first] == [
        "qna:1", "qna:2", "qna:3", "qna:4"
    ]
    assert sum(candidate.answer_text == "ortak" for candidate in first) == 2


def test_calendar_candidates_are_sorted_and_deduplicated(monkeypatch):
    from services import answer_pipeline

    monkeypatch.setattr(
        answer_pipeline.QDRANT_PROVIDER, "search", lambda _query, limit: []
    )
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])
    bahar = SimpleNamespace(
        id=2, period="Bahar", event="Final", start_date="02.01", end_date="02.01"
    )
    guz = SimpleNamespace(
        id=1, period="Güz", event="Vize", start_date="01.01", end_date="01.01"
    )
    duplicate = SimpleNamespace(
        id=3, period="Bahar", event="Final", start_date="02.01", end_date="02.01"
    )

    pool = answer_pipeline._build_candidate_pool("soru", [guz, duplicate, bahar])

    assert [candidate.canonical_text for candidate in pool] == [
        "Bahar Final",
        "Güz Vize",
    ]
    assert [candidate.candidate_ref for candidate in pool] == [
        "calendar:2",
        "calendar:1",
    ]
