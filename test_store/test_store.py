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

# ── Sample catalogs ──────────────────────────────────────────────────────────
# Each variant has DISTINCT product ids and names so multi-tenant isolation is
# provable: a tenant backed by "beta" must never surface an "alpha" product.
# Select with env STORE_VARIANT=alpha|beta|gamma (default alpha).

def _p(pid, name, price, cat, desc, tags, stock=10, regular=None, sale="", on_sale=False):
    return {
        "id": pid, "name": name, "price": str(price),
        "regular_price": str(regular if regular is not None else price),
        "sale_price": str(sale), "on_sale": on_sale,
        "in_stock": stock > 0, "stock_quantity": stock,
        "image_url": f"https://picsum.photos/seed/{pid}/400",
        "permalink": f"https://store.test/p/{pid}",
        "description": desc, "short_description": desc[:40],
        "category_slug": cat, "tags": tags,
    }


_CATALOGS = {
    # Alpha — apparel (ids 101-105)
    "alpha": [
        _p(101, "Blue Cotton T-Shirt", "19.99", "shirts", "Soft 100% cotton tee, unisex fit.", "summer,cotton", 42, "24.99", "19.99", True),
        _p(102, "Black Hoodie", "44.00", "hoodies", "Fleece-lined pullover hoodie.", "winter,fleece", 18),
        _p(103, "Running Shoes", "79.95", "shoes", "Lightweight breathable runners.", "sport,running", 7, "99.95", "79.95", True),
        _p(104, "Leather Wallet", "29.50", "accessories", "Genuine leather bifold wallet.", "leather", 0),
        _p(105, "Denim Jacket", "59.00", "jackets", "Classic blue denim jacket.", "denim,casual", 12),
    ],
    # Beta — electronics (ids 201-205)
    "beta": [
        _p(201, "Wireless Earbuds Pro", "89.00", "audio", "Noise-cancelling true-wireless earbuds.", "audio,bluetooth", 30),
        _p(202, "Mechanical Keyboard", "120.00", "computers", "Hot-swappable RGB mechanical keyboard.", "keyboard,rgb", 15),
        _p(203, "4K Webcam", "65.00", "computers", "1080p/4K USB webcam with mic.", "webcam,video", 8, "79.00", "65.00", True),
        _p(204, "USB-C Charger 65W", "34.99", "accessories", "GaN fast charger, 3 ports.", "charger,usb-c", 50),
        _p(205, "Smart Watch", "149.00", "wearables", "Fitness smartwatch, AMOLED.", "watch,fitness", 0),
    ],
    # Gamma — home goods (ids 301-304)
    "gamma": [
        _p(301, "Ceramic Dinner Set", "75.00", "kitchen", "16-piece ceramic dinnerware set.", "kitchen,ceramic", 20),
        _p(302, "Memory Foam Pillow", "39.99", "bedroom", "Ergonomic memory-foam pillow.", "bedroom,sleep", 40),
        _p(303, "Cast Iron Skillet", "49.00", "kitchen", "Pre-seasoned 12-inch skillet.", "kitchen,cookware", 14, "59.00", "49.00", True),
        _p(304, "Scented Candle Trio", "24.00", "decor", "Set of 3 soy scented candles.", "decor,candle", 0),
    ],
}

STORE_VARIANT = os.getenv("STORE_VARIANT", "alpha").lower()
PRODUCTS = _CATALOGS.get(STORE_VARIANT, _CATALOGS["alpha"])
BY_ID = {p["id"]: p for p in PRODUCTS}

# Categories derived from the active catalog.
_cat_counts: dict[str, int] = {}
for _p_ in PRODUCTS:
    _cat_counts[_p_["category_slug"]] = _cat_counts.get(_p_["category_slug"], 0) + 1
CATEGORIES = [{"id": c, "name": c.title(), "slug": c, "count": n} for c, n in _cat_counts.items()]

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
            page = max(1, int((qs.get("page", ["1"])[0]) or 1))
            per_page = max(1, int((qs.get("per_page", ["100"])[0]) or 100))
            start = (page - 1) * per_page
            return self._send(200, {"products": PRODUCTS[start:start + per_page]})

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
    print(f"Speako test store [{STORE_VARIANT}] listening on http://0.0.0.0:{PORT}")
    print(f"API key: {API_KEY}  (send as 'Authorization: Bearer {API_KEY}')")
    print(f"{len(PRODUCTS)} products loaded (variant={STORE_VARIANT}). Ctrl+C to stop.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
