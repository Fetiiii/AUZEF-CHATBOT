"""Phase 7B qualifier-failure postmortem and candidate-order experiment prep.

Offline only: no provider call. Everything here is model-independent except
the explicitly labelled saved-output analyses (position association and the
prompt-rule compliance taxonomy), which read SAVED Stage A / Variant A
outputs and never select cases.

* A case is **qualifier-sensitive** when its frozen candidate set holds a
  qualified variant S (canonical question carries a qualifier concept q) and
  a sibling G without q that shares a topic stem with S that the user text
  also contains. The qualifier lexicon is a TAXONOMY of generic Turkish
  academic qualifier concepts, not a case list; detection uses canonical
  questions only (never answers, Gold, ranks or model outputs).
* The 471/472 blind packet shows every eligible candidate, hash-ordered, with
  anonymous labels, and nothing else.
* The order experiment differs between conditions ONLY in candidate order.
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from math import comb
from typing import Mapping, Optional, Sequence

from benchmarks.selector_v2 import adjudication as adj
from benchmarks.selector_v2.contract import (
    NEUTRAL_ORDER, PRODUCTION_ORDER, build_model_input, fingerprint, order_candidates, sha256_text,
)
from benchmarks.selector_v2.postmortem import _case_key, terms
from benchmarks.selector_v2.schema import BenchmarkResult, CaseSnapshot

INVENTORY_VERSION = "selector-qualifier-inventory-v1"
REVIEW_SCHEMA_VERSION = "selector-qualifier-readjudication-v1"
DIAGNOSTIC_VERSION = "selector-order-diagnostic-set-v1"
FINAL_VALIDATION_VERSION = "selector-final-validation-proposal-v1"
CRITICAL_CASES = ("471", "472")
REVIEW_DECISIONS = ("SELECT_ACCEPTABLE", "EXPECT_NONE", "EXCLUDE_AMBIGUOUS",
                    "CONTENT_REVIEW_REQUIRED", "RETRIEVAL_OR_KB_MAPPING_REVIEW")

_FOLD = str.maketrans("çğıöşüâîû", "cgiosuaiu")

# Qualifier TAXONOMY (concept → folded-text patterns). Generic academic
# qualifiers; order matters (longer phrases first, matched spans removed).
QUALIFIER_LEXICON: tuple[tuple[str, str], ...] = (
    ("ikinci_universite", r"\bikinci (?:universite|uni)\w*"),
    ("sinavsiz", r"\bsinavsiz\w*"),
    ("merkezi", r"\bmerkezi\b"),
    ("dgs", r"\bdgs\b"),
    ("yks", r"\byks\b"),
    ("uzaktan", r"\buzaktan\b"),
    ("acikogretim", r"\bacik ?ogretim\w*|\baof\b"),
    ("yuksek_lisans", r"\byuksek ?lisans\w*"),
    ("onlisans", r"\bon ?lisans\w*"),
    ("lisans", r"\blisans\w*"),
    ("final", r"\bfinal\w*"),
    ("butunleme", r"\bbutunleme\w*"),
)
# Compound nouns / proper names that contain a qualifier word without
# qualifying anything ("sınav merkezi" = exam centre, "Çözüm Merkezi" = help desk).
LEXICON_EXCLUSIONS = (r"\b(?:cozum|sinav|cagri|iletisim|destek|uygulama|arastirma) merkez\w*",)
def fold(text: Optional[str]) -> str:
    lowered = (text or "").replace("İ", "i").replace("I", "ı").lower()
    folded = unicodedata.normalize("NFKC", lowered).translate(_FOLD)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", folded).split())


def _scan(text: Optional[str]) -> tuple[set[str], str]:
    """(qualifier concepts, folded text with qualifier spans removed)."""
    rest = fold(text)
    for pattern in LEXICON_EXCLUSIONS:
        rest = re.sub(pattern, " ", rest)
    found = set()
    for concept, pattern in QUALIFIER_LEXICON:
        if re.search(pattern, rest):
            found.add(concept)
            rest = re.sub(pattern, " ", rest)
    return found, rest


def qualifiers(text: Optional[str]) -> set[str]:
    """Qualifier concepts present in a text (lexicon taxonomy, spans consumed)."""
    return _scan(text)[0]


def topic_terms(text: Optional[str]) -> set[str]:
    """Topic stems with the qualifier spans themselves removed."""
    return terms(_scan(text)[1])


def neutral_order(snapshot: CaseSnapshot) -> list[str]:
    cands = order_candidates(snapshot.selector_candidates(), case_id=snapshot.case.case_id,
                             order=NEUTRAL_ORDER)
    return [c.candidate_ref for c in cands]


def original_order(snapshot: CaseSnapshot) -> list[str]:
    return [c.candidate_ref for c in snapshot.selector_candidates()]


# ── 1. inventory (model-independent) ───────────────────────────────────────

def qualifier_pairs(snapshot: CaseSnapshot) -> list[dict]:
    """(q, specific S, general G) triples inside one frozen candidate set."""
    user_topic = topic_terms(snapshot.case.intent_text)
    cands = snapshot.candidates
    quals = {c.candidate_ref: qualifiers(c.canonical_text) for c in cands}
    topic = {c.candidate_ref: topic_terms(c.canonical_text) for c in cands}
    out = []
    concepts = sorted({q for qs in quals.values() for q in qs})
    for q in concepts:
        specific = [c.candidate_ref for c in cands if q in quals[c.candidate_ref]]
        general = [c.candidate_ref for c in cands if q not in quals[c.candidate_ref]]
        pairs = []
        for s in specific:
            for g in general:
                shared = topic[s] & topic[g] & user_topic
                if shared:
                    pairs.append((s, g, sorted(shared)))
        if pairs:
            out.append({"qualifier": q,
                        "specific": sorted({p[0] for p in pairs}, key=_ref_key),
                        "general": sorted({p[1] for p in pairs}, key=_ref_key),
                        "shared_topic_stems": sorted({t for p in pairs for t in p[2]})})
    return out


def _ref_key(ref: str):
    kind, _, num = ref.partition(":")
    return (kind, int(num) if num.isdigit() else num)


def inventory(snapshots: Sequence[CaseSnapshot], sem_by: Mapping[str, CaseSnapshot],
              side: Mapping[str, str]) -> list[dict]:
    rows = []
    for snap in snapshots:
        if not snap.case.primary or not snap.candidates:
            continue
        pairs = qualifier_pairs(snap)
        if not pairs:
            continue
        cid = snap.case.case_id
        user_q = qualifiers(snap.case.intent_text)
        sem = sem_by[cid]
        rows.append({
            "case_id": cid,
            "source_case_id": snap.case.source_case_id,
            "split": side.get(cid, "OUTSIDE_CHALLENGE"),
            "user_qualifiers": sorted(user_q),
            "pairs": pairs,
            "mode": ("STATED" if any(p["qualifier"] in user_q for p in pairs) else "UNSTATED"),
            "unstated_qualifiers": sorted({p["qualifier"] for p in pairs} - user_q),
            "semantic_evaluable": sem.selector_evaluable,
            "semantic_status": (sem.case.evaluation_status.value if not sem.selector_evaluable
                                else "SELECTOR_EVALUABLE"),
            "semantic_status_reason": (None if sem.selector_evaluable
                                       else sem.case.status_reason or sem.pool_status.value),
        })
    return rows


def expectation_class(row: dict, sem: CaseSnapshot) -> dict:
    """Semantic Gold expectation relative to the case's qualifier pairs."""
    if not sem.selector_evaluable:
        return {"class": "EXCLUDED", "accepted": []}
    if sem.case.expected_decision == "NONE":
        return {"class": "NONE", "accepted": []}
    accepted = list(sem.case.acceptable_candidate_refs)
    specific = {r for p in row["pairs"] for r in p["specific"]}
    general = {r for p in row["pairs"] for r in p["general"]}
    kinds = {("specific" if r in specific else "general" if r in general else "other")
             for r in accepted}
    if len(accepted) > 1:
        cls = "MULTI_ACCEPTABLE"
    else:
        cls = {"specific": "SPECIFIC", "general": "GENERAL", "other": "OTHER"}[next(iter(kinds))]
    return {"class": cls, "accepted": accepted, "accepted_kinds": sorted(kinds)}


