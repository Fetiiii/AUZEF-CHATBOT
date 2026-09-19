# Answer Pipeline V2 — Phase 6 Report

## Status and scope

```text
Phase 6 — Model Registry + Admin Control
STATUS: PASS
```

- Branch: `production-readiness`
- Starting HEAD: `a758c0f`
- Open task in scope: **g29** — "Phase 6 model registry / admin model control
  yapılmadı (allowlist, rol, audit, rollback)". This phase implements exactly
  that task. g18 (Calendar backfill) and g02/g26 (no benchmark harness) are
  rollout or Phase 7 items and are **not** closed here.
- **Not changed:**
  - production model, provider, temperature, max tokens and reasoning;
  - candidate K, the selector prompt and the thresholds;
  - bot context, semantic metadata and exact alias;
  - no Phase 7 experiment was started and no live provider call was made.

## Concepts and schema

The three concepts are kept separate, and all four tables are admin-DB owned.
The schema is created by `init_admin_db` and by the reversible
`scripts/migrate_model_registry.py`.

| Table | Concept | Key fields |
|---|---|---|
| `ai_model_registry` | Model definition | `provider` (typed: `openai`/`openrouter`/`gemini`), `model_identifier`, **UNIQUE(provider, model_identifier)**, `enabled`, `allowed_capabilities` (JSON), `supports_structured_output`, `supports_reasoning_effort`, `allowed_reasoning_efforts` (JSON), `qualification_status`, `qualified_at`/`qualified_by`/`qualification_reference`, `created_by`/`updated_by`, timestamps |
| `ai_capability_config` | Capability assignment (PK `capability`) | `model_registry_id` (FK, ON DELETE RESTRICT), `temperature`, `max_tokens`, `reasoning_effort`, `timeout_seconds`, `max_retries`, `structured_output_enabled`, `config_version_id`, `updated_by` |
| `ai_config_version` | Immutable config version (the highest id is active) | `snapshot` (secret-free JSON of both capabilities incl. fingerprint), `change_type` (BOOTSTRAP/UPDATE/ROLLBACK), `previous_version_id`, `source_version_id`, `summary`, `created_by` |
| `ai_config_audit` | Append-only audit | `event_type`, `actor`, `capability`, `model_registry_id`, `old_value`/`new_value` (JSON), `old_version_id`/`new_version_id`, `created_at` |

A Postgres trigger (`ai_append_only_guard`) rejects UPDATE and DELETE on
`ai_config_version` and `ai_config_audit`. Registry rows are only ever
soft-disabled, never deleted, and the RESTRICT foreign key keeps history
references valid. **No table stores API keys.** Provider secrets stay where
they were: the OpenRouter key is read from DB and then `.env`, OpenAI and
Gemini keys from env.

## Validation (backend-authoritative, `services/ai_registry.py`)

**Assignment** (update, rollback and runtime load all run it):

- the model must be `enabled`;
- its qualification must be `QUALIFIED` or `LEGACY_APPROVED`;
- the capability must be in the model's `allowed_capabilities`;
- `supports_structured_output` must be true.

**Structured output.** `supports_structured_output` means the model reliably
follows the strict-JSON contract that Pydantic validates. Both capabilities
rely on that contract today (Phases 2 and 4), so it is not a provider-native
flag. `structured_output_enabled=true` is rejected, because the adapters do
not implement native `response_format` (the Phase 4/5 contract).

**Parameter bounds:**

| Parameter | Allowed |
|---|---|
| Temperature | 0–1, finite |
| `max_tokens`, Intent Analyzer | 200–8192 |
| `max_tokens`, Selector | 24–4096 (the strict JSON needs about 15–20 tokens, so e.g. `1` is rejected) |
| Timeout | unset (provider default) or 1–120 s |
| Retries | unset or 0–5 |

**Reasoning.** A value is allowed only if the model declares
`supports_reasoning_effort` and the value is in `allowed_reasoning_efforts`.
Registering a reasoning-capable model with no levels, or levels without
support, is rejected.

- **Honest limitation:** the adapters still do **not** transmit
  `reasoning_effort` (the Phase 1 contract, pinned by
  `test_openai_adapter_preserves_metadata_and_omits_unsupported_optional_settings`).
- The value is validated, stored and fingerprinted, but sending it to the
  provider is part of the Phase 7 reasoning experiment.
- All seeded models have `supports_reasoning_effort=false`, so in production
  the UI control stays disabled.

**Model definitions:**

