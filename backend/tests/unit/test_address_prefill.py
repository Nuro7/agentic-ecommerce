"""Tests for the checkout address prefill FSM (save new / fetch existing by phone)."""
import asyncio
from src.app.agent.brain.address import (
    AddressCollectionState as S,
    handle_address_collection,
)


def call(msg, state, addr_data, page_context, tenant_id="t1"):
    return asyncio.run(
        handle_address_collection(
            session_id="s1", user_message=msg, current_state=state,
            address_data=addr_data, language="en",
            page_context=page_context, tenant_id=tenant_id,
        )
    )


def test_checkout_idle_starts_phone_first():
    resp, next_state, data, actions = call(
        "5123456789", S.IDLE, {}, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert next_state == S.COLLECTING_PHONE


def test_existing_customer_fetched_by_phone_and_prefilled(monkeypatch):
    async def fake_get(phone, tenant_id):
        assert tenant_id == "t1"
        return {
            "first_name": "Asha", "last_name": "Nair", "address_1": "Flat 12, MG Road",
            "city": "Kochi", "state": "Kerala", "postcode": "682015",
            "email": "asha@example.com",
        }
    monkeypatch.setattr("src.app.modules.users.address_service.get_address_by_phone", fake_get)

    resp, next_state, addr, actions = call(
        "9876543210", S.COLLECTING_PHONE, {}, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert next_state == S.CONFIRMING
    assert addr["first_name"] == "Asha"
    assert addr["_using_saved"] == "1"
    assert addr["postcode"] == "682015"
    assert any(a.get("type") == "prefill_address" for a in actions)


def test_new_customer_saves_address_on_confirm(monkeypatch):
    saved = {}
    async def fake_save(session_id, tenant_id, phone, address_data):
        saved["phone"] = phone
        saved["data"] = address_data
    monkeypatch.setattr("src.app.modules.users.address_service.save_address", fake_save)

    addr = {
        "first_name": "Ravi", "last_name": "Kumar", "address_line1": "12 Park Road",
        "city": "Chennai", "state": "Tamil Nadu", "postcode": "600001", "phone": "9876543210",
    }
    resp, next_state, data, actions = call(
        "yes", S.CONFIRMING, addr, {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert next_state == S.COMPLETE
    assert saved.get("phone") == "9876543210"
    assert saved["data"]["city"] == "Chennai"
    assert any(a.get("type") == "redirect_checkout_with_address" for a in actions)


def test_unknown_phone_keeps_manual_confirm_on_checkout(monkeypatch):
    monkeypatch.setattr(
        "src.app.modules.users.address_service.get_address_by_phone",
        lambda phone, tenant: None,
    )
    resp, next_state, addr, actions = call(
        "9876543211", S.COLLECTING_PHONE, {},
        {"page_type": "checkout", "url": "https://store/checkout"},
    )
    assert next_state == S.CONFIRMING
    assert addr.get("_using_saved", "") != "1"
    assert any(a.get("type") == "prefill_address" for a in actions)