# ── 2. order analysis (retrieval order; model-independent) ─────────────────

def order_row(row: dict, sem: CaseSnapshot) -> dict:
    order = original_order(sem)
    pos = {r: i + 1 for i, r in enumerate(order)}
    exp = expectation_class(row, sem)
    accepted = exp["accepted"]
    unstated = set(row["unstated_qualifiers"])
    cand_q = {c.candidate_ref: qualifiers(c.canonical_text) for c in sem.candidates}
    specific = {r for p in row["pairs"] for r in p["specific"]}
    general = {r for p in row["pairs"] for r in p["general"]}
    p1 = order[0]
    neutral = neutral_order(sem)
    return {
        "case_id": row["case_id"], "split": row["split"], "mode": row["mode"],
        "expectation": exp["class"],
        "accepted_positions": sorted(pos[r] for r in accepted if r in pos),
        "position1": p1,
        "position1_accepted": p1 in accepted,
        "position1_too_specific": p1 not in accepted and bool(cand_q[p1] & unstated),
        "accepted_general_positions": sorted(pos[r] for r in accepted if r in general),
        "accepted_specific_positions": sorted(pos[r] for r in accepted if r in specific),
        "general_expected_specific_position1": (exp["class"] == "GENERAL" and p1 not in accepted
                                                and bool(cand_q[p1] & unstated)),
        "candidate_count": len(order),
        "neutral_position1": neutral[0],
        "neutral_accepted_positions": sorted(neutral.index(r) + 1 for r in accepted if r in neutral),
    }


