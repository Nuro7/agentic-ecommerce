"""Unit tests for the Storefront GraphQL overlay service (no network).

Uses httpx.MockTransport to assert the exact query strings + variables sent to
Shopify, and that responses are normalized into the shapes the overlay SPA
consumes. Token stays server-side — never asserted/echoed in a browser path.
"""
import json

import httpx
import pytest
from src.app.integrations.shopify.storefront import (
    StorefrontService,
    StorefrontServiceError,
    build_storefront_query,
    parse_price_query,
    strip_option_terms,
)


def _make_service(handler):
    return StorefrontService(
        store_domain="demo.myshopify.com",
        storefront_token="stoken",
        transport=httpx.MockTransport(handler),
    )


def _capture(handler):
    captured = {}

    async def wrapper(request):
        captured["request"] = request
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=handler())

    return wrapper, captured


def _search_response():
    return {
        "data": {
            "products": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/Product/1",
                            "handle": "runner-shoe",
                            "title": "Runner Shoe",
                            "vendor": "Speako",
                            "productType": "Shoes",
                            "tags": ["running"],
                            "availableForSale": True,
                            "featuredImage": {"url": "https://cdn/shoe.jpg", "altText": "shoe"},
                            "priceRange": {
                                "minVariantPrice": {"amount": "49.99", "currencyCode": "INR"},
                                "maxVariantPrice": {"amount": "59.99", "currencyCode": "INR"},
                            },
                        }
                    }
                ]
            }
        }
    }


@pytest.mark.asyncio
async def test_search_query_and_variables_shape():
    wrap, captured = _capture(_search_response)
    sf = _make_service(wrap)
    out = await sf.search_products("sneakers", first=20)

    body = captured["body"]
    assert "products(query: $query" in body["query"]
    assert "StorefrontSearchProducts" in body["query"]
    assert body["variables"]["query"] == "sneakers"
    assert body["variables"]["first"] == 20
    assert body["variables"]["sortKey"] == "RELEVANCE"

    assert out["match"] == "exact"
    assert out["count"] == 1
    assert len(out["products"]) == 1
    p = out["products"][0]
    assert p["handle"] == "runner-shoe"
    assert p["title"] == "Runner Shoe"
    assert p["price"]["amount"] == 49.99
    assert p["price"]["currencyCode"] == "INR"
    assert p["image"] == "https://cdn/shoe.jpg"
    assert p["available_for_sale"] is True


