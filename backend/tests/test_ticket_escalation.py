"""Tests for deterministic ticket escalation, PII capture, and ticket intake."""
from datetime import datetime, timedelta, timezone

import pytest

from src.app.agent.brain.ticket_intake import (
    TicketIntakeState,
    detect_escalation,
    handle_ticket_intake_turn,
    session_contact,
)
from src.app.agent.guardrails import extract_contact_info, is_pii_placeholder
from src.app.modules.tickets.repository import TicketRepository
from src.app.services.ticketing import (
    _classify_issue,
    _detect_priority,
    _extract_order_id,
    _priority_reason,
    ticket_created_message,
)


@pytest.mark.unit
class TestExtractContactInfo:
    def test_email_and_phone(self):
        assert extract_contact_info(
            "reach me at john.doe@example.com or 9876543210"
        ) == ("john.doe@example.com", "9876543210")

    def test_only_email(self):
        assert extract_contact_info("My email is a@b.co") == ("a@b.co", "")

    def test_none(self):
        assert extract_contact_info("") == ("", "")
        assert extract_contact_info("no personal data here") == ("", "")

    def test_is_pii_placeholder(self):
        assert is_pii_placeholder("[email]")
        assert is_pii_placeholder(" [PHONE] ")
        assert is_pii_placeholder("real@mail.com") is False


@pytest.mark.unit
class TestDetectEscalation:
    @pytest.mark.parametrize(
        "message",
        [
            "Ente oru order damage aayirunnu",          # Malayalam damaged-order mis-route
            "my order arrived damaged",
            "I want a refund for my order",
            "I want to talk to a human",
            "please connect me to customer care",
            "the item is broken and I'm furious",
            "my order was not received",
        ],
    )
    def test_escalates(self, message):
        assert detect_escalation(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "What is your return policy?",
            "How long does a refund take?",
            "Do you have a refund policy?",
            "Can I return this item?",
            "show me damage-resistant phone cases",
            "what if the item arrives late?",
            "Hi there",
        ],
    )
    def test_does_not_escalate(self, message):
        assert detect_escalation(message) is False


@pytest.mark.unit
class TestTicketingHelpers:
    def test_classify_issue(self):
        assert _classify_issue("my order was damaged and broken") == "damaged_order"
        assert _classify_issue("I want a refund") == "refund"
        assert _classify_issue("item missing from my order") == "missing_item"
        assert _classify_issue("I got the wrong item") == "wrong_item"
        assert _classify_issue("talk to a human please") == "talk_to_human"
        assert _classify_issue("") == "other"

    def test_extract_order_id(self):
        assert _extract_order_id("order #12345 is late") == "12345"
        assert _extract_order_id("my order 99887766 not delivered") == "99887766"
        assert _extract_order_id("tracking id: ABC123XYZ") == "ABC123XYZ"
        assert _extract_order_id("order damage aayirunnu") == ""
        assert _extract_order_id("no order here") == ""

    def test_priority(self):
        assert _detect_priority("refund please") == "high"
        assert _detect_priority("talk to a human") == "low"
        assert _detect_priority("hello") == "medium"
        assert _priority_reason("refund please") == "keyword:refund"
        assert _priority_reason("hello") == "llm"

    def test_localized_message(self):
        assert "ticket" in ticket_created_message("en").lower()
        assert ticket_created_message("ml") != ticket_created_message("en")


@pytest.mark.unit
class TestSessionContact:
    def test_from_meta(self):
        email, phone = session_contact({"customer_email": "a@b.co", "customer_phone": "9876543210"})
        assert (email, phone) == ("a@b.co", "9876543210")

    def test_placeholders_cleared(self):
        email, phone = session_contact(
            {"customer_email": "[email]", "address_data": {"phone": "9876543210"}}
        )
        assert email == ""
        assert phone == "9876543210"


