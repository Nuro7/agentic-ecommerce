# Speako Ticket System — Complete Implementation Reference

This document is the single source of truth for how support tickets are
detected, collected, persisted, prioritized, and dispatched in Speako. It is
written to be LLM-friendly: precise, file-anchored, and rule-based so an agent
can extend the system without guessing.

---

## 1. What the system does

When a customer asks Aria for human help (refund, damaged order, "talk to a
human", unresolvable query), Speako:

1. **Detects** the escalation deterministically (keyword match, no LLM).
2. **Collects** contact + name + issue step-by-step via a persisted FSM,
   one field per voice prompt, in the customer's language.
3. **Verifies** phone numbers by reading them back and requiring "yes/correct".
4. **Persists** a `voice_tickets` row with a merchant-facing number (`TK-1001`),
   structured classification (`issue_type`, `order_id`, `product_id`),
   triage `priority` + `heat`, and a sanitized transcript.
5. **Notifies** the merchant's external helpdesk via a `ticket.created` webhook
   (async).
6. **Best-effort** syncs ticket tags + metafields onto the Shopify customer
   (background task, never on the critical path).

Latency contract: the ticket insert + spoken confirmation return in **<1s**.
No blocking LLM call, no blocking network call on the critical path.

---

## 2. File map (all paths under `backend/src/app/`)

| Concern | File |
|---|---|
| FSM states, prompts (7 languages), name sanitization, intake turn handler | `agent/brain/ticket_intake.py` |
| Orchestration: create ticket, priority/heat/issue_type scoring, transcript building, Shopify sync, dedup | `services/ticketing.py` |
| Brain pipeline integration: contact capture (Step 1), FSM dispatch (Step 4c), escalation trigger (Step 5b), SUPPORT_TICKET lock | `agent/brain/core.py` |
| LLM tool-call path (`create_support_ticket` / `request_human_support`) | `agent/brain/tool_dispatch.py` |
| Legacy tool-call path (`request_human_support`) | `agent/tools/base.py` |
| Widget `/greet` first-message name capture | `api/v1/public.py` |
| SQLAlchemy model + status/priority/heat enums | `modules/tickets/models.py` |
| Repository: create, per-tenant number, dedup lookup, list, count, update | `modules/tickets/repository.py` |
| Service: persistence + async helpdesk webhook + webhook_sent flag | `modules/tickets/service.py` |
| Merchant dashboard API | `modules/tickets/router.py` + `schemas.py` + `dependencies.py` |
| DB migrations | `migrations/versions/0021_voice_tickets.py`, `0022_ticket_structured_fields.py`, `0023_ticket_number_heat_notes.py` |
| Tests (52 passing) | `backend/tests/test_ticket_escalation.py` |

---

## 3. End-to-end flow

```
customer says "i need a refund" (text or voice)
        │
        ▼
brain/core.py Step 1
  ├─ sanitize_text() + check_input()          → cleaned_message (PII redacted → [email]/[phone])
  ├─ extract_contact_info(RAW message)        → real email/phone captured server-side, saved to session meta
  └─ extract_customer_name(cleaned_message)   → sanitized name captured (if not already known)

Step 2  parallel: classifier + session state + session meta

Step 4c  if ticket_intake_state != idle → handle_ticket_intake_turn() owns the turn
         (persists new FSM state to session meta; sets fsm_state="SUPPORT_TICKET")

SUPPORT_TICKET LOCK: while FSM active OR fsm_state=="SUPPORT_TICKET",
  product search / fast-intent / LLM are all SKIPPED (never recommend shoes mid-escalation)

Step 5b  if result is None and detect_escalation(cleaned_message):
         ├─ email known  → enter AWAITING_NAME (pending_email set)
         ├─ phone known  → enter VERIFYING_PHONE (pending_phone set)
         └─ no contact   → enter AWAITING_CONTACT

FSM (ticket_intake.py), persisted in session meta under
  ticket_intake_state / ticket_intake_pending / fsm_state:

  AWAITING_CONTACT ──phone──► VERIFYING_PHONE ──"yes/correct"──► AWAITING_NAME
       │  (email)                          ▲   │"no/wrong"            │
       └───────────────────────────────────┘   ▼ (reask)              │
  AWAITING_NAME ──"name"──► AWAITING_ISSUE ──"issue"──► create_support_ticket()
       │ ("skip"/"guest")                                    │
       └──────────────────────────────────────────────────────┘

create_support_ticket() (services/ticketing.py)
  ├─ dedup: reuse open ticket for tenant+session within 60 min (already_exists=true)
  ├─ _collect_customer_context() from session meta
  ├─ sanitize phone (≥10 digits) + name (DOM-noise blacklist)
  ├─ score priority/heat/issue_type/order_id from the customer's OWN issue text
  ├─ INSERT voice_tickets row (get TK-#### number)
  ├─ spawn background Shopify customer sync (fire-and-forget, strong-ref kept)
  └─ return confirmation message ("call you at <phone> shortly")
```

---

## 4. The intake FSM — state by state

States live in `TicketIntakeState` (ticket_intake.py:36).

### AWAITING_CONTACT
- Prompt: "I can raise a support ticket for that right away… could you share
  your email address or phone number?"
- Parse `extract_contact_info`; fall back to `session_contact()` (real values
  captured pre-redaction in brain Step 1 / address FSM).
- **Phone path**: normalize to digits (`normalize_phone_digits`), require
  `len >= 10`, take last 10 digits, transition to `VERIFYING_PHONE`.
- **Email path**: format-verified → transition straight to `AWAITING_NAME`.
- A partial number (`73 72 27`) → `invalid_phone_prompt` (never silently
  persisted).

### VERIFYING_PHONE
- Read back dash-spaced: "Got it. So that's 8-9-4-3-7-3-7-2-2-7, correct?"
- `_CONFIRM_RE` ("yes/yeah/correct/sahi/ha…") → `AWAITING_NAME`.
- `_DENY_RE` ("no/wrong/change/try again…") → back to `AWAITING_CONTACT`.
- Anything else → repeat the read-back.

### AWAITING_NAME
- Prompt: "Who do I have the pleasure of speaking with?"
- `extract_customer_name()` (intro-pattern regex) then `sanitize_customer_name()`.
- **DOM-noise blacklist** (`_DOM_NAME_NOISE`, ticket_intake.py:202): Footer,
  Header, Navigation, Main, Sidebar, Menu, Cart, Checkout, … are NEVER names.
- `_SKIP_NAME_RE` ("skip/guest/no name/naam nahi/venda…") → empty name, proceed
  to issue. (Global cancel is EXEMPTED during this step so "skip" doesn't
  cancel the ticket.)
- Invalid reply → `reask_name_prompt`.

### AWAITING_ISSUE
- Prompt: "Great! Briefly, what issue would you like our team to help you with?"
- The customer's **own words** become `issue_summary` and drive
  priority/heat/issue_type — no chat-history guessing.
- Empty / PII-placeholder / <3 chars → `reask_issue_prompt`.
- Else → `_create_ticket_with(...)` → `create_support_ticket(...)`.

### Cancel
- `_CANCEL_RE` matches ("never mind/cancel/stop/koi baat nahi/venda/nillu…") →
  reset to IDLE. Exempted when in AWAITING_NAME AND `_SKIP_NAME_RE` matches.

Every prompt + re-ask + confirmation has a 7-language table (en, hi, ml, ta,
te, bn, kn). Language comes from `lang` already resolved in brain/core.py.

---

## 5. Escalation trigger (deterministic, pre-LLM)

`detect_escalation(text)` (ticket_intake.py:67):

- **Returns False** if `_ESCALATION_EXCLUDE_RE` matches — informational /
  policy Q&A must NOT escalate ("what is your return policy?", "how long is a
  refund?").
- **Returns False** if `_DAMAGE_MARKETING_RE` matches — "damage-resistant",
  "damage proof" are marketing terms.
- Otherwise **True** if any `_ESCALATION_TOKENS` substring appears. These are
  `_HIGH_PRIORITY_TOKENS ∪ _LOW_PRIORITY_TOKENS` from services/ticketing.py.

Key design: escalation runs **before** the LLM so a mis-routed (e.g. Malayalam)
complaint still escalates deterministically instead of getting a product answer.

---

## 6. Data model — `voice_tickets`

| Column | Type | Notes |
|---|---|---|
| id | String PK | uuid4 |
| tenant_id | String FK→tenants | indexed; RLS column |
| shop_domain | String(255) | merchant store |
| session_id | String(255) | indexed; dedup key |
| customer_name | String(255) | sanitized, DOM-noise filtered |
| customer_phone | String(50) | digits only, ≥10 |
| customer_email | String(255) | lowercased |
| issue_summary | Text | customer's own words, cleaned ≤100 chars |
| transcript_json | JSONB | `{turns: [{role, content}]}`, sanitized |
| priority | String(20) | low/medium/high/urgent (default medium) |
| status | String(20) | open/in_progress/resolved (default open) |
| issue_type | String(50) | damaged_order/wrong_item/missing_item/refund/exchange/delivery_issue/billing/talk_to_human/other |
| order_id | String(100) | extracted |
| product_id | String(50) | from page context |
| priority_reason | String(255) | `keyword:<token>` or `llm` |
| source | String(20) | `llm` or `deterministic` |
| ticket_number | String(20) | `TK-1001`… per-tenant (indexed) |
| heat | String(10) | hot/warm/cold |
| merchant_notes | Text | dashboard staff notes |
| webhook_sent | Boolean | helpdesk delivery flag |
| created_at / updated_at | timestamptz | server defaults |

RLS: enabled + forced, policy `tenant_id = current_setting('app.tenant_id')`.

### Ticket number generation
`TicketRepository._next_ticket_number` (repository.py:16): `MAX(ticket_number)`
numeric suffix per tenant + 1, formatted `TK-####`. Falls back to `TK-1001`.
First ticket for a tenant = `TK-1001`.

---

## 7. Scoring — priority, heat, issue_type, order_id

All deterministic keyword matching in `services/ticketing.py` (no LLM latency).

### Priority (`_detect_priority`, ticketing.py:198)
1. `_URGENT_TOKENS` hit → `urgent` ("urgent", "asap", "immediately", "emergency",
   "fire", "furious", "angry", "manager", "complain", "legal", "sue", "scam",
   "fraud", "stolen", "unacceptable", …)
2. `_HIGH_PRIORITY_TOKENS` → `high` ("refund", "damaged", "broken", "wrong item",
   "missing item", "never received", "return", "cracked", "torn", …)
3. `_LOW_PRIORITY_TOKENS` → `low` ("talk to a human", "representative",
   "customer care", "helpdesk", …)
4. else → `medium`

### Heat (`_detect_heat`, ticketing.py:209)
- priority `urgent` → `hot`
- any `_HOT_TOKENS` (= `_URGENT_TOKENS`) → `hot`
- priority `high` OR any `_WARM_TOKENS` ("refund", "damaged", "wrong item",
  "missing item", "not delivered", "return", "exchange", …) → `warm`
- else → `cold`

### issue_type (`_classify_issue`, ticketing.py:229)
Order matters — first match wins:
`damaged_order` → `wrong_item` → `missing_item` → `refund` → `exchange` →
`delivery_issue` → `billing` → `talk_to_human` → `other`.

### order_id (`_extract_order_id`, ticketing.py:266)
- "order #1234" / "tracking id: ABC" patterns
- bare "#1234" anywhere
- "order 99887766" (digits after order-word) — never matches a word.

### Which text is scored?
When the deterministic intake collected the issue explicitly, the customer's
**`issue_summary` wins** over chat-history inference for summary AND
priority/heat/issue_type/order_id. Otherwise the trigger message + recent user
turns are scored.

---

## 8. Issue summary + transcript cleaning

### `_clean_issue_summary` (ticketing.py:109)
- Drops PII placeholders (`[email]`, `[phone]`, `[card]`, `[pan]`, `[aadhaar]`).
- Strips generated boilerplate after `"context:"` (the old "Context: <blob>"
  artifact is removed).
- Collapses whitespace, strips leading "ok/okay/hi/hello/so/and".
- Caps at **100 chars** (word-safe split).

### `_generate_issue_summary` (ticketing.py:342) — LLM/legacy path only
Deterministic 1-sentence classification from recent user turns:
"Customer is requesting a refund." / "Customer received a damaged or defective
order." / … + optional ≤10-word context snippet. Used only when the intake did
NOT collect an explicit issue.

### Transcript building (`_build_transcript_turns`, ticketing.py:310)
- Last 40 turns; only `user`/`assistant` with non-empty text.
- Drops PII placeholders and artifact markers (`system prompt`, `fallback
  error`, `[trace]`, `[flow]`, `traceback`, JSON-shell strings like `{}`/`None`).
- Content capped at 1000 chars/turn.

---

## 9. Deduplication

`create_support_ticket` reuses an **open** ticket for the same tenant+session
created within `DEDUP_WINDOW_MINUTES = 60` (ticketing.py:164, :487-523).
Returned with `already_exists: true`; no new insert/webhook/shopify-sync.
Only active when `session_id` is a real value (not empty / `"legacy"`).

---

## 10. External helpdesk webhook

`TicketService.create_ticket` (service.py:34) fires `_emit_ticket_created` as a
fire-and-forget `asyncio.create_task` — never blocks the turn.

- URL: tenant `tenants.tickets_webhook_url`, else `GORGIAS_WEBHOOK_URL` env.
- Method: `POST`, timeout 15s.
- Headers: `Content-Type: application/json`, `X-Speako-Event: ticket.created`.
- Payload (`_ticket_payload`, service.py:102):
  `event: "ticket.created"` + `payload: {id, ticket_number, shop_domain,
  session_id, customer_name, customer_phone, customer_email, order_id,
  product_id, issue_type, issue_summary, transcript, priority, heat, status,
  source, created_at}`.
- On HTTP success, `webhook_sent=true` is written back (fresh session).

---

## 11. Shopify customer sync (background)

`_sync_shopify_customer` (ticketing.py:658) runs in a background task:
- Resolve Shopify customer GID by email (Admin API query).
- Add tags: `voice-support-open`, `escalated-from-speako` (merged with existing).
- Write metafields: namespace `speako`, keys `ticket_status`="open" and
  `last_ticket_id`=<ticket_id>.

Strong reference kept in module-level `_BG_TASKS` set so the task isn't GC'd
(ticketing.py:33). Falls back to inline execution when there's no running event
loop (tests).

---

## 12. API (merchant dashboard)

Mount: `api/v1/router.py` → `modules/tickets/router.py` at
`/api/v1/merchant/tickets`.

| Method | Path | Auth | Behavior |
|---|---|---|---|
| GET | `/api/v1/merchant/tickets?shop=&status=&limit=&offset=` | merchant JWT | list (paginated, tenant-scoped). `?shop=` must resolve to the SAME tenant else 403. `status` one of open/in_progress/resolved else 400. |
| PATCH | `/api/v1/merchant/tickets/{id}?shop=` | merchant JWT | update `status` and/or `notes` (merchant_notes). 404 if not found. |

Schemas: `TicketCreate` (internal), `TicketUpdate` (status/notes),
`TicketOut` (full model, `from_attributes`), `TicketListOut {tickets, total, status}`.

---

## 13. Entry points and how each is wired

### Deterministic path (recommended, default)
`brain/core.py` → `handle_ticket_intake_turn()` → `_create_ticket_with()` →
`create_support_ticket(source="deterministic")`. Used whenever the FSM
collected the data.

### LLM tool path (fallback)
- `agent/tools/base.py` `execute_tool("request_human_support")` → legacy
  `escalate_and_sync_shopify_ticket`.
- `agent/brain/tool_dispatch.py` `_create_support_ticket` / `_request_human_support`
  → `create_support_ticket(source="llm")` with `tool_args` for
  customer_email/phone/name + `issue_description` → `trigger_message`.
- Both emit a `show_ticket` UI action:
  `{type: "show_ticket", payload: {ticket_id, ticket_number, priority, heat, message}}`.

Note: `tool_dispatch.py` clears PII placeholders (`[email]`/`[phone]`) from
tool args before calling create — they are not real contact data.

---

## 14. Name sanitization rules (3 layers)

1. **`extract_customer_name`** (ticket_intake.py:358): intro-pattern regex
   ("my name is X", "call me X", "mera naam X", …) → capture group 2.
2. **`sanitize_customer_name`** (ticket_intake.py:329): strip tags/punctuation,
   reject if whole-string in `_DOM_NAME_NOISE`, reject if any single token is
   DOM noise, reject if `_SKIP_NAME_RE` matches, require `_NAME_RE`
   (letters/spaces/'/./- only, 2-40 chars). Returns `""` otherwise.
3. **Final safety net** `_sanitize_customer_name` (ticketing.py:85) inside
   `create_support_ticket` — re-runs the same blacklist + regex before
   persisting (covers LLM tool args too).

Widget `/greet` (public.py:359) and brain Step 1 (core.py:505) both route
name capture through `extract_customer_name` — never raw widget text.

---

## 15. Language support

All prompts, re-asks, confirmations, cancel texts, and created-messages have 7
variants: `en, hi, ml, ta, te, bn, kn`. Helper `_localized(mapping, language)`
falls back to `en`. The read-back verification and the final confirmation
(`ticket_created_with_issue_message`) are localized, including the callback
phrase ("call you at {contact}" / "email you at {contact}").

---

## 16. Guarantees / invariants (do not break these)

- **Never persist an unverified phone.** A phone must survive
  VERIFYING_PHONE read-back confirmation before it reaches the DB; final guard
  drops anything <10 digits in `create_support_ticket`.
- **Never persist DOM noise as a name.** Footer/Header/Navigation/etc. are
  blacklisted in both `ticket_intake.py` and `ticketing.py`.
- **Never block the turn.** Issue scoring is keyword-based; helpdesk webhook is
  a fire-and-forget task; Shopify sync is a background task.
- **The SUPPORT_TICKET lock holds.** While the FSM is active, the brain must
  return the FSM response and never fall through to search/LLM.
- **One field per voice prompt.** Never combine "name and issue" into one ask.
- **PII stays redacted in transcripts.** Placeholders and artifacts are dropped
  before `transcript_json` is written.

---

## 17. Tests

`backend/tests/test_ticket_escalation.py` (52 tests) covers:
- Full FSM flow (phone → verify → name → issue → create) + email variant.
- Phone verification (confirm/deny/repeat), invalid/partial numbers.
- Name sanitization (`TestNameSanitization`), DOM-noise rejection, name-skip.
- Issue summary cleaning (`TestIssueSummaryCleaning`), priority/heat/issue_type
  driven by the stored issue.
- Dedup reusing an open ticket; transcript artifact filtering.
- `show_ticket` action payloads and confirmation echo (ticket # + callback).

Run: `cd backend && python -m pytest tests/test_ticket_escalation.py -q`

---

## 18. Extending the system (guidance for agents)

- **Add an escalation keyword**: edit `_HIGH_PRIORITY_TOKENS` /
  `_LOW_PRIORITY_TOKENS` / `_URGENT_TOKENS` in `services/ticketing.py`.
  Existing tests may assert on specific tokens — add, don't remove.
- **Add a prompt language**: extend every `_*_TEXTS` dict in `ticket_intake.py`
  (and `_TICKET_CREATED_LOCALIZED` in `ticketing.py`). A missing key silently
  falls back to English.
- **Add a state**: add the constant to `TicketIntakeState`, a prompt dict +
  accessor, a branch in `handle_ticket_intake_turn`, and a
  transition in `brain/core.py` Step 5b / Step 4c if it needs a contact-gated
  entry. Persist via `ticket_intake_state`/`ticket_intake_pending`/`fsm_state`.
- **Add a column**: new Alembic migration (0024+), model column in
  `modules/tickets/models.py`, include in `schemas.py` (`TicketCreate`,
  `TicketOut`), webhook payload in `service._ticket_payload`, and repo create.
- **New webhook target**: set `tenants.tickets_webhook_url`; the env fallback
  is `GORGIAS_WEBHOOK_URL`.
- **Scoring change**: keep `_detect_priority` / `_detect_heat` /
  `_classify_issue` deterministic (no LLM) to preserve the <1s latency
  contract.
