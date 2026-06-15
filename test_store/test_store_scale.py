"""Speako SCALE test store — serves a distinct large catalog per merchant.

One process serves all merchants. The merchant is identified by the Bearer API
key "key-m-{N}"; merchant N gets a deterministic catalog of PRODUCTS_PER items
with ids in the disjoint range [N*1_000_000 + 1 .. +PRODUCTS_PER], so cross-tenant
isolation is trivially verifiable.

Run:
    PORT=9100 PRODUCTS_PER=1000 python test_store_scale.py

Auth: Authorization: Bearer key-m-<N>   (any N; unknown/!match -> 401)
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.getenv("PORT", "9100"))
PRODUCTS_PER = int(os.getenv("PRODUCTS_PER", "1000"))

_ADJ = ["Classic", "Premium", "Eco", "Sport", "Vintage", "Smart", "Compact", "Deluxe", "Pro", "Lite"]
_NOUN = ["Shirt", "Mug", "Backpack", "Lamp", "Bottle", "Charger", "Notebook", "Sneaker", "Jacket", "Speaker"]
_CATS = ["apparel", "home", "electronics", "accessories", "outdoor"]

_CATALOG_CACHE: dict[int, list] = {}
_CARTS: dict[str, dict] = {}


def catalog_for(n: int) -> list:
    """Deterministic catalog of PRODUCTS_PER products for merchant N (cached)."""
    cached = _CATALOG_CACHE.get(n)
    if cached is not None:
        return cached
    base = n * 1_000_000
    out = []
    for i in range(1, PRODUCTS_PER + 1):
        pid = base + i
        adj = _ADJ[i % len(_ADJ)]
        noun = _NOUN[(i // 7) % len(_NOUN)]
        cat = _CATS[i % len(_CATS)]
        price = 5 + (i % 200) + 0.99
        stock = 0 if (i % 17 == 0) else (i % 50) + 1   # ~6% out of stock
        out.append({
            "id": pid,
            "name": f"M{n} {adj} {noun} {i}",
            "price": f"{price:.2f}",
            "regular_price": f"{price:.2f}",
            "sale_price": "",
            "on_sale": (i % 5 == 0),
            "in_stock": stock > 0,
            "stock_quantity": stock,
            "image_url": f"https://picsum.photos/seed/{pid}/300",
            "permalink": f"https://m{n}.store.test/p/{pid}",
            "description": f"{adj} {noun} number {i} from merchant {n}. Great for {cat}.",
            "short_description": f"{adj} {noun} {i}",
            "category_slug": cat,
            "tags": f"{cat},{adj.lower()}",
        })
    _CATALOG_CACHE[n] = out
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet — high request volume during sync

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _merchant(self):
        """Return merchant N from the Bearer key, or None if unauthorized."""
        auth = self.headers.get("Authorization", "")
        m = re.fullmatch(r"Bearer key-m-(\d+)", auth.strip())
        return int(m.group(1)) if m else None

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path.rstrip("/") or "/", parse_qs(u.query)
        if path == "/health":
            return self._send(200, {"status": "ok", "merchants_cached": len(_CATALOG_CACHE)})

        n = self._merchant()
        if n is None:
            return self._send(401, {"error": "unauthorized"})
        cat = catalog_for(n)

        if path == "/products/search":
            q = (qs.get("q", [""])[0] or "").lower()
            limit = max(1, min(int(qs.get("limit", ["6"])[0] or 6), 40))
            in_stock_only = qs.get("in_stock_only", ["true"])[0].lower() == "true"
            rows = cat
            if q:
                rows = [p for p in rows if q in p["name"].lower() or q in p["description"].lower() or q in p["tags"].lower()]
            if in_stock_only:
                rows = [p for p in rows if p["in_stock"]]
            return self._send(200, {"products": rows[:limit]})

        if path == "/products":
            page = max(1, int(qs.get("page", ["1"])[0] or 1))
            per = max(1, int(qs.get("per_page", ["100"])[0] or 100))
            start = (page - 1) * per
            return self._send(200, {"products": cat[start:start + per]})

        m = re.fullmatch(r"/products/(\d+)", path)
        if m:
            pid = int(m.group(1))
            p = next((x for x in cat if x["id"] == pid), None)
            return self._send(200, p) if p else self._send(404, {"error": "not found"})

        m = re.fullmatch(r"/products/(\d+)/inventory", path)
        if m:
            pid = int(m.group(1))
            p = next((x for x in cat if x["id"] == pid), None)
            if not p:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"in_stock": p["in_stock"], "stock_quantity": p["stock_quantity"]})

        if re.fullmatch(r"/products/(\d+)/variations", path):
            return self._send(200, {"variations": []})

        if path == "/categories":
            return self._send(200, {"categories": [{"id": c, "name": c.title(), "slug": c, "count": 0} for c in _CATS]})

        if path == "/cart":
            sid = f"{n}:{qs.get('session_id', [''])[0]}"
            return self._send(200, self._cart_view(sid))

        if path == "/store/info":
            return self._send(200, {"name": f"Merchant {n} Store", "currency": "USD",
                                    "url": f"https://m{n}.store.test"})
        if path == "/store/policies":
            return self._send(200, {"shipping": "Free over $50", "returns": "30 days"})
        return self._send(404, {"error": f"no route GET {path}"})

    def do_POST(self):
        n = self._merchant()
        if n is None:
            return self._send(401, {"error": "unauthorized"})
        path = urlparse(self.path).path.rstrip("/")
        b = self._body()
        sid = f"{n}:{b.get('session_id', '')}"
        if path == "/cart/add":
            cart = _CARTS.setdefault(sid, {})
            pid = int(b.get("product_id") or 0)
            p = next((x for x in catalog_for(n) if x["id"] == pid), None)
            if not p:
                return self._send(200, {"success": False, **self._cart_view(sid)})
            row = cart.setdefault(pid, {"product_id": pid, "name": p["name"], "price": p["price"], "quantity": 0})
            row["quantity"] += max(1, int(b.get("quantity", 1) or 1))
            return self._send(200, {"success": True, "cart": self._cart_view(sid)})
        if path == "/cart/remove":
            _CARTS.get(sid, {}).pop(int(b.get("product_id") or 0), None)
            return self._send(200, {"success": True, "cart": self._cart_view(sid)})
        return self._send(404, {"error": f"no route POST {path}"})

    def do_PUT(self):
        n = self._merchant()
        if n is None:
            return self._send(401, {"error": "unauthorized"})
        path = urlparse(self.path).path.rstrip("/")
        b = self._body()
        sid = f"{n}:{b.get('session_id', '')}"
        if path == "/cart/update":
            cart = _CARTS.setdefault(sid, {})
            pid = int(b.get("product_id") or 0)
            qty = int(b.get("quantity", 1) or 1)
            if qty <= 0:
                cart.pop(pid, None)
            elif pid in cart:
                cart[pid]["quantity"] = qty
            return self._send(200, {"success": True, "cart": self._cart_view(sid)})
        return self._send(404, {"error": f"no route PUT {path}"})

    def _cart_view(self, sid):
        items = list(_CARTS.get(sid, {}).values())
        total = sum(float(i["price"]) * i["quantity"] for i in items)
        count = sum(i["quantity"] for i in items)
        return {"items": items, "item_count": count, "total": f"{total:.2f}", "is_empty": count == 0}


if __name__ == "__main__":
    print(f"Speako SCALE store on :{PORT} — {PRODUCTS_PER} products/merchant, key=Bearer key-m-N")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
