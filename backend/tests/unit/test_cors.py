"""Unit tests for the multi-tenant CORS origin matcher (no network)."""

from src.app.core.middleware import origin_matcher


def test_shopify_storefronts_allowed_by_default():
    prod = origin_matcher(["https://merchant-dashboard-coral-sigma.vercel.app"])
    assert prod("https://bigb-pisar0or.myshopify.com")
    assert prod("https://any-cool-store.myshopify.com")
    assert prod("http://dev-store.myshopify.com")
    assert prod("https://merchant-dashboard-coral-sigma.vercel.app")


def test_unknown_or_apex_origins_rejected():
    prod = origin_matcher(["https://merchant-dashboard-coral-sigma.vercel.app"])
    assert not prod("https://evil.example.com")
    assert not prod("https://myshopify.com")  # apex must not match the suffix


def test_explicit_wildcard_suffix_forms():
    wild = origin_matcher(["https://*.example.com", "https://dashboard.example.com"])
    assert wild("https://store.example.com")
    assert wild("https://sub.store.example.com")
    assert wild("https://dashboard.example.com")
    assert not wild("https://example.com")


def test_allow_all_star():
    matcher = origin_matcher(["*"])
    assert matcher("https://anything.com")


def test_empty_config_defaults_to_all_shopify():
    matcher = origin_matcher(None)
    assert matcher("https://default.myshopify.com")


def test_same_origin_or_no_origin_always_allowed():
    matcher = origin_matcher(["https://dashboard.example.com"])
    assert matcher("")