- the provider must be one of the three supported ones;
- the identifier has no whitespace and is at most 200 characters;
- the capabilities must be valid, with at least one;
- provider and model identifier are immutable after creation.

## Qualification states

| State | Meaning | Assignable |
|---|---|---|
| `UNTESTED` | Default for every newly registered model; a model is never self-approved | no |
| `QUALIFIED` | An admin supplied a real `qualification_reference` (run or artifact id); `qualified_at` and `qualified_by` are recorded | yes |
| `LEGACY_APPROVED` | **Bootstrap only**: models already used in production before the registry existed. The API rejects setting it | yes |
| `BLOCKED` | Explicitly barred | no |

Two separate guarantees, both tested:

- **Initial models:** the models in production use are bootstrapped with the
  controlled `LEGACY_APPROVED` state.
- **New models:** a newly registered model is always `UNTESTED` and cannot be
  assigned. The API cannot set `LEGACY_APPROVED`, and `QUALIFIED` requires a
  real reference.

No qualification result or benchmark id was fabricated.

## Bootstrap (idempotent, preserves current behavior)

`bootstrap_ai_registry` runs inside `init_system db` (`seed_default_config`),
under the same advisory lock as other writers:

1. Seeds the three Phase 0 code defaults as `LEGACY_APPROVED` with both
   capabilities, structured output and no reasoning:
   - `openai/gpt-4o-mini`;
   - `openrouter/openai/gpt-4o-mini`;
   - `gemini/gemini-2.5-flash-lite`.
2. Only if no config version exists yet and `LLM_PROVIDER` is set: resolves
   the **env** config exactly as Phase 5 does (`resolve_llm_config_set`).
   Any env model override is registered as `LEGACY_APPROVED`. It then writes
   version 1 (BOOTSTRAP) and both capability rows, but only if the managed
   fingerprint equals the env fingerprint.
3. If the env config violates the Phase 6 bounds (e.g.
   `LLM_SELECTOR_MAX_TOKENS=5`), bootstrap is **skipped** with a visible reason
   rather than rewriting the operator's config, and the legacy env path stays
   active.

Re-running creates no duplicate model and no second version (tested with 3×
`init_system db`). The tests show the managed effective config equals Phase 5
for openai, openrouter and gemini, down to the fingerprint:

- Intent Analyzer: `max_tokens` 300;
- Selector: `max_tokens` 32;
- temperature 0, reasoning/timeout/retries unset, native structured output
  off.

The local `.env` sets only `LLM_PROVIDER=openrouter`, which is exactly the
tested case. **The effective model and config do not change on deploy.**

## Effective config resolution, cache and propagation

`services/llm_runtime.py`, used by `core.deps.get_llm_provider`:

| Situation | Status / source | Runtime |
|---|---|---|
| Valid active DB version | `OK` / `DB` (on load) or `CACHE` | `ManagedLLMProvider`: each capability is routed to its own provider client with the DB-managed `EffectiveLLMConfig` |
| No version ever written | `NOT_CONFIGURED` / `ENV_BOOTSTRAP` | Unchanged pre-Phase-6 env/default provider |
| Persisted config invalid (e.g. out-of-band edit) | `CONFIG_INVALID` | No LLM call, **no silent env fallback**. The request goes to the Phase 5 degraded path with the new `ExecutionMode.CONFIG_DEGRADED` and reason `config_invalid`. The admin API shows the status |
| Assigned provider has no API key (e.g. a selector moved to `openai` without `OPENAI_API_KEY`) | `OK` config, but `llm_config_problem` = `provider_key_missing` | No LLM call; `CONFIG_DEGRADED` with reason `provider_key_missing` (never mislabeled as admin OFF). The UI and `GET /config` show `provider_key_configured=false` (a boolean only), and the save response carries `warnings: ["provider_key_missing"]` |
| DB unreachable | `STALE_CACHE` while the last valid snapshot is younger than `AI_CONFIG_MAX_STALE_SECONDS` (default 300), else `CONFIG_UNAVAILABLE` → `CONFIG_DEGRADED` | No model is guessed |

`ManagedLLMProvider` gives each request a shallow copy of the cached SDK client
with the managed configs bound. The client's boot-time env config is never
used on the managed path and never mutated (tested). This fixes the "config
frozen at process start" problem, so **no redeploy or restart is needed**.

**Cache and multi-node propagation:**

- Every node probes the active version id (one primary-key `max(id)` query) at
  most every `AI_CONFIG_CACHE_TTL_SECONDS` (default **5 s**).
