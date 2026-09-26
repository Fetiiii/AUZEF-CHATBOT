"""Controlled Intent Analyzer load run (experiment/qualification only).

Runs INSIDE the backend container (stdin) and drives the production analyzer
path per logical call:

    CircuitBreaker.acquire -> analyze_intents_with_result -> _availability_kind
    -> CircuitBreaker.record

with a model/reasoning override bound per run via the adapter's own
``_bind_configs``. Nothing is written to the database or registry; production
config, prompt and parser are unchanged. The breaker is a fresh instance with
the production BreakerConfig (env), so the process-global app breaker is never
touched. Output: one ``ANALYZER_LOAD`` JSON line per logical call and a final
``ANALYZER_LOAD_SUMMARY`` line. No prompt, answer, provider message or secret
is printed.

Usage (host):
  docker exec -i auzef_backend python - '<json config>' < analyzer-load.py
  config: {"model": "openai/gpt-6-luna", "reasoning_effort": "none",
           "calls": 60, "concurrency": 2, "interval_seconds": 1.0,
           "cases": [{"current": "...", "previous": ["..."]}, ...]}
"""
import json
import math
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from core.database import SessionLocal
from core.deps import get_llm_provider
import services.llm_provider as llm_provider
from services.answer_pipeline import _availability_kind
from services.circuit_breaker import BreakerConfig, CircuitBreaker, breaker_key
from services.llm_config import LLMCapability, ReasoningEffort
from services.llm_types import LLMOutcomeStatus

cfg = json.loads(sys.argv[1])
calls, concurrency = int(cfg["calls"]), int(cfg.get("concurrency", 1))
interval = float(cfg.get("interval_seconds", 1.0))
cases = cfg["cases"]
if not cases or calls < 1 or not 1 <= concurrency <= 30:
    raise SystemExit("invalid config")

db = SessionLocal()
try:
    managed = get_llm_provider(db)
finally:
    db.close()
client = getattr(managed, "_clients", {}).get(LLMCapability.INTENT_ANALYZER, managed)
base = client.configs
analyzer_cfg = replace(
    base.intent_analyzer,
    model=cfg.get("model") or base.intent_analyzer.model,
    reasoning_effort=(ReasoningEffort(cfg["reasoning_effort"]) if cfg.get("reasoning_effort") else None),
)
bound = llm_provider._bind_configs(client, replace(base, intent_analyzer=analyzer_cfg))
breaker = CircuitBreaker(config=BreakerConfig.from_env())
key = breaker_key(LLMCapability.INTENT_ANALYZER.value, analyzer_cfg.provider, analyzer_cfg.model,
                  analyzer_cfg.fingerprint)
lock = threading.Lock()
rows = []
t0 = None


def one(index):
    # Absolute schedule: wave k starts at t0 + k*interval regardless of how
    # long earlier calls took (a relative sleep would stack across a worker).
    time.sleep(max(0.0, t0 + interval * (index // concurrency) - time.monotonic()))
    case = cases[index % len(cases)]
    permit = breaker.acquire(key)
    row = {"call": index + 1, "case": index % len(cases), "started_unix_ms": round(time.time() * 1000)}
    if not permit.allowed:
        row.update(result="circuit_skipped", circuit_state=permit.state_before.value)
    else:
        started = time.perf_counter()
        result = bound.analyze_intents_with_result(case["current"], tuple(case.get("previous") or ()))
        logical_ms = (time.perf_counter() - started) * 1000
        record = breaker.record(permit, _availability_kind(result.status))
        inv = result.invocation
        meta = inv.metadata if inv else None
        row.update(
            result=result.status.value,
            analyzer_success=result.status is LLMOutcomeStatus.SUCCESS,
            failure_category=inv.failure_category if inv else None,
            retry_count=meta.retry_count if meta else None,
            physical_attempts=(meta.retry_count + 1) if meta and meta.retry_count is not None else None,
            logical_latency_ms=round(logical_ms, 1),
            invocation_latency_ms=round(inv.latency_ms, 1) if inv else None,
            final_attempt_latency_ms=(round(meta.attempt_latency_ms, 1) if getattr(meta, "attempt_latency_ms", None) else None),
            probe=permit.probe, circuit_state_after=record.state_after.value, transition=record.transition,
        )
    with lock:
        rows.append(row)
    print("ANALYZER_LOAD " + json.dumps(row), flush=True)


t0 = time.monotonic()
with ThreadPoolExecutor(max_workers=concurrency) as pool:
    list(pool.map(one, range(calls)))


def pct(values, p):
    values = sorted(v for v in values if v is not None)
    return round(values[math.ceil(p / 100 * len(values)) - 1], 1) if values else None


executed = [r for r in rows if r["result"] != "circuit_skipped"]
ok = [r for r in executed if r.get("analyzer_success")]
summary = {
    "model": analyzer_cfg.model, "reasoning_effort": cfg.get("reasoning_effort"),
    "adapter_body_retry_support": hasattr(llm_provider, "ProviderBodyError"),
    "breaker": {"failure_threshold": breaker.config.failure_threshold, "cooldown_seconds": breaker.config.cooldown_seconds},
    "logical_calls": len(rows),
    "executed_logical_calls": len(executed),
    "physical_provider_attempts": sum(r.get("physical_attempts") or 0 for r in executed),
    "rate_limit_final": sum(r.get("failure_category") == "RATE_LIMIT" for r in executed),
    "retry_count_total": sum(r.get("retry_count") or 0 for r in executed),
    "calls_with_retry": sum((r.get("retry_count") or 0) > 0 for r in executed),
    "retry_exhausted": sum(r.get("result") == "model_error" and (r.get("retry_count") or 0) > 0 for r in executed),
    "failure_categories": {c: sum(r.get("failure_category") == c for r in executed)
                           for c in sorted({r.get("failure_category") for r in executed if r.get("failure_category")})},
    "circuit_open_count": sum(r.get("transition") == "opened" for r in executed),
    "circuit_reopen_count": sum(r.get("transition") == "reopened" for r in executed),
    "circuit_skip_count": sum(r["result"] == "circuit_skipped" for r in rows),
    "analyzer_success_rate": round(len(ok) / len(rows), 4),
    "degraded_rate": round(1 - len(ok) / len(rows), 4),
    "invalid_output": sum(r.get("result") == "invalid_output" for r in executed),
    "logical_latency_ms_p50_p95": [pct([r.get("logical_latency_ms") for r in executed], 50),
                                   pct([r.get("logical_latency_ms") for r in executed], 95)],
    "final_attempt_latency_ms_p50_p95_success": [pct([r.get("final_attempt_latency_ms") for r in ok], 50),
                                                 pct([r.get("final_attempt_latency_ms") for r in ok], 95)],
    "note": "physical attempts = adapter attempts (HTTP- and body-level retries; SDK-internal retries are disabled)",
}
print("ANALYZER_LOAD_SUMMARY " + json.dumps(summary), flush=True)
