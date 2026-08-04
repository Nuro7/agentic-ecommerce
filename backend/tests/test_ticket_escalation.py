"""Tests for deterministic ticket escalation, PII capture, and ticket intake."""
from datetime import datetime, timedelta, timezone

import pytest

from src.app.agent.brain.ticket_intake import (
    TicketIntakeState,
    detect_escalation,
    handle_ticket_intake_turn,
    session_contact,
    ticket_created_with_issue_message,
)
from src.app.agent.guardrails import extract_contact_info, is_pii_placeholder
from src.app.modules.tickets.repository import TicketRepository
from src.app.services.ticketing import (
    _build_transcript_turns,
    _classify_issue,
    _detect_heat,
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
        assert _detect_priority("this is urgent, my order was stolen") == "urgent"
        assert _priority_reason("refund please") == "keyword:refund"
        assert _priority_reason("hello") == "llm"

    def test_heat(self):
        assert _detect_heat("my order was stolen help asap", "urgent") == "hot"
        assert _detect_heat("this is urgent", "high") == "hot"
        assert _detect_heat("my order arrived damaged", "high") == "warm"
        assert _detect_heat("I want a refund", "high") == "warm"
        assert _detect_heat("talk to a human", "low") == "cold"
        assert _detect_heat("hello", "medium") == "cold"

    def test_issue_summary_drives_priority(self):
        # The customer's stated issue (captured by the intake FSM) is what scores
        # the ticket — "damaged box + wrong size" must be high/warm, not whatever
        # the chat history happened to say.
        issue = "I received a damaged box and the shoe size is wrong"
        assert _detect_priority(issue) == "high"
        assert _detect_heat(issue, _detect_priority(issue)) == "warm"
        assert _classify_issue(issue) == "damaged_order"
        assert _detect_priority("please refund my money") == "high"
        assert _detect_priority("talk to a real person please") == "low"

    def test_localized_message(self):
        assert "ticket" in ticket_created_message("en").lower()
        assert ticket_created_message("ml") != ticket_created_message("en")
        assert "TK-1001" in ticket_created_message("en", "TK-1001")

    def test_created_message_echoes_issue_and_callback(self):
        # Matches the expected flow: "ticket #TK-1001 with your issue summary:
        # 'Damaged box and incorrect shoe size' ... will call you at
        # 8943737227 shortly."
        msg = ticket_created_with_issue_message(
            "en", "TK-1001", "Damaged box and incorrect shoe size",
            "call", "8943737227",
        )
        assert "TK-1001" in msg
        assert "Damaged box and incorrect shoe size" in msg
        assert "call you at 8943737227" in msg


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

    async def test_email_asks_for_issue_then_creates_ticket(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {
                "status": "success",
                "ticket_id": "T-42",
                "priority": "high",
                "message": "created",
            }

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="my email is john@example.com",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_CONTACT,
                "ticket_intake_pending": {"trigger_message": "order damaged"},
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_email"] == "john@example.com"
        assert actions == []

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I received a damaged box and the shoe size is wrong.",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "order damaged",
                    "pending_email": "john@example.com",
                },
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
        assert captured.get("customer_email") == "john@example.com"
        assert captured.get("issue_summary") == "I received a damaged box and the shoe size is wrong"
        assert any(a["type"] == "show_ticket" for a in actions)

    async def test_phone_requires_verification_first(self, monkeypatch):
        # A phone number is NOT persisted directly — it must be read back and
        # confirmed. First reply moves to VERIFYING_PHONE and asks for "yes".
        text, state, pending, actions = await self._run(
            monkeypatch, "call me at 9876543210", pending={"trigger_message": "refund"}
        )
        assert state == TicketIntakeState.VERIFYING_PHONE
        assert pending["pending_phone"] == "9876543210"
        assert "9-8-7-6-5-4-3-2-1-0" in text
        assert actions == []

    async def test_phone_confirmed_then_asks_issue_then_creates(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {
                "status": "success",
                "ticket_id": "T-42",
                "priority": "high",
                "message": "created",
            }

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="yes that's correct",
            session_meta={
                "ticket_intake_state": TicketIntakeState.VERIFYING_PHONE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                },
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_phone"] == "9876543210"
        assert "issue" in text.lower()
        assert actions == []

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I received a damaged box and the shoe size is wrong.",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                },
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
        assert captured.get("issue_summary") == "I received a damaged box and the shoe size is wrong"
        assert any(a["type"] == "show_ticket" for a in actions)

    async def test_issue_capture_reasks_when_empty(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {"status": "success", "ticket_id": "T-42", "message": "created"}

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="[email]",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_email": "john@example.com",
                },
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert captured == {}  # no ticket until a real issue is described

    async def test_phone_denied_recollects(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {"status": "success", "ticket_id": "T-42", "message": "created"}

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="no that's wrong",
            session_meta={
                "ticket_intake_state": TicketIntakeState.VERIFYING_PHONE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                },
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.AWAITING_CONTACT
        assert captured == {}  # no ticket created on a denied number

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
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_email"] == "john@example.com"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="the box arrived damaged",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "order damaged",
                    "pending_email": "john@example.com",
                },
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
        # Phone captured from session meta → goes to verification, NOT straight
        # to the DB. Confirm it on the follow-up turn.
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
        assert state == TicketIntakeState.VERIFYING_PHONE
        assert pending["pending_phone"] == "9876543210"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="yes",
            session_meta={
                "ticket_intake_state": TicketIntakeState.VERIFYING_PHONE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                },
            },
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            session_service=None,
            language="en",
        )
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_phone"] == "9876543210"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I need a refund",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                },
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
        assert captured.get("issue_summary") == "I need a refund"

    async def test_invalid_phone_asks_again(self, monkeypatch):
        # User tried to say their number in parts ("73 72 27") — never persist a
        # short / mashed number; re-prompt for a valid 10-digit number.
        text, state, pending, actions = await self._run(
            monkeypatch, "73 72 27", pending={"trigger_message": "order damaged"}
        )
        assert state == TicketIntakeState.AWAITING_CONTACT
        assert actions == []
        assert "valid" in text.lower()