@pytest.mark.unit
class TestTicketIntake:
    async def _run(self, monkeypatch, message, pending=None, language="en", meta_extra=None):
        from src.app.agent.brain import ticket_intake as ti

        async def fake_create_support_ticket(**kwargs):
            return {
                "status": "success",
                "ticket_id": "T-42",
                "priority": kwargs.get("source") == "deterministic" and "high" or "medium",
                "message": "created",
            }

        monkeypatch.setattr(ti, "create_support_ticket", fake_create_support_ticket)
        meta = {"ticket_intake_state": TicketIntakeState.AWAITING_CONTACT}
        if meta_extra:
            meta.update(meta_extra)
        if pending is not None:
            meta["ticket_intake_pending"] = pending
        return await handle_ticket_intake_turn(
            cleaned_message=message,
            session_meta=meta,
            tenant_id="t1",
            session_id="s1",
            conversation_history=[{"role": "user", "content": "my order is damaged"}],
            store_client=None,
            session_service=None,
            language=language,
        )

    async def test_creates_ticket_with_email(self, monkeypatch):
        text, state, pending, actions = await self._run(
            monkeypatch, "my email is john@example.com", pending={"trigger_message": "order damaged"}
        )
        assert state == TicketIntakeState.IDLE
        assert pending == {}
        assert any(a["type"] == "show_ticket" for a in actions)

    async def test_creates_ticket_with_phone(self, monkeypatch):
        text, state, pending, actions = await self._run(
            monkeypatch, "call me at 9876543210", pending={"trigger_message": "refund"}
        )
        assert state == TicketIntakeState.IDLE
        assert actions[0]["payload"]["ticket_id"] == "T-42"

    async def test_reasks_when_no_contact(self, monkeypatch):
        text, state, pending, actions = await self._run(
            monkeypatch, "I don't know", pending={"trigger_message": "order damaged"}
        )
        assert state == TicketIntakeState.AWAITING_CONTACT
        assert pending["trigger_message"] == "order damaged"
        assert actions == []

    async def test_cancel_resets(self, monkeypatch):
        text, state, pending, actions = await self._run(
            monkeypatch, "never mind", pending={"trigger_message": "order damaged"}
        )
        assert state == TicketIntakeState.IDLE
        assert pending == {}
        assert actions == []

    async def test_stale_pending_resets(self, monkeypatch):
        text, state, pending, actions = await self._run(monkeypatch, "anything")
        assert state == TicketIntakeState.IDLE

    async def test_creates_ticket_when_message_redacted_but_meta_has_email(self, monkeypatch):
        # check_input redacts PII on the cleaned_message (email → [email]) but the
        # real value is captured from the RAW message and stored in session meta.
        # The intake FSM must fall back to session meta, not re-ask forever.
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {
                "status": "success",
                "ticket_id": "T-99",
                "priority": "high",
                "message": "created",
            }

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="my email is [email]",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_CONTACT,
                "ticket_intake_pending": {"trigger_message": "order damaged"},
                "customer_email": "john@example.com",
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.IDLE
        assert pending == {}
        assert any(a["type"] == "show_ticket" for a in actions)
        assert captured.get("customer_email") == "john@example.com"

    async def test_creates_ticket_when_message_redacted_but_meta_has_phone(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {
                "status": "success",
                "ticket_id": "T-99",
                "priority": "high",
                "message": "created",
            }

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="call me at [phone]",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_CONTACT,
                "ticket_intake_pending": {"trigger_message": "refund"},
                "customer_phone": "9876543210",
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.IDLE
        assert pending == {}
        assert captured.get("customer_phone") == "9876543210"


@pytest.mark.integration
class TestRepositoryFindOpenBySession:
    @pytest.fixture
    async def repo_db(self):
        from sqlalchemy.ext.asyncio import (
            async_sessionmaker,
            create_async_engine,
        )

        from src.app.modules.tickets.models import VoiceTicket

        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(VoiceTicket.__table__.create)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                yield session
        finally:
            await engine.dispose()

    async def test_returns_open_recent_ticket(self, repo_db):
        repo = TicketRepository(repo_db)
        now = datetime.now(timezone.utc)
        await repo.create("t1", {
            "session_id": "s1",
            "issue_summary": "first",
            "priority": "high",
            "status": "open",
            "created_at": now - timedelta(minutes=10),
        })
        found = await repo.find_open_by_session("t1", "s1", now - timedelta(minutes=60))
        assert found is not None
        assert found.issue_summary == "first"

    async def test_ignores_resolved_or_old_tickets(self, repo_db):
        repo = TicketRepository(repo_db)
        now = datetime.now(timezone.utc)
        await repo.create("t1", {
            "session_id": "s1",
            "issue_summary": "old open",
            "status": "open",
            "created_at": now - timedelta(hours=2),
        })
        await repo.create("t1", {
            "session_id": "s1",
            "issue_summary": "resolved recent",
            "status": "resolved",
            "created_at": now - timedelta(minutes=5),
        })
        assert await repo.find_open_by_session("t1", "s1", now - timedelta(minutes=60)) is None
