"""Saf fonksiyonlar: DB/ağ gerektirmez."""
from types import SimpleNamespace

import services.calendar_utils as calendar_utils
import services.llm_provider as llm_provider
from admin.settings_api import mask_key


class _P(llm_provider.BaseLLMProvider):
    def _complete(self, s, u, max_tokens=5): return ""
    def ask(self, q, c): return None


def test_parse_selection_accepts_noisy_numbers():
    p = _P()
    ctx = [{"answer": "A1"}, {"answer": "A2"}, {"answer": "A3"}]
    assert p._parse_selection("2", ctx) == "A2"
    assert p._parse_selection(" [3] ", ctx) == "A3"   # eskiden None dönerdi
    assert p._parse_selection("1.", ctx) == "A1"
    assert p._parse_selection("0", ctx) is None        # "uygun aday yok"
    assert p._parse_selection("7", ctx) is None        # aralık dışı
    assert p._parse_selection(None, ctx) is None
    assert p._parse_selection("cevap yok", ctx) is None


def test_regex_split_fallback():
    assert llm_provider._regex_split("") == []
    assert llm_provider._regex_split("tek soru") == ["tek soru"]
    parts = llm_provider._regex_split("vize ne zaman? final ne zaman?")
    assert parts == ["vize ne zaman", "final ne zaman"]


def test_parse_split_strips_bullets_and_dedupes():
    p = _P()
    raw = "1. Vize ne zaman?\n- Vize ne zaman?\n* Final ne zaman?"
    assert p._parse_split(raw, "orijinal") == ["Vize ne zaman?", "Final ne zaman?"]
    assert p._parse_split("", "orijinal") == ["orijinal"]


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
    assert not is_date_query("şifremi unuttum")


def test_contextual_retrieval_uses_only_last_two_user_messages():
    from services.answer_pipeline import _contextual_retrieval_query

    context = (
        {"role": "user", "content": "ilk kullanıcı mesajı"},
        {"role": "bot", "content": "uzun bot cevabı"},
        {"role": "user", "content": "ikinci kullanıcı mesajı"},
        {"role": "user", "content": "üçüncü kullanıcı mesajı"},
    )
    result = _contextual_retrieval_query("güncel mesaj", context)
    assert result == "ikinci kullanıcı mesajı\nüçüncü kullanıcı mesajı\ngüncel mesaj"
    assert "bot cevabı" not in result
    assert "ilk kullanıcı" not in result


def test_selector_question_separates_history_from_current_message():
    from services.answer_pipeline import _selector_question

    result = _selector_question(
        "Fakat şu an dört ders görünüyor",
        (
            {"role": "user", "content": "Yedi ders görünüyordu"},
            {"role": "bot", "content": "Derslerinizi OBS'den görebilirsiniz"},
        ),
    )
    assert "Önceki konuşma" in result
    assert "Öğrenci: Yedi ders görünüyordu" in result
    assert "Asistan: Derslerinizi" in result
    assert result.endswith("Fakat şu an dört ders görünüyor")


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

    first = answer_pipeline._build_candidate_pool("soru", [])
    qdrant.hits.reverse()
    second = answer_pipeline._build_candidate_pool("soru", [])

    assert first == second
    assert [candidate["qna_id"] for candidate in first] == [1, 2, 3, 4]
    assert sum(candidate["answer"] == "ortak" for candidate in first) == 2


def test_current_query_candidates_precede_contextual_candidates(monkeypatch):
    from services import answer_pipeline

    class Qdrant:
        def search(self, query, limit):
            del limit
            if "önceki" in query:
                return [{
                    "id": 1, "qna_id": 1, "question": "context",
                    "answer": "context", "score": 1.0, "source": "qdrant",
                }]
            return [{
                "id": 9, "qna_id": 9, "question": "current",
                "answer": "current", "score": 0.1, "source": "qdrant",
            }]

    monkeypatch.setattr(answer_pipeline, "QDRANT_PROVIDER", Qdrant())
    monkeypatch.setattr(answer_pipeline, "meili_search_safe", lambda _q, limit: [])

    pool = answer_pipeline._build_candidate_pool(
        "güncel", [], ({"role": "user", "content": "önceki"},)
    )

    assert [candidate["qna_id"] for candidate in pool] == [9, 1]


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

    assert [candidate["question"] for candidate in pool] == [
        "Bahar Final ne zaman?",
        "Güz Vize ne zaman?",
    ]