- Only when the id changes does it reload and re-validate the snapshot.
- The writing node invalidates its cache immediately.
- **Propagation bound: ≤ 5 s on every app node**, with no new infrastructure.
  The two-node simulation test shows node B is stale at 4.9 s and fresh at
  5.0 s.
- The cache holds only validated effective configs, the version id and
  registry provenance — no secrets.

## Versioning, fingerprint, atomicity, audit, rollback

- **Atomic writes.** Every write (capability update, rollback, registry
  change) runs in **one transaction** under
  `pg_advisory_xact_lock(7106060)`. The new version row, both capability rows
  and the audit row commit together or not at all.
- **Optimistic concurrency.** Config writes carry `expected_version`. A stale
  value returns **409 `stale_version`**, never a silent overwrite (tested
  through the service and the API).
- **Version vs fingerprint.** The config *version* is management and audit
  identity. The *fingerprint* is behavior identity (Phase 1 SHA-256 over
  `EffectiveLLMConfig`). The version is never folded into the fingerprint, so
  Phase 5 breaker keys remain behavior-based. A rollback gets a new version id
  with the old fingerprint.
- **Rollback.** Rollback to vN re-validates the vN snapshot against the
  **current** registry, then writes a new `ROLLBACK` version (for example
  `ROLLBACK v3 → snapshot(v1) → v4`) with its `source_version_id`. History is
  never deleted. A snapshot whose model is now disabled, BLOCKED or no longer
  allowed is rejected (`rollback_invalid`) and the active config is untouched.
- **Active-model protection.** Disabling, BLOCKing, removing the assigned
  capability from, or dropping structured output or the in-use reasoning level
  of a model used by an active assignment is rejected (**409 `model_in_use`**).
  The capability has to be moved first.
- **Audit events:**
  - `AI_CONFIG_BOOTSTRAPPED`, `AI_CONFIG_CHANGED`, `AI_CONFIG_ROLLED_BACK`;
  - `MODEL_REGISTERED`, `MODEL_UPDATED`, `MODEL_DISABLED`, `MODEL_ENABLED`,
    `MODEL_QUALIFICATION_CHANGED`.

  Each records the actor (session email, nullable only when admin auth is not
  enforced), old/new JSON, old/new version and a timestamp. Each is also
  emitted as a structured `ai_config_event=` log line. Tests confirm no secret
  (e.g. a DB-stored OpenRouter key) appears in snapshots or audit.

## Authorization

The existing role hierarchy is reused, so there is no parallel permission
system:

- **view_ai_config → `admin`.** A new middleware rule gives `^/api/ai-config`
  an `admin` floor. Without it, the prefix would have been unauthenticated.
- **manage_ai_config → `super_admin`.** Every write handler also depends on
  `require_ai_config_manager`, which checks the session user's role. This
  stays authoritative even if a route rule is missed.

The middleware is path-only, so this read/write split is as fine-grained as
the current model allows. Tests call the endpoints directly with each role:
unauthenticated → 401, editor → 403 for everything, admin → 200 on reads and
403 on every write, super_admin → writes allowed.

## Admin API (`/api/ai-config`)

| Method + path | Role | Purpose |
|---|---|---|
| `GET /models` | admin | Registry |
| `POST /models` | super_admin | Register a model (always `UNTESTED`) |
| `PATCH /models/{id}` | super_admin | Enable/disable, capabilities, support flags, qualification (`QUALIFIED` requires a reference) |
| `GET /config` | admin | Active version, status, LLM flag, per-capability config, `eligible_model_ids`, bounds, propagation time |
| `PUT /config/{capability}` | super_admin | Update one capability. Body: `expected_version` + `model_registry_id` + typed parameters |
| `GET /config/history` | admin | Version history |
| `POST /config/rollback/{version}` | super_admin | Rollback. Body: `expected_version` |
| `GET /audit` | admin | Audit events |

Request models are Pydantic with `extra="forbid"` and Literal types for
provider, capability and reasoning. A free `"model": "openrouter/whatever"`
field is therefore a **422**, and a capability can only reference a registry
id.

## Admin UI (Angular, `/chatbot/ai-config`)

The page is under a new sidebar item, "AI Model Yapılandırması", visible to
admin and above.

- **Current config.** Intent Analyzer and Selector cards show the provider and
  model dropdown, reasoning, temperature, max tokens, timeout, retries and the
  fingerprint prefix.
  - The dropdown lists **only** eligible registry models: enabled, qualified,
    capability-allowed, structured output, intersected with the backend's
    `eligible_model_ids`. There is no free-text model field.
  - The reasoning control is disabled when the model has no reasoning support
    and otherwise limited to the allowed levels. An invalid level is cleared
    when the model changes.
  - Changing a model asks for a small confirmation, for example: "Selector
    modelini X → Y olarak değiştirmek üzeresiniz."
