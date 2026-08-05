# Speako — Checkout Address Prefill & Premature-Navigation Fix

LLM-friendly implementation guide for the address-prefill feature and the
premature-navigation fix. Read this before touching any of the files below.

---

## 1. Problem statement

A customer on a product page says **"proceed to checkout"**. Aria asks for the
phone number. The expected flow is:

```
PDP → "proceed to checkout" → ask phone → collect/confirm delivery address
    → user verifies ("yes") → THEN redirect to Shopify checkout, pre-filled
```

Two bugs were reported:

1. **Premature navigation** — when the phone number matched a *saved* DB
   address, the address state machine skipped address collection AND
   verification and jumped straight to checkout.
2. **Checkout not auto-filled** — the production store pinned
   `SHOPIFY_API_VERSION=2025-01`, but the reliable prefill mutation
   `cartDeliveryAddressesReplace` only exists in Storefront API **2025-10+**.
   The modern mutation failed silently and checkout opened empty.

A **third bug** was reported from the cart page: typing *"proceed to checkout"*
navigated to checkout **before the phone/address was even asked** (the console
`/private_access_tokens ... 401` is a red herring — see §4.6). Cause: the generic
`append_live_navigation` post-processor matched the word "checkout" and appended
a plain `redirect /checkout` action alongside the FSM's phone prompt.

A **fourth bug** surfaced in the voice flow: after giving the phone number,
Aria answered *"I could not find any products"*. Two causes in the brain gate:

- `core.py` only ran the FSM when **this turn** looked like checkout
  (`_is_checkout_page or _checkout_intent`). A bare phone number matches neither,
  so mid-flow turns fell through to product search. Fixed: once the flow is
  active (`_addr_flow_active`), the FSM owns the turn; the checkout signal is
  only needed to **start** the flow (`core.py:670`).
- Even when the FSM did run, `check_input()` had replaced the phone with
  `[phone]` (PII redaction), so `normalize_phone_digits("[phone]")` was empty and
  the FSM rejected it. Fixed: the FSM receives the pre-redaction sanitized text
  (`_addr_input`, `core.py:670`-era block).

All four are fixed. Details below.

---

## 2. Architecture / data flow

```
PDP widget (backend/static/wooagent-widget.js)
  │  user: "proceed to checkout"
  ▼
POST /api/v1/chat  →  brain pipeline (agent/brain/core.py)
  │  address state machine (agent/brain/address.py) owns the flow:
  │     COLLECTING_PHONE → [saved-address?] → CONFIRMING → (yes) → COMPLETE
  │  emits ui_action(s) in the response:
  │     - {"type":"prefill_address",   "payload": <billing/shipping map>}
  │     - {"type":"redirect_checkout_with_address", "payload":{url,billing,shipping}}
  ▼
widget processAction()  →  prefill_address (widget:2834) → dispatch
       │                →  redirect_checkout_with_address (widget:2847)
       ▼                        │
  goToCheckout() (widget:4243)  ▼
    _addrUsable()? (widget:4186)  prepareShopifyCheckout(addr) (widget:4204)
       └─ NO → sends "I want to checkout now" back to the brain (loop again)
       └─ YES → POST /api/v1/cart/checkout  →  returns real checkoutUrl
                        ▼
        (if Shopify) attach_buyer_identity() binds address to Storefront cart
                        ▼
  widget navigates to the returned checkoutUrl  →  checkout opens PRE-FILLED

In-checkout (speako-checkout/checkout extension):
  Checkout.jsx chat → POST /api/v1/chat → reads data.ui_action / ui_actions[0] /
  actions[0] → applyPrefill() → useApplyShippingAddressChange() fills the form.
```

Key rule: **the checkout redirect is ALWAYS the last step** (only after the
customer confirms the address). The phone-first FSM never emits a redirect
itself except from the CONFIRMING "yes" branch.

---

## 3. Files & responsibilities

