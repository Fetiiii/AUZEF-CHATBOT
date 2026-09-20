"""V2 Intent Analyzer prompt, strict parser, and fail-safe construction."""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from pydantic import ValidationError

from services.llm_types import IntentAnalysis, IntentItem


MAX_PREVIOUS_USER_TURNS = 2


def safe_single_intent(current_user_turn: str) -> IntentAnalysis:
    """Return the lossless fallback used for every analyzer/parse failure."""
    current = current_user_turn.strip()
    if not current:
        raise ValueError("current user turn must not be empty")
    return IntentAnalysis(
        intent_count=1,
        intents=[
            IntentItem(
                source_text=current,
                normalized_text=current,
                resolved_text=current,
                context_used=False,
                calendar_relevant=False,
            )
        ],
    )


def build_intent_analyzer_prompt(
    current_user_turn: str,
    previous_user_turns: list[str] | tuple[str, ...],
) -> tuple[str, str]:
    """Build a task-only prompt. Assistant turns are not accepted as input."""
    previous = [text.strip() for text in previous_user_turns if text.strip()][
        -MAX_PREVIOUS_USER_TURNS:
    ]
    system = (
        "Sen AUZEF Intent Analyzer'sın. Cevap, QnA veya takvim kaydı seçmezsin; "
        "yalnız current user turn'deki bağımsız cevap ihtiyaçlarını analiz edersin. "
        "Current turn ana gerçekliktir. "
        "source_text current turn'deki gerçek segment olmalıdır. normalized_text yalnız "
        "yazım, noktalama ve basit dilbilgisi düzeltmesi yapabilir. "
        "Kullanıcıda veya previous user "
        "turns'de bulunmayan kurum, kanal, işlem, qualifier ya da semantik bilgi EKLEME. "
        "calendar_relevant yalnız doğru cevap AUZEF Academic Calendar tarih/dönem/event "
        "bilgisine ihtiyaç duyuyorsa true olur; salt kayıt/sınav/dönem kelimesi yetmez. "
        "Yalnız strict JSON object döndür; "
        "markdown, açıklama veya ek alan kullanma. "
        "intent_count bir JSON integer olmalıdır: 1 ya da 2. Tırnak içinde string "
        "gönderme (\"1\" YANLIŞ, 1 DOĞRU). intent_count her zaman intents "
        "dizisinin uzunluğuna eşittir: SINGLE için 1, MULTI için 2. "
        "context_used ve calendar_relevant JSON boolean olmalıdır (true/false).\n"
        # ── MULTI: decided by goal count, never by whether text changed ──────
        "INTENT SAYISI KURALI: intent_count YALNIZ current turn'de kaç tane "
        "birbirinden bağımsız, ayrı cevap gerektiren hedef bulunduğuna göre "
        "belirlenir. normalized_text ya da resolved_text'in değişip değişmediğiyle "
        "HİÇBİR İLGİSİ YOKTUR; metin hiç değişmese de intent_count 2 olabilir. "
        "Birbirinden bağımsız iki hedef varsa (A hedefi + A ile ilgisiz B hedefi) "
        "ve her biri kendi cevabını gerektiriyorsa intent_count = 2 olur. "
        "Sentetik örnek: 'Kütüphane saatleri nedir ve spor salonu nerede?' → "
        "intent_count 2. "
        "Şunlar SINGLE kalır: aynı hedefin iki kez ifade edilmesi; tek hedef + "
        "niteleyici; tek hedefin birden çok ayrıntısı; bağlaçla birleştirilmiş tek "
        "istek; ikinci hedefin varlığı belirsizse. Belirsizlikte SINGLE üret. "
        "Current turn 3 veya daha fazla bağımsız hedef içeriyorsa hiçbirini keyfi "
        "atmadan current turn'ü tek intent olarak koru; en fazla 2 intent üretilebilir.\n"
        # ── Context: explicit decision order; verbatim rule scoped to false ──
        "CONTEXT KARAR SIRASI: "
        "1) Current user message tek başına güvenilir biçimde anlaşılıyor mu? "
        "2) Anlaşılıyorsa context_used = false ve resolved_text = normalized_text "
        "BİREBİR AYNI olmalıdır: normalized_text'i aynen kopyala; yeniden ifade "
        "etme, özetleme, soruyu cümleye çevirme ya da 'bilgi almak istiyorsunuz' "
        "gibi açıklama üretme. "
        "3) Current message zamir, eksilti, örtük özne veya önceki turn'e gönderme "
        "yüzünden tek başına anlaşılamıyorsa VE previous_user_turns bunu çözüyorsa "
        "context_used = true olur. "
        "4) Bu durumda resolved_text, önceki USER turn'den YALNIZ gerekli bilgiyi "
        "ekleyerek current intent'i self-contained hale getirir ve normalized_text'ten "
        "farklı olur; gereksiz paraphrase yapma, bu iki kaynakta geçmeyen kelime ekleme. "
        "Sentetik örnek: previous 'Kütüphane hakkında bilgi almak istiyorum', "
        "current 'Saatleri nedir?' → context_used true, resolved_text "
        "'Kütüphane saatleri nedir?'. "
        "BİREBİR eşitlik YALNIZ context_used = false iken zorunludur; context "
        "gerçekten gerekliyse zorunlu DEĞİLDİR. "
        "Açık ve tek başına anlaşılan bir current turn'e eski konuyu taşıma: "
        "geçmiş görünür olduğu için context kullanma, yalnız gerçekten gerekliyse kullan."
    )
    # output_schema is a typed *example*: every value is a real instance of the
    # JSON type the field must carry. It previously used string type labels
    # ("intent_count": "1 or 2"), which the model mirrored literally and
    # returned "1" as a string — rejected by the strict Literal[1, 2] contract,
    # degrading every live request. Keep these values typed, never labels.
    payload = {
        "previous_user_turns": previous,
        "current_user_turn": current_user_turn.strip(),
        "output_schema": {
            "intent_count": 1,
            "intents": [
                {
                    "source_text": "non-empty string",
                    "normalized_text": "non-empty string",
                    "resolved_text": "non-empty string",
                    "context_used": False,
                    "calendar_relevant": False,
                }
            ],
        },
    }
    return system, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_intent_analysis(
    raw: str,
    *,
    current_user_turn: str,
    previous_user_turns: list[str] | tuple[str, ...],
) -> IntentAnalysis:
    """Strictly validate JSON plus current-turn/context provenance invariants."""
    if not raw or not raw.strip():
        raise ValueError("empty analyzer output")
    try:
        analysis = IntentAnalysis.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError("invalid analyzer schema") from exc

    current = current_user_turn.strip()
    previous = [text.strip() for text in previous_user_turns if text.strip()][
        -MAX_PREVIOUS_USER_TURNS:
    ]
    current_folded = _collapse(current)
    seen_sources: set[str] = set()

    for item in analysis.intents:
        source_folded = _collapse(item.source_text)
        if analysis.intent_count == 1:
            if source_folded != current_folded:
                raise ValueError("single intent source must preserve the whole current turn")
        elif source_folded not in current_folded:
            raise ValueError("multi intent source is not a current-turn segment")
        if source_folded in seen_sources:
            raise ValueError("duplicate source intent")
        seen_sources.add(source_folded)

        if not _tokens_are_supported(item.normalized_text, [item.source_text]):
            raise ValueError("normalized intent contains unsupported semantic expansion")

        if item.context_used:
            if not previous or _collapse(item.resolved_text) == _collapse(item.normalized_text):
                raise ValueError("context_used does not reflect context resolution")
            if not _tokens_are_supported(
                item.resolved_text, [item.normalized_text, *previous]
            ):
                raise ValueError("resolved intent contains unsupported semantic expansion")
            if not _uses_context_tokens(item.resolved_text, item.normalized_text, previous):
                raise ValueError("resolved intent does not use previous-user information")
        elif _collapse(item.resolved_text) != _collapse(item.normalized_text):
            raise ValueError("resolved intent changed without context")

    return analysis


def _collapse(text: str) -> str:
    return " ".join(text.strip().casefold().split())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def _token_supported(token: str, sources: list[str]) -> bool:
    if len(token) <= 2:
        return True
    for source in sources:
        if token == source:
            return True
        prefix = 0
        for left, right in zip(token, source):
            if left != right:
                break
            prefix += 1
        if prefix >= 3 or SequenceMatcher(None, token, source).ratio() >= 0.58:
            return True
    return False


def _tokens_are_supported(text: str, source_texts: list[str]) -> bool:
    source_tokens = [token for value in source_texts for token in _tokens(value)]
    return all(_token_supported(token, source_tokens) for token in _tokens(text))


def _uses_context_tokens(
    resolved_text: str, normalized_text: str, previous_user_turns: list[str]
) -> bool:
    normalized_tokens = _tokens(normalized_text)
    context_tokens = [token for value in previous_user_turns for token in _tokens(value)]
    return any(
        not _token_supported(token, normalized_tokens)
        and _token_supported(token, context_tokens)
        for token in _tokens(resolved_text)
    )
