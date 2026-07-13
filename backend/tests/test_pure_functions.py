"""Saf fonksiyonlar: DB/ağ gerektirmez."""
import calendar_utils
import llm_provider
from settings_api import mask_key


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
    from main import _csv_safe
    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_safe("+SUM(A1)") == "'+SUM(A1)"
    assert _csv_safe("normal mesaj") == "normal mesaj"
    assert _csv_safe(None) == ""


def test_is_date_query():
    from main import is_date_query
    assert is_date_query("final sınavı ne zaman")
    assert is_date_query("bütünleme tarihi")
    assert not is_date_query("şifremi unuttum")