| File | Role |
|------|------|
| `backend/src/app/agent/brain/address.py` | Address collection state machine (FSM). Owns phone-first flow, saved-address lookup, confirmation gating, and emits `prefill_address` / `redirect_checkout_with_address` actions. |
| `backend/src/app/agent/brain/text_utils.py` | `normalize_province_code()` / `normalize_country_code()` ISO-2 helpers used everywhere. |
| `backend/src/app/integrations/shopify/client.py` | Shopify client. `replace_cart_delivery_address()` (modern), `_bind_address_legacy()` (fallback), `attach_buyer_identity()`, `_ensure_cart_gid()`. |
| `backend/src/app/api/v1/public.py` | `POST /api/v1/cart/checkout` (`prepare_checkout`) — server-side address bind + real `checkoutUrl`. |
| `backend/static/wooagent-widget.js` | PDP/theme widget. Processes `ui_action`s and navigates. **DO NOT MODIFY WooCommerce paths.** |
| `speako-checkout/extensions/ask-aria-checkout/src/Checkout.jsx` | Shopify checkout extension (block `purchase.checkout.block.render`). Real-time prefill via `useApplyShippingAddressChange`. |
| `speako-checkout/extensions/ask-aria-checkout/shopify.extension.toml` | Extension config; `api_version = "2026-07"`. |
| `backend/tests/unit/test_address_prefill.py` | 19 unit tests covering the FSM, ISO-2 helpers, client binding + the brain live-nav guard + mid-flow routing. |
| `backend/.env`, `backend/.env.worker` | `SHOPIFY_API_VERSION` — **bumped 2025-01 → 2026-07** so the modern mutation is reachable. |

---

## 4. The address FSM (`address.py`)

### 4.1 States

`IDLE → COLLECTING_PHONE → COLLECTING_NAME → COLLECTING_LAST_NAME →
COLLECTING_ADDRESS_LINE1 → COLLECTING_CITY → COLLECTING_STATE →
COLLECTING_PINCODE → COLLECTING_EMAIL → CONFIRMING → COMPLETE`
(+ `COLLECTING_UPDATE_FIELD` / `COLLECTING_UPDATE_VALUE` for returning
customers who want to change one saved detail).

### 4.2 Phone-first entry (`address.py:232-247`)

Entry to the flow requires **either**:
- `page_context.page_type == "checkout"` or URL contains `/checkout`, **or**
- a checkout-intent phrase in the message (`buy it now`, `proceed to checkout`,
  `place order`, `pay now`, …).

From `IDLE` this sets `COLLECTING_PHONE` and asks for the phone first.

### 4.3 Saved-address confirmation (THE FIX #1, `address.py:299-360`)

In `COLLECTING_PHONE`, after normalizing the phone digits:

1. Look up `get_address_by_phone(phone, tenant_id)` (DB).
2. **If a saved address is found** and it has `address_line1 + city + postcode`:
   - copy the saved fields onto `AddressData`,
   - set `addr._using_saved = "1"`,
   - transition to **`CONFIRMING`** (NOT `COMPLETE`),
   - emit `{"type":"prefill_address","payload": addr.to_woocommerce_format()}`,
   - ask the confirm prompt.
   - **No redirect is emitted here.** This is the fix — previously the FSM went
     straight to `COMPLETE` and emitted `redirect_checkout_with_address`,
     jumping to checkout without verification.
3. If no match (or a partial saved record), continue the normal
   phone-first collection: name → address → city → state → pincode → email.

### 4.4 Confirmation gate (`address.py:393-438`)

In `CONFIRMING`:
- **"yes"** (`yes/okay/sure/correct/ha/seri/…`): transition to `COMPLETE`,
  emit **`redirect_checkout_with_address`** with `billing`+`shipping`, and
  persist the address via `save_address(...)` for future lookups.
- **"no"** and `_using_saved == "1"`: go to `COLLECTING_UPDATE_FIELD` so the
  customer can change just one field while keeping the rest of the saved
  details; after the update it returns to `CONFIRMING` and re-emits
  `prefill_address`.
- **"no"** otherwise: restart the full collection from `COLLECTING_NAME`.

### 4.5 Update-value path (`address.py:440-510`)