- **Model registry table.** Display name, provider, model id, enabled,
  capabilities, structured output, reasoning and qualification. super_admin
  also gets enable/disable, Qualify (asks for a reference), Block, and an "add
  model" form that registers the model as UNTESTED.
- **History.** Version, time, actor, type (with `← vN` for rollbacks) and a
  summary. The rollback action asks for confirmation and sends
  `expected_version`.
- **Errors.** API errors are mapped to readable Turkish messages. A 409 stale
  version reloads the page.

Non-super-admins see the page read-only (controls disabled or hidden). The
backend still enforces authorization.

## Circuit breaker integration

- Breaker keys stay `capability:provider:model:fingerprint`, so a config
  switch is isolated automatically: a new fingerprint starts CLOSED even when
  the old one is OPEN.
- When a node's cache activates a new version, it calls
  `on_config_activated`:
  - keys that become newly active are reset;
  - keys of an unchanged capability keep their state;
  - keys no longer active are dropped (bounded memory).
- A rollback to a previously OPEN fingerprint therefore starts CLOSED
  (deterministic, tested).
- The Phase 5 admin OFF → ON reset is unchanged.

## Admin LLM OFF / ON

`LLM_ENABLED` semantics are unchanged and it is managed on the Settings page.
The AI config page shows it read-only.

- While OFF, config can still be changed. The runtime stays `ADMIN_DEGRADED`
  and no provider is called.
- When turned ON, the latest saved version is used (tested).

## Decision trace schema v6

The request section now also records:

- `ai_config_status`, `ai_config_source` (`DB`/`CACHE`/`STALE_CACHE`/
  `ENV_BOOTSTRAP`), `ai_config_version`, `ai_config_error`;
- `ai_capabilities.{intent_analyzer,selector}`: `registry_model_id`, provider,
  model, `qualification_status`, `config_fingerprint`.

These sit next to the existing `effective_configs` and
`ai_config_fingerprint`. Invalid or unavailable config shows up as
`execution.execution_mode = CONFIG_DEGRADED`, never as semantic NONE.

Each metric maps to specific trace fields (one trace per request):

| Question | Field(s) |
|---|---|
| Requests per model | `request.ai_capabilities.{intent_analyzer,selector}.registry_model_id` (+ `.provider`, `.model`) |
| Requests per config version | `request.ai_config_version` |
| NONE rate after a model change | `execution.intents[].resolution == "semantic_none"` grouped by `request.ai_config_version` |
| MULTI rate after a model change | `intent_analyzer.intent_count == 2` grouped by `request.ai_config_version` |
| Which version opened a breaker | `circuit[].transition == "opened"` joined with `request.ai_config_version` (plus `circuit[].config_fingerprint`) |
| Behavior before/after a rollback | the metrics above, split at the `ROLLBACK` version id from `GET /api/ai-config/config/history` |

## Tests

- **`backend/tests/test_model_registry.py`: 51 tests**, covering:
  - registry create, duplicates (service and DB unique constraint), provider,
    capability, identifier and reasoning validation, and qualification rules;
  - ineligible assignments: disabled, UNTESTED, BLOCKED, capability, and
    structured output;
  - capability-aware reasoning;
  - 11 parameter-bound cases;
  - different or same model per capability;
  - bootstrap equality ×3 providers, idempotence, env overrides, out-of-bounds
    skip, and `init_system db` ×3;
  - runtime change without restart, with capability routing and no mutation of
    the boot client;
  - trace provenance;
  - two-node TTL propagation;
  - stale-cache and unavailable DB handling;
  - malformed config → `CONFIG_DEGRADED` (not NONE);
  - rollback creating a new version;
  - invalid rollback being rejected;
  - optimistic concurrency;
  - active-model disable protection and soft-disable;
  - audit content, secret-freedom, and DB-level append-only;
  - breaker isolation and rollback reset;
  - LLM OFF/ON;
  - authorization matrix;
  - API flow (arbitrary model string → 422, unqualified → 400, stale → 409,
    history, rollback, audit);
  - reversible migration plus triggers;
  - a missing provider key on the real client path: save-time warning,
    `provider_key_configured=false`, and `CONFIG_DEGRADED/provider_key_missing`.
- **Updated deliberately:** `test_database_routing.py` (the admin table
  ownership contract now includes the 4 AI tables and proves their physical DB
  routing) and the trace schema assertions (5 → 6). `conftest.py` resets the
  process-global AI config cache between tests.
