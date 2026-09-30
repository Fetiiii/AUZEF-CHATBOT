# Manual Stage G checklist (A-01), internal pilot, amendment 6

**Status: OPEN.** This checklist has not been run by an authorised person. Automated preflight, smoke and unit tests do **not** replace it.

- **Who:** an authorised AUZEF tester plus the release owner.
- **Where:** the pilot environment itself (pilot host, pilot DB), in a normal browser, with no developer tools needed.

## How to record results

Write one row per item: the item ID, PASS or FAIL, the UTC time, the tester's initials, and a note. Add a screenshot for any FAIL.

For every chat item, also record the widget's `conversation_id`. You can find it in the network response if needed; the operator can then find the matching `decision_trace` through `request.conversation_id` / `request.user_message_id`.

## Before starting (operator)

| ID | Check |
|---|---|
| G0.1 | `python -m scripts.internal_pilot_preflight pilot --scope live` prints `PASS` in the pilot backend container. |
| G0.2 | `python -m scripts.internal_pilot_preflight pilot --scope static` prints `PASS` in the release checkout. |
| G0.3 | `python -m scripts.pilot_db_validation` prints `PASS` on the pilot DB. |
| G0.4 | The backend log source and the edge access log are both reachable (see the runbook fill-in table). |

## Widget

| ID | Step | Expected |
|---|---|---|
| G1.1 | Open the page that embeds the widget; open and close the widget. | Opens and closes without errors or layout breaks, on desktop and mobile width. |
| G1.2 | Type 501 characters. | The input stops at 500 characters. |
| G1.3 | Send a message and watch the waiting state. | Typing dots appear while waiting. There is no technical or model wording. |
| G1.4 | Rate an answer (1–5 stars). | The rating is accepted once. |

## Login and session (admin panel)

| ID | Step | Expected |
|---|---|---|
| G2.1 | Log in as an editor, an admin and a super_admin. | Each role sees only its own menu items. |
| G2.2 | Log out; press browser Back. | Protected pages are not shown without logging in again. |
| G2.3 | Try a wrong password several times. | Generic error, no user enumeration, and the rate limit applies. |

## Normal chat

| ID | Step | Expected |
|---|---|---|
| G3.1 | "Öğrenci belgemi nasıl alabilirim?" | Answer about AKSİS/e-Devlet document requests. |
| G3.2 | Ask 3 everyday questions of your own choice. | A relevant answer, or a clear "bilgim bulunmuyor". Never an unrelated answer. |
| G3.3 | Ask a question the KB clearly answers with a negation, e.g. about a programme that does not exist. | Answer that corrects the premise (e.g. "…bulunmamaktadır"). |

## Context follow-up

| ID | Step | Expected |
|---|---|---|
| G4.1 | "Yatay geçiş başvurusu nasıl yapılıyor?", then in the same conversation "Bunun için hangi belgeler gerekiyor?" | The second answer stays on the transfer topic. |
| G4.2 | Start a new conversation and send only "Bunun için hangi belgeler gerekiyor?" | No leakage from the previous conversation. |

## Calendar

| ID | Step | Expected |
|---|---|---|
| G5.1 | "Final sınavları ne zaman?" | Date of the **current** term's final. **Known risk from the 2026-09-29 smoke:** the spring final was selected while the current term is GUZ; record the result carefully. |
| G5.2 | "Bahar dönemi final sınavı ne zaman?" | Spring final date. |
| G5.3 | "2025-2026 güz final tarihleri?" | Handled as a past year, with no wrong current-year answer. |

## NONE and degraded behaviour

| ID | Step | Expected |
|---|---|---|
| G6.1 | A question outside the KB, e.g. "Kampüste bisiklet park yeri var mı?" | "Bu konuda bilgim bulunmuyor." or suggestions. No invented answer. |
| G6.2 | Operator only, on a pilot-like environment and never on live pilot traffic: temporarily disable `LLM_ENABLED` from the admin panel. | Widget still answers from the degraded path. Re-enable, then preflight G0.1 is PASS again. |

## Maintenance mode (if used in the pilot)

| ID | Step | Expected |
|---|---|---|
| G7.1 | Enable maintenance mode from the admin panel. | Widget shows the maintenance message. The admin panel stays usable. |
| G7.2 | Disable maintenance mode. | Widget answers again. |

## Critical admin flows

| ID | Step | Expected |
|---|---|---|
| G8.1 | QnA: edit one test record's alias, then revert. | Saved, audited, and search reflects it after reindex. **Do not change pilot KB content** (KB `b53e0458…` is frozen); use a test record or skip. |
| G8.2 | Calendar CRUD: view the 2026-2027 rows. | Terms GUZ/BAHAR are listed; current year/term are shown correctly. |
| G8.3 | AI config page: view it (super_admin). | Analyzer and selector are `openai/gpt-6-luna`, reasoning none, version shown. **Do not change it.** |
| G8.4 | Conversations/statistics pages. | Pilot conversations are listed; no SMS/TC data is visible in plain text. |

## Sign-off

| Role | Name | Date | Result |
|---|---|---|---|
| Tester | | | |
| Release owner | | | |

A-01 can only be closed with this table filled in and every FAIL resolved or explicitly accepted by the release owner.