`COLLECTING_UPDATE_FIELD` detects which field by keyword
(`_detect_address_field`), then `COLLECTING_UPDATE_VALUE` applies it via
`_apply_update_value` (reuses `normalize_province_code` for state) and returns
to `CONFIRMING` with a fresh `prefill_address` action.

### 4.6 The `append_live_navigation` guard (THE FIX #3, `core.py:616-703`, `core.py:1215`)

`append_live_navigation` (`text_utils.py:106`) runs on every turn's
post-processing and drives the real storefront (search/product/cart pages). Its
"Checkout Page Navigation" block (`text_utils.py:303-309`) matches the word
`checkout` and appends `{"type":"redirect","payload":{"url":"/checkout",
"reason":"checkout","delay_ms":1500}}`. When the address FSM claimed the turn
first (it asks for the phone), `ui_actions` is still empty, so that block fired
and the widget received a phone prompt **plus** a `/checkout` redirect → the
customer was navigated to checkout before any address was collected.

Fix: when the address FSM owns the turn, `append_live_navigation` is skipped
entirely — the FSM alone decides checkout navigation. `core.py` tracks
`_addr_fsm_owned` (set `False` at `core.py:665`, `True` when the FSM produced
the result, `core.py:698`) and the call is wrapped in `if not _addr_fsm_owned:`
(`core.py:1221`). The FSM then emits `prefill_address` while collecting, and
only `redirect_checkout_with_address` on confirmation.

> **Red herring:** the console error
> `https://<store>/private_access_tokens?id=...&checkout_type=c1 401 Unauthorized`
> on the checkout page is Shopify **by design** — Apple's Private Access Token
> bot/spam protection returns 401 per spec on every store, even clean ones. It is
> unrelated to this bug and must not be "fixed".

---

## 5. ISO-2 normalization (`text_utils.py`)

All addresses are normalized to ISO codes **before** they reach Shopify, because
`CartSelectableAddressInput` requires valid province/country codes.

- `normalize_province_code(state, country)` (`text_utils.py:627`)
  - India (`IN`) → full state-name mapping (`Kerala → KL`, `Maharashtra → MH`).
  - US → full state-name mapping (`California → CA`, …).
  - Bare 2-letter input trusted + uppercased; otherwise returned raw.
- `normalize_country_code(country, default="US")` (`text_utils.py:648`)
  - Alias table checked FIRST (`USA/US/united states → US`, `india/bharat → IN`,
    `uk/united kingdom/england → GB`, …). Alias check precedes the 2-letter
    pass-through so `uk → GB` (not left as `UK`).
  - Bare 2-letter trusted + uppercased; empty/unknown → `default`.
- `AddressData.to_woocommerce_format()` (`address.py:56`) always includes
  `state_code` and `country_code` keys in addition to the raw `state`/`country`.

---

## 6. Shopify cart binding (`client.py`)

### 6.1 `_ensure_cart_gid(cart_id)` (`client.py:73`)

Prefixes short cart tokens (`c1-abc`) with `gid://shopify/Cart/`; full GIDs
pass through.

### 6.2 `replace_cart_delivery_address(...)` (`client.py:1464`) — modern path

```
mutation CartDeliveryAddressesReplace($cartId: ID!, $addresses: [CartSelectableAddressInput!]!) {
  cartDeliveryAddressesReplace(cartId: $cartId, addresses: $addresses) {
    cart { id checkoutUrl }
    userErrors { field message }
  }
}
```
- Builds a `CartDeliveryAddressInput` (`firstName, lastName, address1, city,
  provinceCode, zip, phone, countryCode`) using `state_code`/`country_code` when
  present; strips empty/null fields. `provinceCode`/`countryCode` must be ISO-2.
- Sends `[{"address": {"deliveryAddress": <input>}, "oneTimeUse": true,
  "selected": true}]`. **`selected: true` is what makes Shopify pre-select the
  address as the buyer's delivery address so the hosted checkout opens
  pre-filled — without it the checkout form stays blank.**
- Returns normalized cart with `checkout_url` + `success=True`, or an empty cart
  dict on failure.
