"""Smoke tests for health, ready, version endpoints."""

import pytest


@pytest.mark.integration
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
async def test_version_returns_info(client):
    response = await client.get("/version")
    assert response.status_code == 200
    body = response.json()
    assert "app" in body
    assert "version" in body
    assert "environment" in body


@pytest.mark.integration
async def test_request_id_header_present(client):
    response = await client.get("/health")
    assert "x-request-id" in response.headers
