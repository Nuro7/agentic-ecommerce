"""Custom-API timeout/retries are env-configurable + per-call latency is logged.

Lets each merchant set CUSTOM_API_TIMEOUT above their store's measured p95 and bound
retries, instead of the fixed 8s × 3 hang. The latency log line is the measurement tool.
"""
import asyncio
import logging

import httpx
import pytest

from src.app.config import settings
from src.app.core.http_retry import request_with_retries
from src.app.integrations.custom_api.client import CustomApiClient

pytestmark = pytest.mark.asyncio


# ── request_with_retries honors the attempts bound ────────────────────────────

async def test_attempts_one_means_no_retry(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = []

    async def send():
        calls.append(1)
        raise httpx.ReadTimeout("boom")

    with pytest.raises(httpx.ReadTimeout):
        await request_with_retries(send, attempts=1)
    assert len(calls) == 1  # no retry


async def test_attempts_three_retries_then_raises(monkeypatch):
    async def _no_sleep(*_a, **_k):
        return None
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    calls = []

    async def send():
        calls.append(1)
        raise httpx.ReadTimeout("boom")

    with pytest.raises(httpx.ReadTimeout):
        await request_with_retries(send, attempts=3)
    assert len(calls) == 3


# ── client builds timeout + retries from settings ─────────────────────────────

async def test_client_reads_timeout_and_retries_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "custom_api_timeout", 4.0)
    monkeypatch.setattr(settings, "custom_api_connect_timeout", 2.0)
    monkeypatch.setattr(settings, "custom_api_retries", 1)

    c = CustomApiClient(base_url="https://example.test")
    assert c._timeout.read == 4.0
    assert c._timeout.connect == 2.0
    assert c._retries == 1


async def test_retries_floored_at_one(monkeypatch):
    monkeypatch.setattr(settings, "custom_api_retries", 0)
    c = CustomApiClient(base_url="https://example.test")
    assert c._retries == 1  # never zero attempts


# ── _get logs a greppable latency line on success ─────────────────────────────

class _FakeResp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return [{"id": 1, "name": "Shoe", "price": "10"}]


class _FakeHttpx:
    async def get(self, path, params=None):
        return _FakeResp()


async def test_get_emits_latency_log(caplog):
    c = CustomApiClient(base_url="https://example.test")
    c._client = _FakeHttpx()  # stub the network
    with caplog.at_level(logging.INFO):
        data = await c._get("/products/search", {"q": "shoes"})
    assert data == [{"id": 1, "name": "Shoe", "price": "10"}]
    assert any("custom-api-latency GET /products/search 200" in r.message for r in caplog.records)
