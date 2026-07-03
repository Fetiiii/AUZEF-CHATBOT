"""Akademik takvim yardımcıları.

LLM'siz (yedek) eşleştirme mantığı burada, saf Python olarak durur; böylece
harici bir servis olmadan test edilebilir. ``search_calendar`` LLM çağrısı
başarısız olduğunda (ör. sağlayıcı hatası) bu yedeğe düşer.
"""
import re

# Sorunun tarih sorma kalıbından gelen, eşleştirmede ayırt edici olmayan kelimeler.
# Bunlar dışlanmazsa üretilen adayların hepsinde geçen "zaman" gibi kelimeler
# her kayda eşleşip yanlış (ilk) kaydı döndürür.
CALENDAR_STOPWORDS = {
    "ne", "zaman", "vakit", "hangi", "tarih", "tarihi", "tarihleri", "tarihler",
    "gun", "gün", "günü", "gunu", "kaci", "kaçı", "kacinda", "kaçında",
    "kacinca", "kaçınca", "ayin", "ayın", "acaba", "nedir", "olur", "hakkinda",
    "hakkında", "icin", "için", "ile", "olan",
}


def calendar_tokens(text_value: str) -> set:
    """Metni, eşleştirmede kullanılacak anlamlı kelime kümesine çevirir."""
    return {
        w for w in re.findall(r"\w+", (text_value or "").lower())
        if len(w) > 2 and w not in CALENDAR_STOPWORDS
    }


def format_calendar_answer(period: str, event: str, start_date: str, end_date: str) -> str:
    """Bir takvim kaydını kullanıcıya gösterilecek cümleye çevirir."""
    if start_date == end_date:
        return f"{period} — {event}: {start_date} tarihindedir."
    return f"{period} — {event}: {start_date} – {end_date} tarihleri arasındadır."


def match_calendar_entry(query: str, entries):
    """Soruya en çok kelime örtüşen takvim kaydını döndürür.

    ``entries``: ``period``, ``event`` alanlarına sahip nesneler (ORM satırları
    ya da test için basit nesneler). Anlamlı örtüşme yoksa ``None`` döner —
    böylece tarih sorusu gibi görünen ama takvimle ilgisi olmayan sorgular
    yanlış bir kayda eşlenmez, akış normal aramaya devam eder.
    """
    q_tokens = calendar_tokens(query)
    if not q_tokens:
        return None

    best = None
    best_score = 0
    for e in entries:
        score = len(q_tokens & calendar_tokens(f"{e.period} {e.event}"))
        if score > best_score:
            best_score = score
            best = e
    return best if best_score > 0 else None