- **On exception → falls through to `_bind_address_legacy()`.**
- Requires Storefront API **2025-10+** (the mutation was introduced in 2025-10;
  on older versions the schema error triggers the legacy fallback).
### 6.3 `_bind_address_legacy(gid, mailing)` (`client.py:1487`) — THE FIX #2

For Storefront API versions **before 2025-10** (`cartDeliveryAddressesReplace`
does not exist there), bind the same address via the deprecated but still
functional field:

```
mutation CartBuyerIdentityUpdate($cartId: ID!, $buyerIdentity: CartBuyerIdentityInput!) {
  cartBuyerIdentityUpdate(cartId: $cartId, buyerIdentity: $buyerIdentity) {
    cart { id checkoutUrl }
    userErrors { field message }
  }
}
```
with `buyerIdentity.deliveryAddressPreferences = [{"deliveryAddress": mailing}]`.

### 6.4 `attach_buyer_identity(...)` (`client.py:1523`)

1. Resolve the session's Storefront cart id.
2. If `address` present → `replace_cart_delivery_address(...)` (which internally
   falls back to the legacy path).
3. If `email`/`phone` present → a separate `cartBuyerIdentityUpdate` with only
   those fields. **Safe because `cartBuyerIdentityUpdate` leaves omitted fields
   unchanged** (verified against Shopify issue tracker / docs — a partial update
   does not wipe a previously-bound address).
4. Returns the normalized cart with live `checkout_url`, or an empty cart dict
   (never raises).

---

## 7. Server handler (`public.py:476`)

`POST /api/v1/cart/checkout` (`prepare_checkout`):
1. Rebuilds the session Storefront cart from the widget's line items.
2. Normalizes the incoming address to ISO-2:
   `state_code = normalize_province_code(raw_state, country)` and
   `country_code = normalize_country_code(raw_country)` (`public.py:523-531`).
3. Calls `attach_buyer_identity(session_id, email, phone, address)`.
4. Returns `{ ok, checkout_url, cart, bound }`.
   - `checkout_url` comes from the bound cart; falls back to the session cart's
     native URL if binding couldn't refresh it. Empty → widget must not navigate.
   - WooCommerce path returns `bound: false` and the widget keeps its client-side
     DOM prefill (unchanged).

---

## 8. Widget action handling (`wooagent-widget.js`)

- `case 'prefill_address'` (widget:2834) → dispatches
  `wooagent_prefill_address` (client-side form fill where the DOM is same-origin).
- `case 'redirect_checkout_with_address'` (widget:2847) → after ~200ms calls
  `prepareShopifyCheckout(addr)`.
- `goToCheckout()` (widget:4243):
  - `_addrUsable()` (widget:4186) → if usable, `prepareShopifyCheckout(addr)`.
  - else sends "I want to checkout now" back to the brain so the FSM collects
    the address first (this is the loop that makes the flow self-correcting).
- `prepareShopifyCheckout(addr)` (widget:4204) → `POST /api/v1/cart/checkout`
  with `{session_id, email, phone, address, lines}`, then navigates to the
  returned `checkout_url`. If `ok` is false, it does **not** navigate.

---

## 9. In-checkout extension (`Checkout.jsx`)

- Extension block `purchase.checkout.block.render`, `api_version = "2026-07"`.
- Real-time prefill path:
  - user chats → `POST {backendUrl}/api/v1/chat` with a session + shop domain.
  - response actions parsed defensively:
    `data.ui_action || ui_actions[0] || actions[0]`.
  - `applyPrefill(action)` maps the WooCommerce-style payload to Shopify fields:
    `first_name → firstName`, `address_1 → address1`, `state_code → provinceCode`,
    `country_code → countryCode`, `postcode → zip`, `phone → phone`.
  - guards: no-op if `applyAddressChange` is unavailable, or if
    `instructions?.delivery?.canSelectCustomAddress === false`.
  - calls `applyAddressChange({ type: 'updateShippingAddress', address })`;
    handles `result.type === 'error'` and thrown exceptions with a visible
    fallback message (never crashes the checkout).