@pytest.mark.asyncio
async def test_search_passing_buyer_ip_header():
    captured = {}

    async def handler(request):
        captured["header"] = request.headers.get("Shopify-Storefront-Buyer-IP")
        return httpx.Response(200, json=_search_response())

    sf = _make_service(handler)
    await sf.search_products("sneakers", buyer_ip="1.2.3.4")
    assert captured["header"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_get_product_builds_variants_and_options():
    async def handler(request):
        body = json.loads(request.content)
        assert "product(handle: $handle)" in body["query"]
        assert body["variables"]["handle"] == "runner-shoe"
        return httpx.Response(200, json={
            "data": {"product": {
                "id": "gid://shopify/Product/1",
                "handle": "runner-shoe",
                "title": "Runner Shoe",
                "descriptionHtml": "<p>Great</p>",
                "featuredImage": {"url": "https://cdn/shoe.jpg", "altText": "shoe"},
                "priceRange": {
                    "minVariantPrice": {"amount": "49.99", "currencyCode": "INR"},
                    "maxVariantPrice": {"amount": "59.99", "currencyCode": "INR"},
                },
                "options": [{"name": "Color", "values": ["Red", "Blue"]}],
                "variants": {"edges": [{"node": {
                    "id": "gid://shopify/ProductVariant/11",
                    "title": "Red",
                    "availableForSale": True,
                    "price": {"amount": "49.99", "currencyCode": "INR"},
                    "selectedOptions": [{"name": "Color", "value": "Red"}],
                }}]},
            }}
        })

    sf = _make_service(handler)
    out = await sf.get_product("runner-shoe")
    assert out["handle"] == "runner-shoe"
    assert out["options"][0]["name"] == "Color"
    assert out["variants"][0]["id"] == "gid://shopify/ProductVariant/11"
    assert out["variants"][0]["price"]["amount"] == 49.99
    assert out["variants"][0]["selected_options"][0]["value"] == "Red"


@pytest.mark.asyncio
async def test_get_product_missing_returns_none():
    async def handler(request):
        return httpx.Response(200, json={"data": {"product": None}})

    sf = _make_service(handler)
    assert await sf.get_product("missing") is None


@pytest.mark.asyncio
async def test_cart_create_variables_and_normalization():
    captured = {}

    async def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": {"cartCreate": {
                "cart": {
                    "id": "gid://shopify/Cart/c1",
                    "checkoutUrl": "https://demo.myshopify.com/checkout/c/abc",
                    "totalQuantity": 2,
                    "buyerIdentity": {"email": None, "countryCode": None},
                    "discountCodes": [],
                    "cost": {
                        "subtotalAmount": {"amount": "99.98", "currencyCode": "INR"},
                        "totalAmount": {"amount": "99.98", "currencyCode": "INR"},
                    },
                    "lines": {"edges": [{"node": {
                        "id": "gid://shopify/CartLine/1",
                        "quantity": 2,
                        "merchandise": {
                            "id": "gid://shopify/ProductVariant/11",
                            "title": "Red",
                            "price": {"amount": "49.99", "currencyCode": "INR"},
                            "product": {"handle": "runner-shoe", "title": "Runner Shoe",
                                        "featuredImage": {"url": "https://cdn/shoe.jpg", "altText": ""}},
                        },
                        "cost": {"totalAmount": {"amount": "99.98", "currencyCode": "INR"}},
                    }}]},
                },
                "userErrors": [],
            }}
        })

    sf = _make_service(handler)
    out = await sf.cart_create(lines=[{"merchandiseId": "gid://shopify/ProductVariant/11", "quantity": 2}])

    vars_ = captured["body"]["variables"]
    assert vars_["input"]["lines"] == [
        {"merchandiseId": "gid://shopify/ProductVariant/11", "quantity": 2}
    ]
    assert out["cart_id"] == "gid://shopify/Cart/c1"
    assert out["checkout_url"] == "https://demo.myshopify.com/checkout/c/abc"
    assert out["total_quantity"] == 2
    assert out["subtotal"]["amount"] == 99.98
    assert out["lines"][0]["product_handle"] == "runner-shoe"
    assert out["lines"][0]["unit_price"]["amount"] == 49.99


@pytest.mark.asyncio
async def test_cart_lines_add_variables():
    captured = {}

    async def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": {"cartLinesAdd": {
                "cart": {"id": "gid://shopify/Cart/c1", "checkoutUrl": "https://x/checkout",
                         "totalQuantity": 3,
                         "discountCodes": [{"code": "SALE10", "applicable": True}],
                         "cost": {"subtotalAmount": {"amount": "10.0", "currencyCode": "INR"},
                                  "totalAmount": {"amount": "10.0", "currencyCode": "INR"}},
                         "lines": {"edges": []}},
                "userErrors": [],
            }}
        })

    sf = _make_service(handler)
    out = await sf.cart_lines_add("gid://shopify/Cart/c1",
                                  [{"merchandiseId": "gid://shopify/ProductVariant/12", "quantity": 1}])
    assert captured["body"]["variables"]["cartId"] == "gid://shopify/Cart/c1"
    assert captured["body"]["variables"]["lines"] == [
        {"merchandiseId": "gid://shopify/ProductVariant/12", "quantity": 1}
    ]
    assert out["discount_codes"] == [{"code": "SALE10", "applicable": True}]


