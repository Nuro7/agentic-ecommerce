"""
Speako test store — a fake custom ecommerce backend for end-to-end testing.

Implements every REST endpoint the Speako CustomApiClient calls, with in-memory
sample products and per-session carts. Zero dependencies — Python 3 stdlib only.

Run:
    python test_store.py            # listens on 0.0.0.0:9000
    PORT=9000 API_KEY=test-key-123 python test_store.py

Auth: every request must send  Authorization: Bearer <API_KEY>
      (default API_KEY = "test-key-123"; set the same value in Speako onboarding).

Endpoints (all return JSON):
    GET  /products/search?q=&limit=&in_stock_only=
    GET  /products/{id}
    GET  /products/{id}/inventory
    GET  /products/{id}/variations
    GET  /categories
    GET  /store/info
    GET  /store/policies
    GET  /cart?session_id=
    POST /cart/add        body: {session_id, product_id, quantity}
    POST /cart/remove     body: {session_id, product_id}
    PUT  /cart/update     body: {session_id, product_id, quantity}
    GET  /health          (no auth) — quick liveness check
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.getenv("PORT", "9000"))
API_KEY = os.getenv("API_KEY", "test-key-123")

# ── Sample catalog ───────────────────────────────────────────────────────────
PRODUCTS = [
    {"id": 101, "name": "Blue Cotton T-Shirt", "price": "19.99", "regular_price": "24.99",
     "sale_price": "19.99", "on_sale": True, "in_stock": True, "stock_quantity": 42,
     "image_url": "https://picsum.photos/seed/101/400", "permalink": "https://store.test/p/101",
     "description": "Soft 100% cotton tee, unisex fit.", "short_description": "Cotton tee",
     "category_slug": "shirts", "tags": "summer,cotton,unisex"},
    {"id": 102, "name": "Black Hoodie", "price": "44.00", "regular_price": "44.00",
     "sale_price": "", "on_sale": False, "in_stock": True, "stock_quantity": 18,
     "image_url": "https://picsum.photos/seed/102/400", "permalink": "https://store.test/p/102",
     "description": "Fleece-lined pullover hoodie with kangaroo pocket.", "short_description": "Warm hoodie",
     "category_slug": "hoodies", "tags": "winter,fleece"},
    {"id": 103, "name": "Running Shoes", "price": "79.95", "regular_price": "99.95",
     "sale_price": "79.95", "on_sale": True, "in_stock": True, "stock_quantity": 7,
     "image_url": "https://picsum.photos/seed/103/400", "permalink": "https://store.test/p/103",
     "description": "Lightweight breathable runners with cushioned sole.", "short_description": "Runners",
     "category_slug": "shoes", "tags": "sport,running"},
    {"id": 104, "name": "Leather Wallet", "price": "29.50", "regular_price": "29.50",
     "sale_price": "", "on_sale": False, "in_stock": False, "stock_quantity": 0,
     "image_url": "https://picsum.photos/seed/104/400", "permalink": "https://store.test/p/104",
     "description": "Genuine leather bifold wallet, 6 card slots.", "short_description": "Wallet",
     "category_slug": "accessories", "tags": "leather"},
    {"id": 105, "name": "Steel Water Bottle 1L", "price": "15.00", "regular_price": "15.00",
     "sale_price": "", "on_sale": False, "in_stock": True, "stock_quantity": 120,
     "image_url": "https://picsum.photos/seed/105/400", "permalink": "https://store.test/p/105",
     "description": "Insulated stainless steel bottle, keeps drinks cold 24h.", "short_description": "Bottle",
     "category_slug": "accessories", "tags": "eco,steel"},
]
BY_ID = {p["id"]: p for p in PRODUCTS}

CATEGORIES = [
    {"id": "shirts", "name": "Shirts", "slug": "shirts", "count": 1},
    {"id": "hoodies", "name": "Hoodies", "slug": "hoodies", "count": 1},
    {"id": "shoes", "name": "Shoes", "slug": "shoes", "count": 1},
    {"id": "accessories", "name": "Accessories", "slug": "accessories", "count": 2},
]

# session_id -> { product_id: {"product_id","name","price","quantity"} }
CARTS: dict[str, dict] = {}


# ── Cart helpers ─────────────────────────────────────────────────────────────
def cart_view(session_id: str) -> dict:
    items_map = CARTS.get(session_id, {})
    items = list(items_map.values())
    total = sum(float(i["price"]) * i["quantity"] for i in items)
    count = sum(i["quantity"] for i in items)
    return {
        "items": items,
        "item_count": count,
        "total": f"{total:.2f}",
        "subtotal": f"{total:.2f}",
        "is_empty": count == 0,
    }


def cart_add(session_id, product_id, quantity):
    p = BY_ID.get(int(product_id))
    if not p:
        return None
    cart = CARTS.setdefault(session_id, {})
    pid = int(product_id)
    if pid in cart:
        cart[pid]["quantity"] += max(1, int(quantity))
    else:
        cart[pid] = {"product_id": pid, "name": p["name"], "price": p["price"],
                     "quantity": max(1, int(quantity))}
    return cart_view(session_id)


def cart_remove(session_id, product_id):
    cart = CARTS.get(session_id, {})
    cart.pop(int(product_id), None)
    return cart_view(session_id)


def cart_update(session_id, product_id, quantity):
    pid, qty = int(product_id), int(quantity)
    if qty <= 0:
        return cart_remove(session_id, pid)
    cart = CARTS.setdefault(session_id, {})
    if pid in cart:
        cart[pid]["quantity"] = qty
    else:
        cart_add(session_id, pid, qty)
    return cart_view(session_id)


# ── Search ───────────────────────────────────────────────────────────────────
def search(q, limit, in_stock_only, category=None):
    q = (q or "").lower().strip()
    rows = PRODUCTS
    if q:
        rows = [p for p in rows if q in p["name"].lower() or q in p["description"].lower()
                or q in p["tags"].lower()]
    if category:
        rows = [p for p in rows if p["category_slug"] == category]
    if in_stock_only:
        rows = [p for p in rows if p["in_stock"]]
    return rows[: max(1, min(int(limit or 6), 40))]


# ── HTTP handler ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # concise logging
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        return self.headers.get("Authorization", "") == f"Bearer {API_KEY}"

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path.rstrip("/") or "/", parse_qs(u.query)

        if path == "/health":
            return self._send(200, {"status": "ok"})

        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})

        if path == "/products/search":
            rows = search(
                qs.get("q", [""])[0],
                qs.get("limit", ["6"])[0],
                qs.get("in_stock_only", ["true"])[0].lower() == "true",
                (qs.get("category", [None])[0]),
            )
            return self._send(200, {"products": rows})

        if path == "/products":  # paginated bulk fetch (used by product sync)
            return self._send(200, {"products": PRODUCTS})

        m = re.fullmatch(r"/products/(\d+)", path)
        if m:
            p = BY_ID.get(int(m.group(1)))
            return self._send(200, p) if p else self._send(404, {"error": "not found"})

        m = re.fullmatch(r"/products/(\d+)/inventory", path)
        if m:
            p = BY_ID.get(int(m.group(1)))
            if not p:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"in_stock": p["in_stock"], "stock_quantity": p["stock_quantity"]})

        m = re.fullmatch(r"/products/(\d+)/variations", path)
        if m:
            return self._send(200, {"variations": []})

        if path == "/categories":
            return self._send(200, {"categories": CATEGORIES})

        if path == "/cart":
            sid = qs.get("session_id", [""])[0]
            return self._send(200, cart_view(sid))

        if path == "/store/info":
            return self._send(200, {"name": "Speako Test Store", "currency": "USD",
                                    "url": "https://store.test", "email": "hello@store.test"})

        if path == "/store/policies":
            return self._send(200, {"shipping": "Free shipping over $50.",
                                    "returns": "30-day returns.", "payment": "Cards, PayPal."})

        return self._send(404, {"error": f"no route GET {path}"})

    # ---- POST ----
    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        path = urlparse(self.path).path.rstrip("/")
        b = self._body()
        sid = b.get("session_id", "")

        if path == "/cart/add":
            cart = cart_add(sid, b.get("product_id"), b.get("quantity", 1))
            return self._send(200, {"success": cart is not None, "cart": cart or cart_view(sid)})

        if path == "/cart/remove":
            return self._send(200, {"success": True, "cart": cart_remove(sid, b.get("product_id"))})

        return self._send(404, {"error": f"no route POST {path}"})

    # ---- PUT ----
    def do_PUT(self):
        if not self._auth_ok():
            return self._send(401, {"error": "unauthorized"})
        path = urlparse(self.path).path.rstrip("/")
        b = self._body()
        if path == "/cart/update":
            cart = cart_update(b.get("session_id", ""), b.get("product_id"), b.get("quantity", 1))
            return self._send(200, {"success": True, "cart": cart})
        return self._send(404, {"error": f"no route PUT {path}"})


if __name__ == "__main__":
    print(f"Speako test store listening on http://0.0.0.0:{PORT}")
    print(f"API key: {API_KEY}  (send as 'Authorization: Bearer {API_KEY}')")
    print(f"{len(PRODUCTS)} sample products loaded. Ctrl+C to stop.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
