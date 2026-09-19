"""Selector V2: semantic verifier prompt and strict SELECT/NONE parser.

The selector receives only the resolved intent and the eligible, bounded
candidate set (ref, kind, canonical text, answer). It never sees conversation
history, retrieval scores, ranks, providers or alias flags. Its output is a
strict JSON decision; malformed output, an unknown candidate_ref and an empty
response are INVALID_OUTPUT — never semantic NONE.
"""
from __future__ import annotations

import json
from typing import Optional, Sequence

from pydantic import ValidationError

from services.candidate_eligibility import SelectorCandidate
from services.llm_types import (
    LLMInvocationResult,
    LLMOutcomeStatus,
    LLMParseStatus,
    SelectorDecision,
    SelectorResult,
)


SELECTOR_SYSTEM_PROMPT = (
    "Sen AUZEF Selector'sın. Görevin en benzer adayı bulmak değil, doğrulamaktır: "
    "resolved_intent'i ve sana verilen candidate listesini değerlendirir, intent'i "
    "güvenilir biçimde karşılayan TEK bir candidate varsa onu seçersin; yoksa NONE "
    "döndürürsün.\n"
    "Bir candidate yalnız şu koşulların hepsi sağlanıyorsa seçilebilir:\n"
    "1. Konu gerçekten aynıdır.\n"
    "2. answer_text kullanıcının asıl bilgi ihtiyacını cevaplar; yan bilgi yetmez.\n"
    "3. Seçmek için kullanıcının söylemediği bir koşul, qualifier veya durum "
    "varsaymak gerekmez.\n"
    "4. Candidate kullanıcının açıkça belirttiği bir qualifier ile çelişmez.\n"
    "5. Ortak kelimeler taşımak tek başına yeterli değildir.\n"
    "6. answer_text intent'in merkezini gerçekten karşılar.\n"
    "Genel/özel kuralı: kullanıcının açıkça söylemediği bir qualifier varsayılarak "
    "daha özel bir candidate seçilmez. Qualifier örnekleri: belirli bir başvuru veya "
    "geçiş türü (ör. merkezi yatay geçiş), belirli program ya da öğrenci grubu (ör. "
    "ikinci üniversite, belirli bir bölüm, lisans/önlisans), belirli sınav türü, "
    "belirli dönem. Örnek: 'Yatay geçiş nasıl yapılır?' sorusunda kullanıcı merkezi "
    "demediyse merkezi yatay geçiş candidate'ı seçilmez; genel ihtiyacı karşılayan "
    "candidate varsa o seçilir. 'Merkezi yatay geçiş nasıl yapılır?' sorusunda merkezi "
    "yatay geçiş candidate'ı uygundur. Bu bir kelime eşleştirme kuralı değildir; "
    "anlamı değerlendir.\n"
    "kind=CALENDAR candidate'ları akademik takvim kayıtlarıdır; yalnız kullanıcı o "
    "event'in tarihini veya dönemini soruyorsa intent'i karşılar.\n"
    "Birden fazla candidate kabul edilebilir olsa bile yalnız birini seç. Candidate "
    "sırası önem, doğruluk veya güven bildirmez. Candidate listesi dışında ref, cevap "
    "veya bilgi üretme.\n"
    "Yalnız strict JSON object döndür: "
    '{"decision":"SELECT","candidate_ref":"<listedeki candidate_ref>"} ya da '
    '{"decision":"NONE"}. Markdown, açıklama, gerekçe, güven skoru veya ek alan yazma.'
)


def build_selector_prompt(
    resolved_intent: str, candidates: Sequence[SelectorCandidate]
) -> tuple[str, str]:
    """Serialize only resolved intent + semantic candidate content."""
    payload = {
        "resolved_intent": resolved_intent.strip(),
        "candidates": [candidate.prompt_view() for candidate in candidates],
    }
    return SELECTOR_SYSTEM_PROMPT, json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )


def _invalid(reason: str, invocation: Optional[LLMInvocationResult]) -> SelectorResult:
    return SelectorResult(
        status=LLMOutcomeStatus.INVALID_OUTPUT,
        parse_status=LLMParseStatus.INVALID_OUTPUT,
        answer=None,
        invalid_reason=reason,
        invocation=invocation,
    )


def parse_selector_output(
    raw: Optional[str],
    candidates: Sequence[SelectorCandidate],
    invocation: Optional[LLMInvocationResult] = None,
) -> SelectorResult:
    """Strict JSON + Pydantic validation + candidate-set membership check."""
    if raw is None or not raw.strip():
        return _invalid("empty_output", invocation)
    try:
        decision = SelectorDecision.model_validate_json(raw.strip())
    except ValidationError as exc:
        json_error = any(error.get("type") == "json_invalid" for error in exc.errors())
        return _invalid("malformed_json" if json_error else "schema_violation", invocation)

    if decision.decision == "NONE":
        return SelectorResult(
            status=LLMOutcomeStatus.SEMANTIC_NONE,
            parse_status=LLMParseStatus.SEMANTIC_NONE,
            answer=None,
            decision="NONE",
            invocation=invocation,
        )

    by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
    selected = by_ref.get(decision.candidate_ref)
    if selected is None:
        return _invalid("unknown_candidate_ref", invocation)
    return SelectorResult(
        status=LLMOutcomeStatus.SUCCESS,
        parse_status=LLMParseStatus.SUCCESS,
        answer=selected.answer_text,
        decision="SELECT",
        selected_candidate_ref=selected.candidate_ref,
        selected_kind=selected.kind.value,
        selected_qna_id=selected.qna_id,
        selected_calendar_id=selected.calendar_id,
        selected_candidate_source=selected.source,
        invocation=invocation,
    )
