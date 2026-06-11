# Speako — Merchant Integration Guide (Custom Platform)

How to add Speako's AI shopping assistant **Aria** to a custom ecommerce
platform. Written for a new merchant. Every endpoint, parameter, and field below
matches the Speako backend exactly.

---

## 1. How the integration works

Speako runs as a hosted backend. It does **not** read your database directly —
instead, **Speako calls a small REST API that you expose on your store**, and
renders a chat widget on your site.

```
 Customer ── chats with ──▶  Aria widget (on your site)
                                  │
                                  ▼
                         Speako backend (hosted)
                                  │  calls your REST API
                                  ▼
                    YOUR store API  (/products/search, /cart/add, …)
```

So integration is three phases:

1. **Build 6 REST endpoints** on your store  ← the only real coding work
2. **Register** your store with Speako (self-service onboarding page)
3. **Paste one snippet** into your storefront HTML

---

## 2. PHASE 1 — Build the REST endpoints on your store

Speako calls all endpoints under **one base URL** you choose, e.g.
`https://api.your-store.com`. Each request includes the header:

```
Authorization: Bearer <your-api-key>
```

(You invent this key and give the same value to Speako during onboarding.)

All endpoints must return **JSON** and be reachable over **HTTPS** from the
public internet (Speako's servers must be able to call them).

### 2.1 The 6 essential endpoints (required)

| # | Method & path | What Speako sends | What you return |
|---|---------------|-------------------|-----------------|
| 1 | `GET /products/search` | query params: `q`, `limit`, `in_stock_only` (also optional `category`, `min_price`, `max_price`, `on_sale`) | list of products |
| 2 | `GET /products/{id}` | product id in path | one product object |
| 3 | `GET /cart` | query param: `session_id` | the cart for that session |
| 4 | `POST /cart/add` | body: `session_id`, `product_id`, `quantity`, `variation_id` | updated cart |
| 5 | `POST /cart/remove` | body: `session_id`, `product_id` (or `cart_item_key`) | updated cart |
| 6 | `PUT /cart/update` | body: `session_id`, `product_id`, `quantity` | updated cart |

> **Cart rule:** the cart is identified by the `session_id` Speako passes. Your
> endpoints must store and return a separate guest cart per `session_id`.

### 2.2 Optional endpoints (Speako works without them — features just degrade)

| Method & path | Purpose |
|---------------|---------|
| `GET /categories` | category list |
| `GET /products/{id}/inventory` | stock for a product/variation |
| `GET /products/{id}/variations` | product variants |
| `GET /store/info` | store name, currency, contact |
| `GET /store/policies` | shipping / returns / payment text |
| `GET /orders?customer_email=&limit=` | a customer's recent orders |
| `GET /products/{id}/reviews` / `POST /products/{id}/reviews` | reviews |
| `GET /coupons/best?cart_total=` / `POST /coupons/apply` | discounts |

### 2.3 Required JSON response shapes

**Product object** — return these fields (Speako reads them by these names; if
your field names differ, use the `field_mapping` option — see §2.4):

```json
{
  "id": 101,
  "name": "Blue T-Shirt",
  "price": "19.99",
  "regular_price": "24.99",
  "sale_price": "19.99",
  "on_sale": true,
  "in_stock": true,
  "stock_quantity": 42,
  "image_url": "https://your-store.com/img/shirt.jpg",
  "permalink": "https://your-store.com/products/blue-tshirt",
  "description": "Soft cotton tee",
  "short_description": "Cotton tee",
  "category_slug": "shirts",
  "tags": "summer,cotton"
}
```

**Product list responses** (`/products/search`) may be either a bare array
`[ {...}, {...} ]` **or** wrapped as `{ "products": [...] }` or `{ "data": [...] }`.

**Cart object** (`/cart`, and inside `/cart/add` etc.):

```json
{
  "items": [
    { "product_id": 101, "name": "Blue T-Shirt", "quantity": 2, "price": "19.99" }
  ],
  "item_count": 2,
  "total": "39.98"
}
```

Cart-mutation endpoints (`add` / `remove` / `update`) should return either the
cart directly, or `{ "success": true, "cart": { ...cart... } }`.

### 2.4 If your field names are different

You do **not** have to rename anything in your database. During onboarding (or
in your tenant settings) Speako accepts a **`field_mapping`** that maps your
field names to the names above — e.g. your `title` → `name`, your `cost` →
`price`. Provide your real product JSON and the mapping is straightforward.

### 2.5 Reference implementation (Node.js / Express)

Working example — adapt the `db.*` calls to your real database. The same logic
applies in any language.

```js
const express = require("express");
const router = express.Router();

// Speako sends: Authorization: Bearer <your-api-key>
const API_KEY = process.env.SPEAKO_API_KEY;
router.use((req, res, next) => {
  if (req.headers.authorization !== `Bearer ${API_KEY}`)
    return res.status(401).json({ error: "unauthorized" });
  next();
});

// 1. Search products
router.get("/products/search", async (req, res) => {
  const { q = "", limit = 6, in_stock_only = "true" } = req.query;
  const rows = await db.searchProducts(q, Number(limit), in_stock_only === "true");
  res.json({ products: rows.map(toSpeakoProduct) });
});

// 2. Product details
router.get("/products/:id", async (req, res) => {
  const p = await db.getProduct(req.params.id);
  if (!p) return res.status(404).json({ error: "not found" });
  res.json(toSpeakoProduct(p));
});

// 3. Get cart  (keyed by session_id)
router.get("/cart", async (req, res) => {
  res.json(toSpeakoCart(await db.getCart(req.query.session_id)));
});

// 4. Add to cart
router.post("/cart/add", async (req, res) => {
  const { session_id, product_id, quantity = 1 } = req.body;
  const cart = await db.addToCart(session_id, product_id, quantity);
  res.json({ success: true, cart: toSpeakoCart(cart) });
});

// 5. Remove from cart
router.post("/cart/remove", async (req, res) => {
  const { session_id, product_id } = req.body;
  const cart = await db.removeFromCart(session_id, product_id);
  res.json({ success: true, cart: toSpeakoCart(cart) });
});

// 6. Update quantity
router.put("/cart/update", async (req, res) => {
  const { session_id, product_id, quantity } = req.body;
  const cart = await db.updateCartQty(session_id, product_id, quantity);
  res.json({ success: true, cart: toSpeakoCart(cart) });
});

// ── map YOUR DB fields → Speako's expected names ──
function toSpeakoProduct(p) {
  return {
    id: p.id, name: p.title, price: String(p.price),
    regular_price: String(p.compare_at_price || p.price),
    in_stock: p.stock > 0, stock_quantity: p.stock,
    image_url: p.image, permalink: p.url,
    description: p.description, category_slug: p.category,
  };
}
function toSpeakoCart(c) {
  const items = c?.items || [];
  return {
    items,
    item_count: items.reduce((n, i) => n + i.quantity, 0),
    total: String(c?.total || 0),
  };
}

module.exports = router; // mount so paths resolve under your base URL
```

---

## 3. PHASE 2 — Register your store with Speako

You do **not** edit any `.env` file. Registration is self-service via the
onboarding page.

> **Which URL do I use?**
> - If **Speako is hosted for you (SaaS)** → use the public onboarding URL the
>   provider gave you, e.g. `https://app.speako.com/static/onboard.html`.
> - If **you run Speako yourself** → it is `http://localhost:8000/static/onboard.html`
>   while testing locally. (Start it first with the command in §6.)

### Steps

1. Open the onboarding page (URL above).
2. Fill the form:
   - **Store name** — your store's display name
   - **Email** — your login email (must be unique)
   - **Password** — your dashboard password
   - **Platform** — select **Custom platform**
   - **API base URL** — `https://api.your-store.com`
   - **API key** — the secret your endpoints check (from §2)
3. Click **Test connection**. Speako calls your `/products/search` with
   `query=""` and `limit=1`, then reports:
   - ✅ *"Connection successful. Found N product(s)."* → continue.
   - ❌ *"Connection failed…"* → your endpoints aren't reachable/public. Fix
     Phase 1 first.
4. Click **Create account**. Speako then:
   - creates your merchant account (credentials stored encrypted),
   - enrolls you on the free Starter plan,
   - queues a background sync of your product catalog,
   - returns your **widget snippet** and **tenant id**.

---

## 4. PHASE 3 — Add Aria to your storefront

1. On the success screen, click **Copy snippet**. It looks like:

   ```html
   <!-- Speako AI Shopping Assistant -->
   <script>
     window.wooagent_config = {
       backend_url: "https://app.speako.com",
       tenant_id:   "your-tenant-id",
       store_name:  "Your Store"
     };
   </script>
   <script src="https://app.speako.com/static/wooagent-widget.js" async></script>
   ```

   **Both `<script>` tags are required, in this order.** The first sets the
   config; the second loads the widget that reads it.

2. Paste the snippet into your storefront's **base layout / master template**,
   immediately before the closing `</body>` tag, so it loads on every page.

   - Plain HTML → in your shared footer / every page
   - React / Next.js → `app/layout.tsx` or `index.html`
   - Vue / Angular → `public/index.html`
   - Django / Laravel / Rails → the base template all pages extend

3. Wait 1–5 minutes for the product sync to finish, then refresh your store.
   The **Aria chat bubble** appears at the bottom-right.

4. **Test end-to-end:** open the chat and ask:
   - *"Show me your products."* → products should appear.
   - *"Add the first one to my cart."* → cart should update.

   If both work, the integration is complete.

---

## 5. Important notes

- **`localhost` only works on your own machine.** A `backend_url` of
  `http://localhost:8000` in the snippet will fail for real visitors. For a live
  store the Speako backend must be at a **public URL** (the SaaS domain, or your
  own deployment / `ngrok` for testing). Only `backend_url` changes — your
  `tenant_id` stays the same.
- **Phase 1 is the only hard part.** Phases 2–3 are point-and-click. If your
  store already exposes product/cart APIs, you only need thin wrapper routes (or
  a `field_mapping`) to reshape the JSON — not a rebuild.
- **HTTPS + public reachability** are mandatory for your store API; Speako will
  not call non-public or internal addresses (SSRF protection).

---

## 6. Appendix — only if YOU host the Speako backend

Skip this entirely if Speako is hosted for you as a SaaS.

```bash
# Start backend (Postgres + Redis + app + worker + beat)
docker compose -f infra/docker/docker-compose.dev.yml up -d

# Confirm it is healthy
curl http://localhost:8000/api/v1/health      # → {"status":"ok","redis":true}

# If the app container is down (ERR_CONNECTION_REFUSED), start just the app:
docker compose -f infra/docker/docker-compose.dev.yml up -d app
```

| API path (relative to backend) | Purpose |
|--------------------------------|---------|
| `POST /api/v1/onboard/` | self-service merchant registration |
| `POST /api/v1/onboard/test-connection` | validate store credentials before signup |
| `GET  /api/v1/onboard/lookup?api_key=` | look up tenant id by api key |
| `GET  /static/onboard.html` | the onboarding page |
| `GET  /static/wooagent-widget.js` | the chat widget script |
| `GET  /api/v1/health` | health check |