@pytest.mark.integration
class TestCreateSupportTicketTranscript:
    async def test_persisted_transcript_includes_trigger_message(self, monkeypatch):
        """The ticket's transcript_json must include the full chat up to and
        including the escalation-triggering turn — not just pre-turn history."""
        import src.app.services.ticketing as svc
        import src.app.core.database as dbmod
        import src.app.modules.tickets.service as svcmod

        persisted = {}

        class FakeTicket:
            def __init__(self, data):
                self.id = "T-1"
                self.ticket_number = "TK-1001"
                self.heat = data.get("heat")

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        def fake_db():
            return _FakeSession()

        monkeypatch.setattr(dbmod, "AsyncSessionLocal", fake_db)

        class FakeTicketService:
            def __init__(self, db):
                pass

            async def create_ticket(self, tenant_id, data):
                persisted.update(data)
                return FakeTicket(data)

        monkeypatch.setattr(svcmod, "TicketService", FakeTicketService)

        result = await svc.create_support_ticket(
            tenant_id="t1",
            session_id="s1",
            conversation_history=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "how can I help?"},
            ],
            store_client=None,
            trigger_message="my order arrived damaged",
            source="deterministic",
        )

        assert result["status"] == "success"
        assert result["ticket_number"] == "TK-1001"
        turns = persisted["transcript_json"]["turns"]
        assert turns[-1]["role"] == "user"
        assert turns[-1]["content"] == "my order arrived damaged"
        assert persisted["heat"] == "warm"


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

    async def test_sequential_ticket_numbers_per_tenant(self, repo_db):
        repo = TicketRepository(repo_db)
        now = datetime.now(timezone.utc)
        t1a = await repo.create("t1", {
            "session_id": "s1", "issue_summary": "one", "created_at": now,
        })
        t1b = await repo.create("t1", {
            "session_id": "s2", "issue_summary": "two", "created_at": now,
        })
        t2a = await repo.create("t2", {
            "session_id": "s3", "issue_summary": "three", "created_at": now,
        })
        assert t1a.ticket_number == "TK-1001"
        assert t1b.ticket_number == "TK-1002"
        # Per-tenant counter restarts — tenant t2 gets its own TK-1001.
        assert t2a.ticket_number == "TK-1001"