# ── 3. saved-output analyses (no case selection) ───────────────────────────

def binom_tail_ge(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1)) if n else 1.0


def position_association(sem_by: Mapping[str, CaseSnapshot], ids: Sequence[str],
                         runs: Mapping[str, Mapping[str, BenchmarkResult]]) -> dict:
    """Group A: position 1 accepted; Group B: not. Saved outputs only."""
    groups = {"A_position1_accepted": [], "B_position1_not_accepted": []}
    for i in ids:
        s = sem_by[i]
        p1 = original_order(s)[0]
        key = "A_position1_accepted" if p1 in s.case.acceptable_candidate_refs \
            else "B_position1_not_accepted"
        groups[key].append(i)
    out = {"groups": {k: {"cases": len(v), "ids": v} for k, v in groups.items()}, "by_run": {}}
    for name, results in runs.items():
        entry = {}
        for g, members in groups.items():
            have = [i for i in members if i in results]
            chose1 = [i for i in have if results[i].selected_candidate_ref
                      == original_order(sem_by[i])[0]]
            entry[g] = {"with_output": len(have), "chose_position1": len(chose1),
                        "position1_rate": round(len(chose1) / len(have), 4) if have else None}
        b = [i for i in groups["B_position1_not_accepted"] if i in results]
        wrong = [i for i in b if results[i].decision == "SELECT" and not results[i].correct]
        wrong_p1 = [i for i in wrong if results[i].selected_candidate_ref == original_order(sem_by[i])[0]]
        chance = [1 / max(1, len(sem_by[i].candidates) - len(
            set(sem_by[i].case.acceptable_candidate_refs) & set(original_order(sem_by[i]))))
            for i in wrong]
        p0 = sum(chance) / len(chance) if chance else 0.0
        entry["group_B"] = {
            "wrong_select": len(wrong), "wrong_position1": len(wrong_p1), "wrong_position1_ids": wrong_p1,
            "wrong_position1_share": round(len(wrong_p1) / len(wrong), 4) if wrong else None,
            "uniform_chance_share": round(p0, 4),
            "ratio_to_chance": round((len(wrong_p1) / len(wrong)) / p0, 3) if wrong and p0 else None,
            "binomial_p_one_sided": round(binom_tail_ge(len(wrong_p1), len(wrong), p0), 6) if wrong else None,
            "none_output": sum(1 for i in b if results[i].decision == "NONE"),
            "correct": sum(1 for i in b if results[i].correct),
        }
        all_have = [i for i in ids if i in results]
        entry["overall_position1_rate"] = (round(sum(
            1 for i in all_have if results[i].selected_candidate_ref == original_order(sem_by[i])[0])
            / len(all_have), 4) if all_have else None)
        entry["with_output"] = len(all_have)
        out["by_run"][name] = entry
    return out