- **Frontend:** the project has **no Angular test harness** (no spec files and
  no test builder in `angular.json`). Adding Karma or Jasmine would be
  unrelated scope. The UI decision logic lives in a framework-free
  `ai-config.logic.ts`, and `chatbot-web/tests/ai-config.logic.test.ts` runs it
  with `node --test tests/`: **6/6 pass**. The tests cover:
  - eligible-only dropdown and backend-id intersection;
  - disabled, unqualified, BLOCKED, capability and structured-output filtering;
  - reasoning capability-awareness;
  - change confirmation;
  - API-error messages.

  **§62's DOM-level assertions were not run.** Component rendering,
  role-based hiding, the dropdown in the DOM and the rollback confirm dialog
  are verified only by the production build, the logic tests and code review. The backend is the
  authoritative guard for everything the UI hides.

**Results:**

```text
test_model_registry.py:                  51 passed
Full backend, backend-only /app layout:  385 passed, 7 failed (known /deploy/production layout cases)
Full backend, repository-root layout:    392 passed, 0 failed
Phase 5 end state (a758c0f):             334 passed, 7 failed / 341 passed
Frontend logic tests (node --test):      6 passed
Angular production build (node:20):      PASS — new lazy chunk chatbot-ai-config (25.1 kB);
                                         academic-calendar chunk builds (Phase 3 UI unaffected)
```

The build's initial-bundle budget warning (2.14 MB vs 2.00 MB) already exists
at `a758c0f`: the HEAD build reports 2.13 MB with the same warning. This phase
adds about 12 kB to the initial bundle (sidebar entry, route and service
typing). The new page itself is lazy.

Regressions: the Phase 2 (analyzer), Phase 3 (Calendar), Phase 4
(eligibility/selector) and Phase 5 (degraded/circuit) suites all pass.

## Architecture alignment

- §13.1–§13.6 are implemented as written:
  - capability-based config;
  - no hardcoded model on the managed path;
  - allowlist registry;
  - admin role, audit and rollback;
  - a qualification gate.
- The Phase 0 code defaults remain only as the bootstrap source and the
  pre-bootstrap compatibility path.
- No conflict with the source of truth.

## Known limitations

- `reasoning_effort` is validated and stored but not transmitted by the
  adapters (Phase 1 contract); transmission belongs to the Phase 7 reasoning
  experiment.
- The read/write split uses existing roles (`admin` / `super_admin`), not
  fine-grained permissions, because the codebase has no permission table.
- The config cache and circuit breaker are node-local. Propagation is bounded
  at ≤ 5 s by version polling, and there is no push invalidation.
- There is no frontend DOM test harness (see above).
- The stale-cache bound (300 s) and the TTL (5 s) are env-configurable but not
  editable in the UI.
- Registry qualification is a manual gate. Automated qualification runs are
  Phase 7.

## Production rollout checklist (open, operational)

1. **Calendar rows (from Phase 3, g18):** review and backfill
   `academic_year`, `term` and `aliases` on the production
   `academic_calendar` rows.
2. **Calendar config:** set `ACADEMIC_CALENDAR_CURRENT_YEAR` and
   `ACADEMIC_CALENDAR_CURRENT_TERM` (Settings → Calendar). Until then
   Calendar V2 fails closed.
3. **Registry bootstrap:**
   - run `python -m scripts.init_system db` once per environment (the
     `auzef-init` service);
   - confirm `GET /api/ai-config/config` shows `status=OK`, `version=1`;
   - confirm provider `openrouter`, model `openai/gpt-4o-mini`, selector
     `max_tokens` 32 and analyzer `max_tokens` 300 — equal to the pre-deploy
     effective config;
   - if bootstrap reports `env_config_out_of_bounds`, review the env first.
4. **Active config validation:** check a live decision trace for
   `ai_config_source=DB|CACHE` and `ai_config_status=OK` on both app nodes.
5. **Permissions:** make sure only intended operators have `super_admin`
   (manage AI config); `admin` can view it.
6. **Propagation:** after the first admin change, verify both nodes report the
   new `ai_config_version` within 5 s.

## Phase 7 experiment backlog (not started)

- Selector live-model evaluation (reviewed Gold / Exact E2E; g02/g26).
- Reasoning effort low/medium/high (needs adapter transmission of
  `reasoning_effort`).
- Semantic metadata necessity.
- Exact alias behavior.
- Candidate K tuning.
- Bot context necessity.

Phase 7 has not been started.
