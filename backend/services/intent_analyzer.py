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
        "Belirsizlikte SINGLE üret. MULTI yalnız birbirinden bağımsız iki hedef varsa "
        "kullanılır ve en fazla 2 intent üretilebilir. Current turn ana gerçekliktir. "
        "Previous user turns yalnız açık referans, eksilti veya kısa follow-up çözmek "
        "için kullanılabilir; açık bir current turn'e eski konu taşıma. "
        "source_text current turn'deki gerçek segment olmalıdır. normalized_text yalnız "
        "yazım, noktalama ve basit dilbilgisi düzeltmesi yapabilir. resolved_text yalnız "
        "gerçekten gereken context referansını açabilir. Kullanıcıda veya previous user "
        "turns'de bulunmayan kurum, kanal, işlem, qualifier ya da semantik bilgi EKLEME. "
        "context_used yalnız previous user turn'den semantik bilgi kullandıysan true olur. "
        "calendar_relevant yalnız doğru cevap AUZEF Academic Calendar tarih/dönem/event "
        "bilgisine ihtiyaç duyuyorsa true olur; salt kayıt/sınav/dönem kelimesi yetmez. "
        "Current turn 3 veya daha fazla bağımsız hedef içeriyorsa hiçbirini keyfi "
        "atmadan current turn'ü tek intent olarak koru. Yalnız strict JSON object döndür; "
        "markdown, açıklama veya ek alan kullanma."
    )
    payload = {
        "previous_user_turns": previous,
        "current_user_turn": current_user_turn.strip(),
        "output_schema": {
            "intent_count": "1 or 2",
            "intents": [
                {
                    "source_text": "non-empty string",
                    "normalized_text": "non-empty string",
                    "resolved_text": "non-empty string",
                    "context_used": "boolean",
                    "calendar_relevant": "boolean",
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