def order_bias_verdict(assoc: Mapping[str, dict], run: str) -> dict:
    """Pre-declared rule on Group-B wrong SELECTs (saved outputs of ``run``)."""
    b = assoc["by_run"][run]["group_B"]
    ratio, p = b["ratio_to_chance"], b["binomial_p_one_sided"]
    if ratio is None:
        verdict = "NOT_MEASURABLE"
    elif ratio >= 2 and p < 0.05 and b["wrong_position1_share"] >= 0.5:
        verdict = "ORDER_BIAS_STRONG"
    elif ratio < 1.5 or p >= 0.2:
        verdict = "ORDER_BIAS_WEAK"
    else:
        verdict = "MIXED"
    return {"run": run, "verdict": verdict, "rule": (
        "STRONG: wrong-position-1 share >= 0.5 AND ratio to uniform chance >= 2 AND one-sided "
        "binomial p < 0.05; WEAK: ratio < 1.5 OR p >= 0.2; otherwise MIXED"),
        "confound": "position 1 is also the retrieval top hit, so a high share is anchoring OR a "
                    "retrieval-ranked plausible distractor; only a neutral-order run separates them",
        **{k: b[k] for k in ("wrong_select", "wrong_position1", "wrong_position1_share",
                             "uniform_chance_share", "ratio_to_chance", "binomial_p_one_sided")}}


COMPLIANCE = ("COMPLIANT_GENERAL", "COMPLIANT_SPECIFIC", "UNSTATED_QUALIFIER_ASSUMED",
              "OTHER_SELECTOR_ERROR", "CORRECT_MULTI_ACCEPTABLE", "NONE")


def compliance(sem: CaseSnapshot, result: BenchmarkResult) -> dict:
    """Deterministic heuristic of the 'do not assume unstated qualifiers' rule."""
    user_q = qualifiers(sem.case.intent_text)
    texts = {c.candidate_ref: c.canonical_text for c in sem.candidates}
    accepted = set(sem.case.acceptable_candidate_refs)
    review = None
    if result.decision == "NONE":
        label = "NONE"
    elif result.outcome.value in ("INVALID_OUTPUT", "MODEL_ERROR", "TIMEOUT"):
        label, review = "OTHER_SELECTOR_ERROR", "invalid/error output"
    else:
        sel_q = qualifiers(texts.get(result.selected_candidate_ref))
        unstated = sel_q - user_q
        acc_q = set().union(*(qualifiers(texts.get(r)) for r in accepted)) if accepted else set()
        if result.correct:
            if len(accepted) > 1:
                label = "CORRECT_MULTI_ACCEPTABLE"
            elif sel_q & user_q:
                label = "COMPLIANT_SPECIFIC"
            else:
                label = "COMPLIANT_GENERAL"
                if unstated:
                    review = "correct under Gold although the selection carries an unstated qualifier"
        elif unstated and not (unstated <= acc_q):
            label = "UNSTATED_QUALIFIER_ASSUMED"
        else:
            label = "OTHER_SELECTOR_ERROR"
            if unstated:
                review = "wrong selection with an unstated qualifier the accepted answer also carries"
    return {"label": label, "review_reason": review}


def compliance_table(sem_by: Mapping[str, CaseSnapshot], ids: Sequence[str],
                     results: Mapping[str, BenchmarkResult]) -> dict:
    rows = {i: compliance(sem_by[i], results[i]) for i in ids if i in results}
    counts = {k: 0 for k in COMPLIANCE}
    for r in rows.values():
        counts[r["label"]] += 1
    return {"counts": counts, "cases": len(rows),
            "unstated_qualifier_assumed_ids": sorted(
                (i for i, r in rows.items() if r["label"] == "UNSTATED_QUALIFIER_ASSUMED"), key=_case_key),
            "review_queue": {i: r["review_reason"] for i, r in sorted(rows.items(), key=lambda x: _case_key(x[0]))
                             if r["review_reason"]},
            "rows": rows}


# ── 4. blind 471/472 re-adjudication packet ────────────────────────────────

def blind_records(snapshots: Mapping[str, CaseSnapshot], case_ids: Sequence[str]) -> list[dict]:
    """All eligible candidates; anonymous labels; neutral hash order."""
    records = []
    for cid in case_ids:
        snap = snapshots[cid]
        cands = snap.selector_candidates()
        refs = [c.candidate_ref for c in cands]
        if len(refs) != len(set(refs)):
            raise SystemExit(f"{cid}: duplicate candidate")
        ordered = sorted(cands, key=lambda c: adj._hash(cid, c.candidate_ref))
        records.append({"case_id": cid, "intent_text": snap.case.intent_text,
                        "candidates": [{"label": label, "question": c.canonical_text,
                                        "answer": c.answer_text}
                                       for label, c in zip(adj._labels(len(ordered)), ordered)],
                        "candidate_view_complete": True, "candidate_count": len(ordered)})
    return records