- Note: hosted Shopify checkout prefill is done **server-side** via the cart
  mutation (section 6) because URL params are ignored and the checkout DOM is
  cross-origin. This extension covers the case where the customer is ALREADY on
  the checkout page when they talk to Aria.

---

## 10. Env / API-version requirement

- `cartDeliveryAddressesReplace` requires Storefront API **2025-10 or newer**
  (it was added in the 2025-10 release, effective 2025-10-01; `2025-07` predates
  it and predates its own retirement).
- The mutation must ALSO set `selected: true` on the
  `CartSelectableAddressInput` — without it the address is stored on the cart but
  not pre-selected as the buyer's delivery address, so the hosted checkout still
  opens with a blank Delivery section.
- `backend/.env` and `backend/.env.worker` are now
  `SHOPIFY_API_VERSION=2026-07` (stable, supported, includes the mutation) →
  modern path active.
- The legacy fallback keeps prefill working on any store still pinned to an
  older version.
- **Deployment note:** `.env` is gitignored; the production server's `.env`
  must also be updated to `2026-07` and the app container recreated
  (`up -d app`; restart does NOT reload `.env`).

---

## 11. Tests (`test_address_prefill.py`, 19 tests)

Coverage highlights:
- FSM flow: phone-first entry, spoken/formatted phone normalization,
  saved-address → CONFIRMING + `prefill_address` (no redirect yet),
  confirm-then-redirect with `state_code: KL`, new-customer save on confirm,
  manual confirm for unknown phone.
- ISO-2: `normalize_province_code` (US full names, India, bare/unknown),
  `normalize_country_code` (`uk → GB`, `U.S. → US`, defaults).
- Payloads: `prefill_address` and redirect payloads emit `state_code`/
  `country_code`.
- Client: `_ensure_cart_gid`, modern `cartDeliveryAddressesReplace` payload,
  **legacy fallback** (`deliveryAddressPreferences` sent when the modern
  mutation raises), empty-cart dict when both paths fail.
- **Brain guard (regression):** `test_checkout_intent_does_not_emit_redirect_while_collecting`
  runs `ask_brain` with mocked deps for "proceed to checkout" from a cart page
  and asserts NO `redirect`/`redirect_checkout*` action is emitted and
  `append_live_navigation` is not invoked on an FSM-owned turn.
- **Mid-flow routing (regression):** `test_active_phone_flow_routes_digits_to_fsm_not_search`
  runs `ask_brain` with a session already in `collecting_phone`, sends a bare
  phone number from a non-checkout page, and asserts the FSM consumes it (asks
  for the name) instead of falling through to product search.

Run: `cd backend && python -m pytest tests/unit/test_address_prefill.py -q`
→ `19 passed`. Full unit suite: 38 passed / 3 failed (pre-existing, unrelated:
`test_live_navigation` × 2, `test_voice_architecture`).

---

## 12. Deploy checklist

Current git state: all fixes are committed and pushed (`0991a61` confirm-first +
legacy fallback, `36c0126` extension, `b82f793` live-nav guard). What remains is
**production deployment** — the running backend still has the old behaviour:

1. Backend deploy: on the production host (`/home/ubuntu/agentic-ecommerce`),
   pull the latest `main`, set production `.env` `SHOPIFY_API_VERSION=2026-07`,
   and recreate the app container (`up -d app`; restart does NOT reload `.env`).
   This ships the fixes: confirm-first FSM, address `selected:true` + legacy
   fallback, the live-nav guard, and the mid-flow routing + PII-redaction fixes.
2. Extension deploy: `cd speako-checkout && shopify app deploy` (registers the
   in-checkout block that was previously committed as files only).
3. Widget: re-register the script tag after any JS change (Shopify caches it).
4. Verify: from a fresh session on the store, "proceed to checkout" should ask
   for the phone (no navigation), collect/confirm the address, then redirect to
   a pre-filled hosted checkout. The `private_access_tokens` 401 in the console
   is expected Shopify bot-protection noise (see §4.6).