@pytest.mark.asyncio
async def test_cart_discount_codes_update_sends_codes():
    captured = {}

    async def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": {"cartDiscountCodesUpdate": {
                "cart": {"id": "gid://shopify/Cart/c1", "checkoutUrl": "https://x/c",
                         "totalQuantity": 1,
                         "discountCodes": [{"code": "SALE10", "applicable": True}],
                         "cost": {"subtotalAmount": {"amount": "90.0", "currencyCode": "INR"},
                                  "totalAmount": {"amount": "81.0", "currencyCode": "INR"}},
                         "lines": {"edges": []}},
                "userErrors": [],
            }}
        })

    sf = _make_service(handler)
    out = await sf.cart_discount_codes_update("gid://shopify/Cart/c1", ["SALE10"])
    assert captured["body"]["variables"]["discountCodes"] == ["SALE10"]
    assert out["discount_codes"][0]["code"] == "SALE10"
    assert out["total"]["amount"] == 81.0


@pytest.mark.asyncio
async def test_cart_buyer_identity_update_sends_email():
    captured = {}

    async def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": {"cartBuyerIdentityUpdate": {
                "cart": {"id": "gid://shopify/Cart/c1", "checkoutUrl": "https://x/c",
                         "totalQuantity": 1,
                         "discountCodes": [],
                         "cost": {"subtotalAmount": {"amount": "1.0", "currencyCode": "INR"},
                                  "totalAmount": {"amount": "1.0", "currencyCode": "INR"}},
                         "lines": {"edges": []}},
                "userErrors": [],
            }}
        })

    sf = _make_service(handler)
    await sf.cart_buyer_identity_update("gid://shopify/Cart/c1", {"email": "a@b.com", "countryCode": "IN"})
    assert captured["body"]["variables"]["buyerIdentity"] == {"email": "a@b.com", "countryCode": "IN"}


@pytest.mark.asyncio
async def test_cart_delivery_addresses_replace_payload():
    captured = {}

    async def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": {"cartDeliveryAddressesReplace": {
                "cart": {"id": "gid://shopify/Cart/c1", "checkoutUrl": "https://x/c",
                         "totalQuantity": 1,
                         "discountCodes": [],
                         "cost": {"subtotalAmount": {"amount": "1.0", "currencyCode": "INR"},
                                  "totalAmount": {"amount": "1.0", "currencyCode": "INR"}},
                         "lines": {"edges": []}},
                "userErrors": [],
            }}
        })

    sf = _make_service(handler)
    await sf.cart_delivery_addresses_replace(
        "gid://shopify/Cart/c1",
        [{"deliveryAddress": {"address1": "1 Main St", "city": "Bengaluru", "countryCode": "IN",
                              "zip": "560001", "firstName": "Riya"}}],
    )
    assert captured["body"]["variables"]["deliveryAddresses"] == [
        {"deliveryAddress": {"address1": "1 Main St", "city": "Bengaluru", "countryCode": "IN",
                             "zip": "560001", "firstName": "Riya"}}
    ]


@pytest.mark.asyncio
async def test_user_errors_surfaced_not_raised():
    async def handler(request):
        return httpx.Response(200, json={
            "data": {"cartLinesAdd": {
                "cart": None,
                "userErrors": [{"field": ["cart"], "message": "Cart not found", "code": "NOT_FOUND"}],
            }}
        })

    sf = _make_service(handler)
    out = await sf.cart_lines_add("gid://shopify/Cart/missing", [])
    assert out["errors"][0]["code"] == "NOT_FOUND"
    assert out.get("cart") is None


@pytest.mark.asyncio
async def test_graphql_errors_raise():
    async def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    sf = _make_service(handler)
    with pytest.raises(StorefrontServiceError):
        await sf.search_products("x")


@pytest.mark.asyncio
async def test_unconfigured_service_raises():
    sf = StorefrontService(store_domain="", storefront_token="")
    with pytest.raises(StorefrontServiceError):
        await sf.search_products("x")


@pytest.mark.asyncio
async def test_checkout_url_via_cart_get():
    async def handler(request):
        body = json.loads(request.content)
        assert body["variables"]["cartId"] == "gid://shopify/Cart/c1"
        return httpx.Response(200, json={
            "data": {"cart": {
                "id": "gid://shopify/Cart/c1",
                "checkoutUrl": "https://demo.myshopify.com/checkout/c/abc",
                "totalQuantity": 0,
                "discountCodes": [],
                "cost": {"subtotalAmount": {"amount": "0.0", "currencyCode": "INR"},
                         "totalAmount": {"amount": "0.0", "currencyCode": "INR"}},
                "lines": {"edges": []},
            }}
        })

    sf = _make_service(handler)
    out = await sf.cart_get("gid://shopify/Cart/c1")
    assert out["checkout_url"] == "https://demo.myshopify.com/checkout/c/abc"