REVIEW_README = """# Blind review — two cases, all candidates ({schema})

Read `review-packet.md` (or `review-cases.jsonl`), record one row per case in
`review-template.csv`. Every eligible candidate is listed once, with an anonymous label;
the listing order is a fixed hash order and carries no meaning.

`blind_decision` values:
- SELECT_ACCEPTABLE + `acceptable_labels` (e.g. `B` or `B D`): the candidate(s) meet the
  user's practical need; several labels = several acceptable answers for ONE request.
- EXPECT_NONE: none of the listed candidates reasonably meets the need.
- EXCLUDE_AMBIGUOUS: the message is too unclear to judge.
- CONTENT_REVIEW_REQUIRED: the request is clear but the knowledge-base content is deficient.
- RETRIEVAL_OR_KB_MAPPING_REVIEW: the right answer is not among the listed candidates.

Judge question AND answer against the user's practical need; exact wording is not required,
a shared topic alone is not enough. Do not assume a qualifier the user did not state; if the
user states one explicitly, the more specific candidate is natural.
"""


def review_markdown(records: Sequence[dict]) -> str:
    lines = [REVIEW_README.format(schema=REVIEW_SCHEMA_VERSION), ""]
    for rec in records:
        lines += [f"## Case {rec['case_id']} — {rec['candidate_count']} candidates", "",
                  f"> {rec['intent_text']}", ""]
        for cand in rec["candidates"]:
            lines += [f"**{cand['label']}.** {cand['question']}", "", cand["answer"], ""]
    return "\n".join(lines)


def review_template_csv(records: Sequence[dict]) -> str:
    return adj.followup_template_csv(records)


def review_violations(records: Sequence[dict], csv_text: str, md_text: str,
                      forbidden_refs: Sequence[str] = ()) -> list[str]:
    readme = REVIEW_README.format(schema=REVIEW_SCHEMA_VERSION)
    problems = adj.followup_visible_violations(records, csv_text, md_text.replace(readme, ""), readme)
    blob = json.dumps(list(records), ensure_ascii=False) + csv_text + md_text
    for ref in forbidden_refs:
        if ref in blob:
            problems.append(f"record ref {ref!r} visible")
    return problems


def review_fingerprint(snapshot_fp: str, records: Sequence[dict]) -> str:
    return fingerprint({"schema": REVIEW_SCHEMA_VERSION, "snapshot": snapshot_fp,
                        "cases": [[r["case_id"], sha256_text(json.dumps(
                            r, ensure_ascii=False, sort_keys=True))] for r in records]})


# ── 5. order experiment conditions ─────────────────────────────────────────

def condition_requests(snap: CaseSnapshot, prompt_text: str) -> dict:
    """(system, user) for ORIGINAL_ORDER and NEUTRAL_ORDER, plus the proof that
    they differ only in candidate order."""
    base = snap.selector_candidates()
    out = {}
    for name, order in (("ORIGINAL_ORDER", PRODUCTION_ORDER), ("NEUTRAL_ORDER", NEUTRAL_ORDER)):
        cands = order_candidates(base, case_id=snap.case.case_id, order=order)
        _sys, user = build_model_input(snap.case.intent_text, cands)
        out[name] = {"system": prompt_text, "user": user,
                     "order": [c.candidate_ref for c in cands]}
    o, n = (json.loads(out[k]["user"]) for k in ("ORIGINAL_ORDER", "NEUTRAL_ORDER"))
    canon = lambda items: sorted(json.dumps(i, ensure_ascii=False, sort_keys=True) for i in items)
    out["only_order_differs"] = (
        out["ORIGINAL_ORDER"]["system"] == out["NEUTRAL_ORDER"]["system"]
        and {k: v for k, v in o.items() if k != "candidates"} == {k: v for k, v in n.items() if k != "candidates"}
        and canon(o["candidates"]) == canon(n["candidates"]))
    out["order_changed"] = out["ORIGINAL_ORDER"]["order"] != out["NEUTRAL_ORDER"]["order"]
    return out


