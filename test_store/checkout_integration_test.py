"""Live Shopify checkout integration test — verifies the real storefront flow:
search product -> get variants -> add_to_cart -> attach_buyer_identity (address
bind) -> checkout_url. Exercises the stale-variant self-heal path too.

Run from backend/ so `src.app` is importable:
    python ../test_store/checkout_integration_test.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / "backend" / ".env")

from src.app.integrations.shopify.client import ShopifyClient

PASS = 0
FAIL = 0


class FakeRedis:
    """Minimal in-memory Redis stand-in (get/set/delete, bytes) so the cart-ID
    persistence works without a real Redis during this integration test."""

    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    async def delete(self, *keys):
        for k in keys:
            self.data.pop(k, None)
        return True

    async def scan_iter(self, match="*", count=100):
        return (k for k in list(self.data))


def report(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


async def main():
    client = ShopifyClient(redis_client=FakeRedis())
    print(f"Store: {client.store_domain}  API v{client.api_version}")

    # 1. Search products (store sells shoes, not shirts)
    print("\n[1] Search products")
    try:
        results = await client.search_products(query="shoe", limit=3)
        report("search_products", isinstance(results, list) and len(results) > 0,
               f"got {len(results) if isinstance(results, list) else results}")
        product = results[0] if isinstance(results, list) and results else {}
    except Exception as e:
        report("search_products", False, str(e))
        product = {}

    # 2. Product details / variants
    print("\n[2] Product details + variants")
    pid = product.get("id")
    detail = {}
    if pid:
        try:
            detail = await client.get_product_details(int(pid))
            variants = detail.get("variations") or []
            report("get_product_details", bool(detail.get("id")), f"variants={len(variants)}")
        except Exception as e:
            report("get_product_details", False, str(e))

    # 3. add_to_cart with a valid live variant
    print("\n[3] add_to_cart (valid variant)")
    valid_vid = 0
    for v in (detail.get("variations") or []):
        if isinstance(v, dict) and v.get("id"):
            if str(v.get("stock_status") or "").lower() != "outofstock":
                valid_vid = int(v["id"])
                break
    if not valid_vid and (detail.get("variations") or []):
        valid_vid = int((detail["variations"][0])["id"])

    cart1 = {}
    if valid_vid:
        try:
            cart1 = await client.add_to_cart(
                session_id="integration-test-session-1",
                product_id=int(pid),
                variation_id=valid_vid,
                quantity=1,
            )
            report("add_to_cart", bool(cart1.get("success")),
                   f"checkout_url={'yes' if cart1.get('checkout_url') else 'no'}")
        except Exception as e:
            report("add_to_cart", False, str(e))
    else:
        report("add_to_cart", False, "no variant resolved from search")

    # 4. attach_buyer_identity (address bind) — the checkout prefill path
    print("\n[4] attach_buyer_identity (address bind)")
    bound = {}
    try:
        bound = await client.attach_buyer_identity(
            session_id="integration-test-session-1",
            email="test@example.com",
            phone="9876543210",
            address={
                "first_name": "Asha",
                "last_name": "Nair",
                "address_1": "Flat 12, MG Road",
                "city": "Kochi",
                "state": "Kerala",
                "state_code": "KL",
                "postcode": "682015",
                "phone": "9876543210",
                "country": "IN",
                "country_code": "IN",
            },
        )
        report("attach_buyer_identity", bool(bound.get("checkout_url")),
               f"checkout_url={'yes' if bound.get('checkout_url') else 'no'} bound={bound.get('success')}")
    except Exception as e:
        report("attach_buyer_identity", False, str(e))

    # 5. Stale-variant self-heal: pass a nonsense variant, expect a retry
    print("\n[5] add_to_cart stale-variant self-heal")
    try:
        cart2 = await client.add_to_cart(
            session_id="integration-test-session-2",
            product_id=int(pid),
            variation_id=10107682521328,  # stale / non-existent
            quantity=1,
        )
        report("stale-variant self-heal", bool(cart2.get("success")),
               f"checkout_url={'yes' if cart2.get('checkout_url') else 'no'}")
    except Exception as e:
        report("stale-variant self-heal", False, str(e))

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    try:
        await client._http.aclose()
    except Exception:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