# ── Natural-language price parsing + zero-result fallback ──────────────────────

def test_parse_price_query_plain_passthrough():
    # No price language → query is untouched (keeps existing search behavior).
    assert parse_price_query("sneakers") == {"terms": "sneakers", "min": None, "max": None}
    assert parse_price_query("nike air max") == {
        "terms": "nike air max", "min": None, "max": None
    }


def test_parse_price_query_under():
    p = parse_price_query("formal shoes under 5000")
    assert p["terms"] == "formal shoes"
    assert p["max"] == 5000
    assert p["min"] is None


def test_parse_price_query_strips_duplicate_number():
    # The reported bug query — a stray number duplicating the bound is removed.
    p = parse_price_query("formal shoes 5000 under 5000")
    assert p["terms"] == "formal shoes"
    assert p["max"] == 5000


def test_parse_price_query_strips_punctuated_duplicate_number():
    # The exact screenshot query: "5000?" must not leak into the keywords.
    p = parse_price_query("formal shoes 5000? under 5000")
    assert p["terms"] == "formal shoes"
    assert p["max"] == 5000


def test_parse_price_query_over_and_currency():
    p = parse_price_query("watches above ₹1,200")
    assert p["terms"] == "watches"
    assert p["min"] == 1200
    assert p["max"] is None


def test_parse_price_query_between():
    p = parse_price_query("headphones between 1000 and 3000")
    assert p["terms"] == "headphones"
    assert p["min"] == 1000
    assert p["max"] == 3000


def test_build_storefront_query_adds_price_filter():
    q, has_price = build_storefront_query("formal shoes under 5000")
    assert q == "formal shoes variants.price:<=5000"
    assert has_price is True


def test_build_storefront_query_plain():
    q, has_price = build_storefront_query("sneakers")
    assert q == "sneakers"
    assert has_price is False


@pytest.mark.asyncio
async def test_search_builds_price_filtered_query():
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body["variables"]["query"])
        return httpx.Response(200, json=_search_response())

    sf = _make_service(handler)
    await sf.search_products("formal shoes under 5000")
    # The FIRST (price-filtered) attempt must carry the parsed price bound.
    assert calls[0] == "formal shoes variants.price:<=5000"


def _formal_shoe_response():
    """A genuine formal shoe priced above a typical budget (8999)."""
    return {
        "data": {
            "products": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/Product/2",
                            "handle": "oxford-formal-shoe",
                            "title": "Oxford Formal Shoe",
                            "vendor": "Speako",
                            "productType": "Formal Shoes",
                            "tags": ["formal", "leather"],
                            "availableForSale": True,
                            "featuredImage": {"url": "https://cdn/oxford.jpg", "altText": "oxford"},
                            "priceRange": {
                                "minVariantPrice": {"amount": "8999.00", "currencyCode": "INR"},
                                "maxVariantPrice": {"amount": "8999.00", "currencyCode": "INR"},
                            },
                        }
                    }
                ]
            }
        }
    }


@pytest.mark.asyncio
async def test_search_relaxes_price_when_matching_product_over_budget():
    """A real formal shoe exists but costs more than the budget: the price-
    filtered attempt is empty, the keyword-only attempt finds it, so we honestly
    surface it as match='price_relaxed' ("no X under N, but here are our X")."""
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        q = body["variables"]["query"]
        calls.append(q)
        if "variants.price" in q:
            return httpx.Response(200, json={"data": {"products": {"edges": []}}})
        return httpx.Response(200, json=_formal_shoe_response())

    sf = _make_service(handler)
    out = await sf.search_products("formal shoes under 5000")
    assert out["match"] == "price_relaxed"
    assert len(out["products"]) == 1
    assert out["products"][0]["handle"] == "oxford-formal-shoe"
    assert calls[0] == "formal shoes variants.price:<=5000"
    assert calls[1] == "formal shoes"
    assert "under" in out["message"].lower()