def informative(order_info: dict, row: dict, sem: CaseSnapshot) -> dict:
    """Does the neutral order change the relative order of the first accepted
    candidate and the first non-accepted qualifier-pair candidate?"""
    accepted = set(sem.case.acceptable_candidate_refs)
    pair_refs = {r for p in row["pairs"] for r in (*p["specific"], *p["general"])}
    rivals = pair_refs - accepted
    orig, neut = original_order(sem), neutral_order(sem)

    def first(order, pool):
        return next((r for r in order if r in pool), None)

    a_o, r_o, a_n, r_n = first(orig, accepted), first(orig, rivals), first(neut, accepted), first(neut, rivals)
    if not (a_o and r_o):
        return {"informative": False, "reason": "no accepted/rival qualifier pair in the candidate set"}
    flip = (orig.index(a_o) < orig.index(r_o)) != (neut.index(a_n) < neut.index(r_n))
    pair_first_moves = orig[0] in pair_refs and neut[0] != orig[0]
    return {"informative": flip or pair_first_moves,
            "relative_order_flips": flip, "pair_member_leaves_position1": pair_first_moves,
            "position1_original": orig[0], "position1_neutral": neut[0],
            "rival_before_accepted_original": orig.index(r_o) < orig.index(a_o),
            "rival_before_accepted_neutral": neut.index(r_n) < neut.index(a_n)}


# ── 6. contamination accounting / final validation proposal ────────────────

def contamination(primary_ids: Sequence[str], evaluable_ids: Sequence[str],
                  source_of: Mapping[str, str], groups: Mapping[str, Sequence[str]]) -> dict:
    used = set().union(*map(set, groups.values()))
    used_sources = {source_of[i] for i in used if i in source_of}
    remaining = [i for i in evaluable_ids if i not in used and source_of.get(i) not in used_sources]
    return {"full_frozen_primary_cases": len(primary_ids),
            "semantic_evaluable_primary_cases": len(evaluable_ids),
            "groups": {k: len(set(v)) for k, v in groups.items()},
            "union_used_cases_dedup": len(used),
            "union_used_sources_dedup": len(used_sources),
            "remaining_candidate_pool": len(remaining),
            "remaining_ids": sorted(remaining, key=_case_key),
            "rule": "a case is used if it is in any group OR shares a source query with a used case"}


def final_validation_proposal(pool: Sequence[str], sem_by: Mapping[str, CaseSnapshot],
                              core_qualifier_ids: set, qualifier_ids: set, size: int) -> dict:
    """Model-independent stratified sample from the unused pool (proposal only).

    Strata (first match): qualifier_core (qualifier-sensitive and the Gold is a
    qualifier-pair member), multi_acceptable, qualifier_other, other."""
    def stratum(i):
        if i in core_qualifier_ids:
            return "qualifier_core"
        if sem_by[i].case.multi_acceptable:
            return "multi_acceptable"
        if i in qualifier_ids:
            return "qualifier_other"
        return "other"

    by: dict[str, list[str]] = {}
    for i in pool:
        by.setdefault(stratum(i), []).append(i)
    key = lambda i: sha256_text(f"{FINAL_VALIDATION_VERSION}|{i}")
    chosen = sorted(by.get("qualifier_core", []), key=key) + sorted(by.get("multi_acceptable", []), key=key)
    rest = sorted(by.get("qualifier_other", []) + by.get("other", []), key=key)
    chosen += rest[:max(0, size - len(chosen))]
    chosen = sorted(chosen, key=_case_key)
    return {"version": FINAL_VALIDATION_VERSION,
            "status": "PROPOSED — ids not frozen, not adjudicated, no model output exists for them",
            "target_size": size,
            "pool_by_stratum": {k: len(v) for k, v in sorted(by.items())},
            "proposed_size": len(chosen),
            "proposed_by_stratum": {k: sum(1 for i in chosen if stratum(i) == k) for k in sorted(by)},
            "first_candidate_correct_in_proposal": sum(1 for i in chosen if (
                sorted(sem_by[i].candidates, key=lambda c: c.order)[0].candidate_ref
                in sem_by[i].case.acceptable_candidate_refs)),
            "selection_rule": "all qualifier_core and multi_acceptable cases of the unused pool, filled to "
                              "the target size with the remaining cases in "
                              f"sha256('{FINAL_VALIDATION_VERSION}|case_id') order",
            "proposed_ids": chosen,
            "proposed_ids_fingerprint": fingerprint({"version": FINAL_VALIDATION_VERSION, "ids": chosen})}


def csv_rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))
