"""Deterministic Stage A postmortem: failure taxonomy, split, alias evidence.

No model is called. Categories come from explicit, reproducible evidence:

- lexical *distinguishing-term support*: which of the competing candidates'
  distinguishing canonical terms (terms one canonical question has and the
  other lacks) actually occur in the user text;
- lexical *coverage*: share of the user's content terms found in a
  candidate's canonical question + answer;
- dataset/review facts: near-QnA relation and general/specific role
  (Phase 7A fixture), kb_overlap_flagged, the Stage A human review queue,
  and the historical alias table (``qna_queries``).

Whatever these rules cannot decide stays ``NEEDS_HUMAN_REVIEW``. The
taxonomy never changes a metric and never edits Gold.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Iterable, Mapping, Optional, Sequence

from benchmarks.selector_v2.challenge import first_candidate_correct, gold_rank, ordered_refs
from benchmarks.selector_v2.contract import fingerprint
from benchmarks.selector_v2.schema import BenchmarkResult, CaseSnapshot
from benchmarks.selector_v2.snapshot import NearPair

TAXONOMY_VERSION = "selector-failure-taxonomy-v1"
SPLIT_VERSION = "prompt-experiment-split-v1"
HOLDOUT_FRACTION = 0.3
STEM = 5
SHORT_INTENT_MAX_TERMS = 2       # FALSE_NONE_UNDERSPECIFIED evidence
SUFFICIENT_COVERAGE = 0.5        # gold candidate lexically covers the request

CATEGORIES = (
    "FALSE_NONE_UNDERSPECIFIED", "FALSE_NONE_OVERSTRICT", "UNSTATED_QUALIFIER_SPECIFICITY",
    "WRONG_NEAR_QNA_DISCRIMINATION", "ANSWER_TEXT_DISTRACTION", "POSITION_OVERRIDE",
    "GOLD_OR_ALIAS_QUESTIONABLE", "KB_OVERLAP_INTRINSIC", "OTHER", "NEEDS_HUMAN_REVIEW",
)
MISMATCH_GROUPS = {
    "A": "selector clearly wrong",
    "B": "Gold/alias expectation questionable",
    "C": "contract mismatch / both readings reasonable",
    "U": "undetermined (needs human review)",
}

# Generic Turkish function words and greetings (no domain terms).
_STOPWORDS = {
    "ne", "mi", "mı", "mu", "mü", "ve", "ile", "bir", "bu", "şu", "için", "icin", "ben",
    "benim", "siz", "size", "merhaba", "merhabalar", "var", "yok", "olarak", "da", "de",
    "ki", "ya", "veya", "hakkında", "hakkinda", "bilgi", "almak", "istiyorum", "istyrm",
    "miyim", "misin", "musun", "mıyım", "nasıl", "nasil", "nedir", "neden", "kadar",
    "daha", "önce", "once", "ama", "fakat", "ancak", "acaba", "lütfen", "lutfen", "mudur",
    "midir", "olur", "olabilir", "yapabilir", "miyiz", "doğru", "dogru", "hangi", "yapılır",
    "yapilir", "zaman", "demek", "demektir", "anlama", "anlamı", "anlami", "gelmektedir",
}
_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")
_FOLDED_STOPWORDS = {word.translate(_FOLD) for word in _STOPWORDS}


def terms(text: Optional[str]) -> set[str]:
    """Casefolded, diacritic-folded, stopword-free 5-char stems."""
    if not text:
        return set()
    lowered = text.replace("İ", "i").replace("I", "ı").lower()
    folded = unicodedata.normalize("NFKC", lowered).translate(_FOLD)
    out = set()
    for token in re.findall(r"[a-z0-9]+", folded):
        if token in _FOLDED_STOPWORDS or len(token) < 2:
            continue
        out.add(token[:STEM])
    return out


def _norm_alias(text: str) -> str:
    return " ".join((text or "").strip().casefold().split())


def alias_owners(alias_map: Mapping[str, set], text: str) -> list[int]:
    return sorted(alias_map.get(_norm_alias(text), set()))


def build_alias_map(rows: Iterable[tuple[int, str]]) -> dict[str, set]:
    out: dict[str, set] = {}
    for qna_id, query in rows:
        out.setdefault(_norm_alias(query), set()).add(int(qna_id))
    return out


# ── case packets ───────────────────────────────────────────────────────────

VALUE = {(True, True): "PRESERVE", (True, False): "CORRUPTION",
         (False, True): "RESCUE", (False, False): "UNRESOLVED"}


def case_packet(snapshot: CaseSnapshot, result: BenchmarkResult, alias_map: Mapping[str, set],
                membership: Sequence[str]) -> dict:
    refs = ordered_refs(snapshot)
    by_ref = {c.candidate_ref: c for c in snapshot.candidates}
    first = refs[0]
    selected = result.selected_candidate_ref
    return {
        "case_id": snapshot.case.case_id,
        "intent_text": snapshot.case.intent_text,
        "acceptable_refs": snapshot.case.acceptable_candidate_refs,
        "first_candidate_ref": first,
        "first_candidate_canonical": by_ref[first].canonical_text,
        "first_candidate_answer": by_ref[first].answer_text,
        "gold_rank": gold_rank(snapshot),
        "candidates": [  # retrieval position/source/score: OFFLINE postmortem only
            {"candidate_ref": c.candidate_ref, "position": c.order, "canonical_text": c.canonical_text,
             "answer_text": c.answer_text, "source": c.source, "retrieval_stage": c.retrieval_stage,
             "score": c.score, "alias_match": c.alias_match}
            for c in sorted(snapshot.candidates, key=lambda c: c.order)
        ],
        "model_decision": result.decision,
        "selected_candidate_ref": selected,
        "selected_position": refs.index(selected) + 1 if selected in refs else None,
        "model_correct": result.correct,
        "outcome": result.outcome.value,
        "value_class": VALUE[(first_candidate_correct(snapshot), result.correct)],
        "intent_alias_owner_qna_ids": alias_owners(alias_map, snapshot.case.intent_text),
        "membership": list(membership),
        "tags": snapshot.all_tags,
    }


def _cand(packet: dict, ref: Optional[str]) -> Optional[dict]:
    return next((c for c in packet["candidates"] if c["candidate_ref"] == ref), None)


def _best_expected(packet: dict) -> dict:
    """Acceptable candidate that is present and best placed."""
    present = [c for c in packet["candidates"] if c["candidate_ref"] in packet["acceptable_refs"]]
    return min(present, key=lambda c: c["position"])


def _pair_of(packet: dict, pairs: Sequence[NearPair]) -> Optional[NearPair]:
    keys = {t.split(":", 1)[1] for t in packet["tags"] if t.startswith("near_qna_pair:")}
    return next((p for p in pairs if p.key in keys), None)


def evidence(packet: dict) -> dict:
    intent = terms(packet["intent_text"])
    expected = _best_expected(packet)
    exp_q = terms(expected["canonical_text"])
    exp_full = exp_q | terms(expected["answer_text"])
    selected = _cand(packet, packet["selected_candidate_ref"])
    ev = {
        "intent_terms": len(intent),
        "expected_ref": expected["candidate_ref"],
        "expected_canonical_coverage": round(len(intent & exp_q) / len(intent), 3) if intent else 0.0,
        "expected_full_coverage": round(len(intent & exp_full) / len(intent), 3) if intent else 0.0,
        "intent_is_alias_of_expected": any(
            f"qna:{i}" in packet["acceptable_refs"] for i in packet["intent_alias_owner_qna_ids"]),
    }
    ev["gold_sufficiency"] = (
        "LEXICALLY_SUFFICIENT" if ev["expected_full_coverage"] >= SUFFICIENT_COVERAGE
        else "ALIAS_MAPPING_ONLY" if ev["intent_is_alias_of_expected"] else "WEAK_SUPPORT")
    if selected is not None and selected["candidate_ref"] not in packet["acceptable_refs"]:
        sel_q = terms(selected["canonical_text"])
        sel_full = sel_q | terms(selected["answer_text"])
        ev.update({
            "selected_distinguishing_support": sorted(intent & (sel_q - exp_q)),
            "expected_distinguishing_support": sorted(intent & (exp_q - sel_q)),
            "selected_full_coverage": round(len(intent & sel_full) / len(intent), 3) if intent else 0.0,
            "selected_canonical_coverage": round(len(intent & sel_q) / len(intent), 3) if intent else 0.0,
        })
    return ev


def classify(packet: dict, pairs: Sequence[NearPair], review_queue: Iterable[str]) -> dict:
    """Primary category + secondary tags + mismatch group, evidence-only."""
    ev = evidence(packet)
    tags = set(packet["tags"])
    secondary: list[str] = []
    none = packet["model_decision"] == "NONE"
    if packet["model_correct"]:
        return {"primary": None, "secondary": [], "mismatch_group": None, "evidence": ev}
    if packet["outcome"] in ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT"):
        return {"primary": "OTHER", "secondary": [packet["outcome"]], "mismatch_group": "U",
                "evidence": ev}
    if not none and packet["gold_rank"] == 1 and (packet["selected_position"] or 0) > 1:
        secondary.append("POSITION_OVERRIDE")
    pair = _pair_of(packet, pairs)
    if not none and pair is not None and pair.relation == "general_specific" \
            and "gs_expected:general" in tags \
            and packet["selected_candidate_ref"] in (f"qna:{pair.a}", f"qna:{pair.b}") \
            and not ev["selected_distinguishing_support"]:
        # Specific sibling chosen although the user stated no distinguishing term.
        extra = ["reviewer_flagged_kb_overlap"] if "kb_overlap_flagged" in tags else []
        return {"primary": "UNSTATED_QUALIFIER_SPECIFICITY", "secondary": secondary + extra,
                "mismatch_group": "A", "evidence": ev}
    if packet["case_id"] in set(review_queue):
        return {"primary": "GOLD_OR_ALIAS_QUESTIONABLE", "secondary": secondary + ["review_queue"],
                "mismatch_group": "B", "evidence": ev}
    if "kb_overlap_flagged" in tags:
        return {"primary": "KB_OVERLAP_INTRINSIC", "secondary": secondary + ["reviewer_flagged"],
                "mismatch_group": "C", "evidence": ev}
    if none:
        sufficiency = ev["gold_sufficiency"]
        if ev["intent_terms"] <= SHORT_INTENT_MAX_TERMS:
            primary, group = "FALSE_NONE_UNDERSPECIFIED", "C"
        elif sufficiency == "LEXICALLY_SUFFICIENT":
            primary, group = "FALSE_NONE_OVERSTRICT", "C"
        else:
            primary, group = "NEEDS_HUMAN_REVIEW", "U"
        return {"primary": primary, "secondary": secondary, "mismatch_group": group, "evidence": ev}

    sel_support = len(ev["selected_distinguishing_support"])
    exp_support = len(ev["expected_distinguishing_support"])
    sibling = pair is not None and packet["selected_candidate_ref"] in (f"qna:{pair.a}", f"qna:{pair.b}")
    if ev["selected_full_coverage"] > ev["selected_canonical_coverage"] and \
            ev["selected_full_coverage"] > ev["expected_full_coverage"]:
        secondary.append("ANSWER_TEXT_SIGNAL")
    if sel_support > exp_support:
        # The user literally states what distinguishes the model's choice.
        group = "C" if pair is not None and pair.relation == "near_duplicate" else "B"
        return {"primary": "GOLD_OR_ALIAS_QUESTIONABLE",
                "secondary": secondary + ["intent_states_selected_distinction"],
                "mismatch_group": group, "evidence": ev}
    if sibling and (exp_support > sel_support
                    or ev["expected_full_coverage"] > ev["selected_full_coverage"]):
        return {"primary": "WRONG_NEAR_QNA_DISCRIMINATION", "secondary": secondary,
                "mismatch_group": "A", "evidence": ev}
    if sibling and pair.relation == "near_duplicate":
        return {"primary": "WRONG_NEAR_QNA_DISCRIMINATION", "secondary": secondary,
                "mismatch_group": "C", "evidence": ev}
    if "ANSWER_TEXT_SIGNAL" in secondary and exp_support >= sel_support \
            and ev["expected_canonical_coverage"] > ev["selected_canonical_coverage"]:
        return {"primary": "ANSWER_TEXT_DISTRACTION", "secondary": secondary,
                "mismatch_group": "A", "evidence": ev}
    return {"primary": "NEEDS_HUMAN_REVIEW", "secondary": secondary, "mismatch_group": "U",
            "evidence": ev}


# ── DEV / HOLDOUT split ────────────────────────────────────────────────────

STRATA = ("A_first_candidate_wrong", "C_general_specific", "B_near_qna",
          "D_kb_overlap_flagged", "easy_control")


def _case_key(case_id: str):
    head = case_id.split("#", 1)[0]
    return (int(head) if head.isdigit() else float("inf"), case_id)


def _split_key(case_id: str) -> str:
    return hashlib.sha256(f"{SPLIT_VERSION}|{case_id}".encode()).hexdigest()


def stratified_split(challenge_cases: Sequence[dict],
                     holdout_fraction: float = HOLDOUT_FRACTION) -> dict:
    strata: dict[str, list[str]] = {}
    for case in challenge_cases:
        key = "|".join(s for s in STRATA if s in case["membership"]) or "other"
        strata.setdefault(key, []).append(case["case_id"])
    dev, holdout = [], []
    for key in sorted(strata):
        ids = sorted(strata[key], key=_split_key)
        n_hold = int(len(ids) * holdout_fraction + 0.5)
        holdout += ids[:n_hold]
        dev += ids[n_hold:]
    dev.sort(key=_case_key)
    holdout.sort(key=_case_key)
    split = {
        "split_version": SPLIT_VERSION,
        "algorithm": f"stratum = membership ∩ {list(STRATA)}; within each stratum sort by "
                     f"sha256('{SPLIT_VERSION}|'+case_id); first round({holdout_fraction}·n) "
                     "→ HOLDOUT, rest → DEV",
        "strata": {k: len(v) for k, v in sorted(strata.items())},
        "dev": dev,
        "holdout": holdout,
    }
    split["split_fingerprint"] = fingerprint({"version": SPLIT_VERSION, "dev": dev, "holdout": holdout})
    return split


# ── full postmortem assembly ───────────────────────────────────────────────

REVIEW_RECOMMENDATIONS = {
    # Stage A human-review queue; recommendations only, never decisions.
    "404": ("REVIEW_RECOMMENDED", "intent names no answerable need; gold looks context-dependent"),
    "19": ("REVIEW_RECOMMENDED", "objection wording vs application-procedure gold"),
    "35": ("KB_MAPPING_REVIEW", "gold answer text scope differs from the canonical question"),
    "221": ("REVIEW_RECOMMENDED", "selected candidate may be an equally acceptable answer"),
    "158": ("KB_MAPPING_REVIEW", "general vs exam-system login overlap in KB"),
    "74": ("KB_MAPPING_REVIEW", "retrieval miss: intent is an exact alias of another QnA"),
    "436": ("KB_MAPPING_REVIEW", "retrieval miss: intent is an exact alias of another QnA"),
}


def _counter(items) -> dict:
    out: dict = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], str(kv[0]))))


def _metrics_for(ids: Sequence[str], packets_all: Mapping[str, dict]) -> dict:
    rows = [packets_all[i] for i in ids]
    first_right = [r for r in rows if r["value_class"] in ("PRESERVE", "CORRUPTION")]
    first_wrong = [r for r in rows if r["value_class"] in ("RESCUE", "UNRESOLVED")]
    rescue = sum(1 for r in rows if r["value_class"] == "RESCUE")
    corruption = sum(1 for r in rows if r["value_class"] == "CORRUPTION")
    return {
        "cases": len(rows),
        "production_exact": f"{sum(r['model_correct'] for r in rows)}/{len(rows)}",
        "first_candidate_exact": f"{len(first_right)}/{len(rows)}",
        "rescue": f"{rescue}/{len(first_wrong)}",
        "corruption": f"{corruption}/{len(first_right)}",
        "preserve": sum(1 for r in rows if r["value_class"] == "PRESERVE"),
        "unresolved": sum(1 for r in rows if r["value_class"] == "UNRESOLVED"),
        "net_corrections": rescue - corruption,
        "false_none": sum(1 for r in rows if r["outcome"] == "FALSE_NONE"),
    }


def alias_structure(snapshots: Sequence[CaseSnapshot], alias_map: Mapping[str, set],
                    case_ids: Optional[set] = None) -> dict:
    rows = [s for s in snapshots if s.selector_evaluable and s.case.primary
            and (case_ids is None or s.case.case_id in case_ids)]
    rel = {"alias_of_expected": 0, "alias_of_other_qna": 0, "not_an_alias": 0}
    first_is_owner = first_is_owner_correct = alias_flag_first = 0
    exact_bypass_correct = exact_bypass_wrong = 0
    for s in rows:
        owners = alias_owners(alias_map, s.case.intent_text)
        expected = {int(r.split(":")[1]) for r in s.case.acceptable_candidate_refs}
        if not owners:
            rel["not_an_alias"] += 1
        elif set(owners) & expected:
            rel["alias_of_expected"] += 1
        else:
            rel["alias_of_other_qna"] += 1
        if len(owners) == 1:
            if owners[0] in expected:
                exact_bypass_correct += 1
            else:
                exact_bypass_wrong += 1
        first = sorted(s.candidates, key=lambda c: c.order)[0]
        first_id = int(first.candidate_ref.split(":")[1])
        alias_flag_first += bool(first.alias_match)
        if first_id in owners:
            first_is_owner += 1
            first_is_owner_correct += first_candidate_correct(s)
    return {
        "cases": len(rows),
        "intent_alias_relation": rel,
        "first_candidate_is_intent_alias_owner": first_is_owner,
        "first_candidate_alias_owner_and_correct": first_is_owner_correct,
        "first_candidate_retrieved_via_alias_point": alias_flag_first,
        "exact_alias_bypass_would_be_correct": exact_bypass_correct,
        "exact_alias_bypass_would_be_wrong": exact_bypass_wrong,
    }


def _specificity_row(packet: dict, pairs: Sequence[NearPair]) -> dict:
    pair = _pair_of(packet, pairs)
    tags = set(packet["tags"])
    role = "general" if "gs_expected:general" in tags else "specific"
    expected_id = int(_best_expected(packet)["candidate_ref"].split(":")[1])
    sibling = f"qna:{pair.sibling(expected_id)}" if pair else None
    ev = evidence(packet)
    selected = packet["selected_candidate_ref"]
    if packet["model_correct"]:
        verdict = "CORRECT"
    elif packet["model_decision"] == "NONE":
        verdict = "NONE"
    elif selected == sibling and role == "general":
        verdict = ("CHOSE_SPECIFIC_USER_STATED_DISTINGUISHING_TERM"
                   if ev.get("selected_distinguishing_support")
                   else "CHOSE_SPECIFIC_UNSTATED_QUALIFIER_RULE_VIOLATED")
    elif selected == sibling:
        verdict = "CHOSE_GENERAL_SIBLING"
    else:
        verdict = "CHOSE_OTHER_CANDIDATE"
    by_ref = {c["candidate_ref"]: c for c in packet["candidates"]}
    return {
        "case_id": packet["case_id"], "expected_role": role, "pair": pair.key if pair else None,
        "intent_text": packet["intent_text"],
        "expected": {r: by_ref.get(r, {}).get("canonical_text") for r in packet["acceptable_refs"]},
        "competing_sibling": {sibling: by_ref.get(sibling, {}).get("canonical_text")} if sibling else None,
        "first_candidate": {packet["first_candidate_ref"]: packet["first_candidate_canonical"]},
        "model_choice": ({selected: by_ref.get(selected, {}).get("canonical_text")}
                         if selected else None),
        "model_none": packet["model_decision"] == "NONE",
        "selected_distinguishing_support": ev.get("selected_distinguishing_support"),
        "verdict": verdict,
    }


def _rescue_row(packet: dict) -> dict:
    ev = evidence(packet)
    first_terms = terms(packet["first_candidate_canonical"])
    exp = _best_expected(packet)
    intent = terms(packet["intent_text"])
    return {
        "case_id": packet["case_id"], "intent_text": packet["intent_text"],
        "gold_rank": packet["gold_rank"],
        "first_candidate": {packet["first_candidate_ref"]: packet["first_candidate_canonical"]},
        "model_choice": {exp["candidate_ref"]: exp["canonical_text"]},
        "why_first_wrong_evidence": {
            "intent_alias_owner_qna_ids": packet["intent_alias_owner_qna_ids"],
            "first_is_alias_owner": int(packet["first_candidate_ref"].split(":")[1])
            in packet["intent_alias_owner_qna_ids"],
        },
        "distinction_terms_supporting_model": sorted(intent & (terms(exp["canonical_text"]) - first_terms)),
        "distinction_terms_supporting_first": sorted(intent & (first_terms - terms(exp["canonical_text"]))),
        "expected_full_coverage": ev["expected_full_coverage"],
        "tags": [t for t in packet["tags"] if t in ("near_qna", "general_specific",
                                                   "kb_overlap_flagged", "multi_acceptable")],
    }


def build_postmortem(snapshots, challenge_cases, results, alias_map, pairs,
                     review_queue: Sequence[str]) -> dict:
    by_id = {s.case.case_id: s for s in snapshots}
    packets = {c["case_id"]: case_packet(by_id[c["case_id"]], results[c["case_id"]], alias_map,
                                         c["membership"]) for c in challenge_cases}
    informative = [p for p in packets.values() if p["value_class"] != "PRESERVE"]
    classified = {p["case_id"]: classify(p, pairs, review_queue) for p in informative}
    failures = [{**p, "classification": classified[p["case_id"]]}
                for p in informative if not p["model_correct"]]
    false_none = []
    for p in failures:
        if p["outcome"] != "FALSE_NONE":
            continue
        ev = classified[p["case_id"]]["evidence"]
        tags = set(p["tags"])
        false_none.append({
            "case_id": p["case_id"], "intent_text": p["intent_text"],
            "acceptable_in_candidate_set": True, "gold_rank": p["gold_rank"],
            "intent_terms": ev["intent_terms"],
            "intent_alias_relation": ("exact_alias_of_expected" if ev["intent_is_alias_of_expected"]
                                      else "alias_of_other_qna" if p["intent_alias_owner_qna_ids"]
                                      else "not_an_alias"),
            "expected_full_coverage": ev["expected_full_coverage"],
            "gold_sufficiency": ev.get("gold_sufficiency"),
            "near_qna": "near_qna" in tags, "general_specific": "general_specific" in tags,
            "kb_overlap_flagged": "kb_overlap_flagged" in tags,
            "primary": classified[p["case_id"]]["primary"],
            "value_class": p["value_class"],
        })
    specificity = [_specificity_row(p, pairs) for p in packets.values()
                   if "general_specific" in p["tags"]]
    rescues = [_rescue_row(p) for p in informative if p["value_class"] == "RESCUE"]

    def dist(value_class):
        return _counter(classified[p["case_id"]]["primary"] for p in failures
                        if p["value_class"] == value_class)

    primaries = [c["primary"] for c in classified.values() if c["primary"]]
    secondaries = [t for c in classified.values() for t in c["secondary"]]
    metadata_cases = sorted((cid for cid, c in classified.items()
                             if c["primary"] in ("KB_OVERLAP_INTRINSIC", "UNSTATED_QUALIFIER_SPECIFICITY")),
                            key=lambda x: int(x.split("#")[0]))
    failure_ids = {p["case_id"] for p in failures}
    counterfactual = {
        "answer_text_signal_cases": sorted(
            (cid for cid, c in classified.items() if "ANSWER_TEXT_SIGNAL" in c["secondary"]),
            key=int),
        "expected_answer_adds_coverage_cases": sorted(
            (cid for cid, c in classified.items()
             if c["evidence"]["expected_full_coverage"] > c["evidence"]["expected_canonical_coverage"]),
            key=int),
        "note": "descriptive only; no canonical-only live counterfactual was run",
    }
    summary = {
        "taxonomy_version": TAXONOMY_VERSION,
        "universe": {"challenge_cases": len(packets), "informative_cases": len(informative),
                     "corruption": sum(p["value_class"] == "CORRUPTION" for p in informative),
                     "unresolved": sum(p["value_class"] == "UNRESOLVED" for p in informative),
                     "rescue": sum(p["value_class"] == "RESCUE" for p in informative),
                     "false_none": len(false_none), "model_wrong": len(failures)},
        "corruption_primary": dist("CORRUPTION"),
        "unresolved_primary": dist("UNRESOLVED"),
        "false_none_primary": _counter(r["primary"] for r in false_none),
        "false_none_gold_sufficiency": _counter(r["gold_sufficiency"] for r in false_none),
        "all_failures_primary": _counter(primaries),
        "secondary_tags": _counter(secondaries),
        "mismatch_groups": {k: {"meaning": MISMATCH_GROUPS[k],
                                "count": sum(1 for cid in failure_ids
                                             if classified[cid]["mismatch_group"] == k)}
                            for k in MISMATCH_GROUPS},
        "needs_human_review": sorted((cid for cid in failure_ids
                                      if classified[cid]["primary"] == "NEEDS_HUMAN_REVIEW"), key=int),
        "metadata_candidate_cases": {"ids": metadata_cases, "count": len(metadata_cases),
                                     "percentage_of_failures": round(len(metadata_cases) / len(failures), 4)
                                     if failures else None,
                                     "definition": "primary in KB_OVERLAP_INTRINSIC, "
                                                   "UNSTATED_QUALIFIER_SPECIFICITY"},
        "specificity_verdicts": {
            role: _counter(r["verdict"] for r in specificity if r["expected_role"] == role)
            for role in ("general", "specific")},
        "canonical_vs_answer_diagnostic": counterfactual,
        "rules": {
            "short_intent_max_terms": SHORT_INTENT_MAX_TERMS,
            "sufficient_coverage": SUFFICIENT_COVERAGE,
            "stem_chars": STEM,
        },
    }
    review = [{"case_id": cid, "recommendation": rec, "evidence_note": note,
               "in_challenge": cid in packets,
               "stage_a_outcome": packets[cid]["outcome"] if cid in packets else None,
               "human_decision": None}
              for cid, (rec, note) in REVIEW_RECOMMENDATIONS.items()]
    accounted = {
        "corruption": summary["universe"]["corruption"],
        "unresolved": summary["universe"]["unresolved"],
        "rescue": summary["universe"]["rescue"],
        "false_none": summary["universe"]["false_none"],
        "every_failure_has_primary": all(classified[cid]["primary"] for cid in failure_ids),
    }
    return {"packets": packets, "failures": failures, "false_none": false_none,
            "specificity": specificity, "rescues": rescues, "summary": summary,
            "review": review, "accounted": accounted}