@pytest.mark.asyncio
async def test_search_returns_none_and_never_dumps_bestsellers():
    """When NO product matches the qualifiers (e.g. only running shoes exist for
    a 'formal shoes' query), the relevance guard rejects the mismatches and we
    return match='none' with an empty product set — the old best-seller dump
    that produced the reported mismatch screenshot must NEVER run."""
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append((body["variables"]["query"], body["variables"]["sortKey"]))
        # Every attempt returns only the mismatched running shoe.
        return httpx.Response(200, json=_search_response())

    sf = _make_service(handler)
    out = await sf.search_products("formal shoes under 5000")
    assert out["match"] == "none"
    assert out["products"] == []
    assert out["count"] == 0
    # The forbidden best-seller dump must never be issued.
    assert not any(sort == "BEST_SELLING" for _, sort in calls)
    assert "couldn't find" in out["message"].lower()


# ── Variant-option (size/fit) tokens must not poison the search ────────────────

def test_strip_option_terms_removes_labeled_size():
    # "size 9" is a variant selector, not a product descriptor.
    assert strip_option_terms("formal shoes size 9") == "formal shoes"
    assert strip_option_terms("formal shoes size uk 9") == "formal shoes"
    assert strip_option_terms("running shoes uk 10") == "running shoes"
    assert strip_option_terms("t-shirt size xl") == "t-shirt"


def test_strip_option_terms_leaves_plain_queries_untouched():
    assert strip_option_terms("formal shoes") == "formal shoes"
    assert strip_option_terms("nike air max") == "nike air max"
    assert strip_option_terms("") == ""


def test_build_storefront_query_strips_size_tokens():
    # The size selector must never reach Shopify's keyword text (it matches no
    # product title and skews relevance), but the price bound is preserved.
    q, has_price = build_storefront_query("formal shoes size 9 under 5000")
    assert q == "formal shoes variants.price:<=5000"
    assert has_price is True


def _affordable_formal_shoe_response():
    """A genuine formal shoe within a typical budget (4999)."""
    return {
        "data": {
            "products": {
                "edges": [
                    {
                        "node": {
                            "id": "gid://shopify/Product/3",
                            "handle": "derby-formal-shoe",
                            "title": "Derby Formal Shoe",
                            "vendor": "Speako",
                            "productType": "Formal Shoes",
                            "tags": ["formal", "leather"],
                            "availableForSale": True,
                            "featuredImage": {"url": "https://cdn/derby.jpg", "altText": "derby"},
                            "priceRange": {
                                "minVariantPrice": {"amount": "4999.00", "currencyCode": "INR"},
                                "maxVariantPrice": {"amount": "4999.00", "currencyCode": "INR"},
                            },
                        }
                    }
                ]
            }
        }
    }


@pytest.mark.asyncio
async def test_search_with_size_token_finds_products_and_never_sends_size_as_keyword():
    """The reported blank-overlay bug: "formal shoes size 9" returned nothing
    because "size" was enforced as a mandatory keyword no product contains.
    After the fix the size selector is stripped, so a genuine in-budget formal
    shoe is surfaced and the Shopify query never carries "size"/the number."""
    calls = []

    async def handler(request):
        body = json.loads(request.content)
        calls.append(body["variables"]["query"])
        return httpx.Response(200, json=_affordable_formal_shoe_response())

    sf = _make_service(handler)
    out = await sf.search_products("formal shoes size 9 under 5000")

    assert out["match"] == "exact"
    assert out["count"] == 1
    assert out["products"][0]["handle"] == "derby-formal-shoe"
    # The size selector must never reach Shopify's keyword text.
    assert calls[0] == "formal shoes variants.price:<=5000"
    assert "size" not in calls[0].lower()
    assert " 9" not in calls[0]
    # The cleaned, speakable query drops the size selector too.
    assert out["query"] == "formal shoes"