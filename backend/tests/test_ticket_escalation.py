"""Tests for deterministic ticket escalation, PII capture, and ticket intake."""
from datetime import datetime, timedelta, timezone

import pytest

from src.app.agent.brain.ticket_intake import (
    TicketIntakeState,
    _extract_core_user_intent,
    detect_escalation,
    extract_customer_name,
    handle_ticket_intake_turn,
    sanitize_customer_name,
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
    _is_generic_issue,
    _priority_reason,
    build_final_issue_summary,
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
class TestNameSanitization:
    def test_valid_name_passes(self):
        assert sanitize_customer_name("John") == "John"
        assert sanitize_customer_name("John Smith") == "John Smith"
        assert sanitize_customer_name("  Mary-Anne  ") == "Mary-Anne"

    def test_dom_layout_noise_never_persisted(self):
        # The widget leaks DOM structure labels — they must never be a name.
        for noise in ("Footer", "Header", "Navigation", "Main", "Sidebar", "Menu"):
            assert sanitize_customer_name(noise) == "", f"{noise} leaked as name"
        assert sanitize_customer_name("Main Footer") == ""

    def test_intro_phrase_extracts_only_the_name(self):
        assert extract_customer_name("my name is John") == "John"
        assert extract_customer_name("call me Ravi Kumar") == "Ravi Kumar"
        assert extract_customer_name("no name to give") == ""

    def test_noise_inside_phrase_rejected(self):
        assert extract_customer_name("my name is Footer") == ""


@pytest.mark.unit
class TestIssueSummaryCleaning:
    def test_strips_context_boilerplate(self):
        from src.app.services import ticketing as svc
        assert svc._clean_issue_summary(
            "Customer requested to speak to a human support agent. Context: "
            "the box arrived damaged and I want a refund."
        ) == "the box arrived damaged and I want a refund"

    def test_bare_classification_kept(self):
        from src.app.services import ticketing as svc
        assert svc._clean_issue_summary("Customer is requesting a refund.") == "Customer is requesting a refund"

    def test_caps_at_100_chars(self):
        from src.app.services import ticketing as svc
        long = "damaged " * 30
        out = svc._clean_issue_summary(long)
        assert len(out) <= 100

    def test_empty_and_placeholders_stripped(self):
        from src.app.services import ticketing as svc
        assert svc._clean_issue_summary("") == ""
        assert svc._clean_issue_summary("[email] refund please") == "refund please"


@pytest.mark.unit
class TestExtractCoreUserIntent:
    def test_specific_complaint_kept(self):
        assert _extract_core_user_intent("my order is broken") == "my order is broken"
        assert _extract_core_user_intent("problem with my last order") == "problem with my last order"
        assert _extract_core_user_intent("I received a damaged box") == "I received a damaged box"

    def test_conversational_lead_stripped(self):
        assert _extract_core_user_intent("I have a problem with my last order") == "problem with my last order"
        assert _extract_core_user_intent("I got the wrong size") == "wrong size"

    def test_phone_only_reply_dropped(self):
        assert _extract_core_user_intent("8943737227") == ""
        assert _extract_core_user_intent("9 8 4 3 7 3 7 2 2 7") == ""

    def test_confirmation_replies_dropped(self):
        for reply in ("yes", "yeah", "that's correct", "theek hai", "no", "sheri"):
            assert _extract_core_user_intent(reply) == "", reply

    def test_name_only_reply_dropped(self):
        assert _extract_core_user_intent("Rahul") == ""
        assert _extract_core_user_intent("Ravi Kumar") == ""
        assert _extract_core_user_intent("my name is John") == ""

    def test_email_contact_reply_dropped(self):
        assert _extract_core_user_intent("my email is john@example.com") == ""
        assert _extract_core_user_intent("call me at 9876543210") == ""

    def test_customer_name_stripped(self):
        assert _extract_core_user_intent(
            "Rahul my order is broken", customer_name="Rahul"
        ) == "my order is broken"

    def test_empty_and_tiny(self):
        assert _extract_core_user_intent("") == ""
        assert _extract_core_user_intent("hi") == ""


@pytest.mark.unit
class TestBuildFinalIssueSummary:
    def test_specific_explicit_wins(self):
        assert build_final_issue_summary(
            "Item arrived broken", ["my order is broken"]
        ) == "Item arrived broken"

    def test_generic_explicit_joins_earlier_detail(self):
        # TK-1004: customer states a specific detail mid-intake, then answers the
        # final "what issue?" prompt with a generic hand-off request.
        assert build_final_issue_summary(
            "connect to customer care", ["problem with my last order"]
        ) == "Problem with my last order (Customer Care Request)"

    def test_generic_explicit_uses_most_recent_specific_detail(self):
        assert build_final_issue_summary(
            "talk to a human", ["order arrived late", "I got the wrong size"]
        ) == "I got the wrong size (Customer Care Request)"

    def test_generic_explicit_no_detail_keeps_cleaned_generic(self):
        assert build_final_issue_summary("connect to customer care", []) == "connect to customer care"

    def test_empty_explicit_falls_back_to_last_intent(self):
        assert build_final_issue_summary("", ["order is missing"]) == "order is missing"

    def test_no_signal_returns_empty(self):
        assert build_final_issue_summary("", []) == ""
        assert build_final_issue_summary("", ["yes", "9876543210"]) == ""

    def test_generic_detection(self):
        assert _is_generic_issue("connect to customer care")
        assert _is_generic_issue("talk to a human")
        assert _is_generic_issue("help")
        assert not _is_generic_issue("problem with my last order")
        assert not _is_generic_issue("my order arrived damaged")


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
        assert state == TicketIntakeState.AWAITING_NAME
        assert pending["pending_email"] == "john@example.com"
        assert actions == []

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="my name is John",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_NAME,
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
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_name"] == "John"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I received a damaged box and the shoe size is wrong.",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "order damaged",
                    "pending_email": "john@example.com",
                    "pending_name": "John",
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
        assert captured.get("customer_name") == "John"
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
        assert state == TicketIntakeState.AWAITING_NAME
        assert pending["pending_phone"] == "9876543210"
        assert "pleasure" in text.lower()
        assert actions == []

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="John Smith",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_NAME,
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
        assert pending["pending_name"] == "John Smith"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I received a damaged box and the shoe size is wrong.",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                    "pending_name": "John Smith",
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
        assert captured.get("customer_name") == "John Smith"
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

    async def test_name_step_rejects_dom_noise(self, monkeypatch):
        # "Footer" leaking in from the widget must NOT become the customer name —
        # the FSM stays on the name step and re-asks.
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {"status": "success", "ticket_id": "T-7", "message": "created"}

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="Footer",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_NAME,
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
        assert state == TicketIntakeState.AWAITING_NAME
        assert "name" in text.lower()
        assert captured == {}

    async def test_name_step_skip_proceeds_to_issue(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {"status": "success", "ticket_id": "T-8", "message": "created"}

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="skip",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_NAME,
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
        assert pending.get("pending_name", "") == ""
        assert "issue" in text.lower()
        assert captured == {}

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
        assert state == TicketIntakeState.AWAITING_NAME
        assert pending["pending_email"] == "john@example.com"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="John",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_NAME,
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
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_name"] == "John"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="the box arrived damaged",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "order damaged",
                    "pending_email": "john@example.com",
                    "pending_name": "John",
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
        assert captured.get("customer_name") == "John"

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
        assert state == TicketIntakeState.AWAITING_NAME
        assert pending["pending_phone"] == "9876543210"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="Ravi Kumar",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_NAME,
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
        assert pending["pending_name"] == "Ravi Kumar"

        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I need a refund",
            session_meta={
                "ticket_intake_state": TicketIntakeState.AWAITING_ISSUE,
                "ticket_intake_pending": {
                    "trigger_message": "refund",
                    "pending_phone": "9876543210",
                    "pending_name": "Ravi Kumar",
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
        assert captured.get("customer_name") == "Ravi Kumar"
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

    async def test_noise_replies_never_pollute_buffer(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti

        async def fake_create_support_ticket(**kwargs):
            return {
                "status": "success",
                "ticket_id": "T-42",
                "priority": "medium",
                "message": "created",
                "issue_summary": kwargs.get("issue_summary", ""),
            }

        monkeypatch.setattr(ti, "create_support_ticket", fake_create_support_ticket)
        # Trigger + phone reply → the buffer holds ONLY the trigger's intent;
        # the phone number (PII) and the "yes" read-back confirmation are noise.
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="my phone is 9876543210",
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
        assert state == TicketIntakeState.VERIFYING_PHONE
        assert pending["user_intents"] == ["order damaged"]

    async def test_accumulated_intents_flow_through_to_ticket(self, monkeypatch):
        from src.app.agent.brain import ticket_intake as ti
        captured = {}

        async def spy_create_support_ticket(**kwargs):
            captured.update(kwargs)
            return {
                "status": "success",
                "ticket_id": "T-42",
                "priority": "medium",
                "message": "created",
                "issue_summary": kwargs.get("issue_summary", ""),
            }

        monkeypatch.setattr(ti, "create_support_ticket", spy_create_support_ticket)

        meta = {
            "ticket_intake_state": TicketIntakeState.AWAITING_CONTACT,
            "ticket_intake_pending": {"trigger_message": "connect to customer care"},
        }
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="my phone is 9876543210", session_meta=meta,
            tenant_id="t1", session_id="s1", conversation_history=[],
            store_client=None, session_service=None, language="en",
        )
        assert state == TicketIntakeState.VERIFYING_PHONE
        assert pending["pending_phone"] == "9876543210"
        assert pending["user_intents"] == ["connect to customer care"]

        meta = {"ticket_intake_state": state, "ticket_intake_pending": pending}
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="yes that's correct", session_meta=meta,
            tenant_id="t1", session_id="s1", conversation_history=[],
            store_client=None, session_service=None, language="en",
        )
        assert state == TicketIntakeState.AWAITING_NAME

        # Customer restates the issue instead of giving a name → intent buffered,
        # name re-asked (a sentence is never persisted as a name).
        meta = {"ticket_intake_state": state, "ticket_intake_pending": pending}
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="I have a problem with my last order", session_meta=meta,
            tenant_id="t1", session_id="s1", conversation_history=[],
            store_client=None, session_service=None, language="en",
        )
        assert state == TicketIntakeState.AWAITING_NAME
        assert pending["user_intents"][-1] == "problem with my last order"

        meta = {"ticket_intake_state": state, "ticket_intake_pending": pending}
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="Rahul", session_meta=meta,
            tenant_id="t1", session_id="s1", conversation_history=[],
            store_client=None, session_service=None, language="en",
        )
        assert state == TicketIntakeState.AWAITING_ISSUE
        assert pending["pending_name"] == "Rahul"

        # Generic final issue → ticket created with the accumulated detail and
        # the verified contact (Layer 2 combines them at create time).
        meta = {"ticket_intake_state": state, "ticket_intake_pending": pending}
        text, state, pending, actions = await handle_ticket_intake_turn(
            cleaned_message="connect to customer care", session_meta=meta,
            tenant_id="t1", session_id="s1", conversation_history=[],
            store_client=None, session_service=None, language="en",
        )
        assert state == TicketIntakeState.IDLE
        assert captured.get("customer_phone") == "9876543210"
        assert captured.get("customer_name") == "Rahul"
        assert captured.get("issue_summary") == "connect to customer care"
        assert captured.get("user_intents") == [
            "connect to customer care", "problem with my last order",
        ]


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

    async def _patch_persistence(self, monkeypatch):
        """Monkeypatch DB + TicketService so create_support_ticket records the
        exact `data` dict it would persist. Returns the dict for assertions."""
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
        return persisted

    async def test_combines_generic_issue_with_accumulated_detail(self, monkeypatch):
        """TK-1004: a generic final issue ('connect to customer care') joins the
        most recent specific detail buffered during intake → the merchant sees
        'Problem with my last order (Customer Care Request)', never a bare
        hand-off request."""
        import src.app.services.ticketing as svc

        persisted = await self._patch_persistence(monkeypatch)
        result = await svc.create_support_ticket(
            tenant_id="t1",
            session_id="s1",
            conversation_history=[],
            store_client=None,
            customer_phone="9876543210",
            issue_summary="connect to customer care",
            user_intents=[
                "connect to customer care",
                "problem with my last order",
            ],
            source="deterministic",
        )
        assert result["status"] == "success"
        assert persisted["issue_summary"] == "Problem with my last order (Customer Care Request)"
        assert result["issue_summary"] == "Problem with my last order (Customer Care Request)"

    async def test_explicit_specific_issue_not_clobbered(self, monkeypatch):
        """Regression: the passed `issue_summary` must not be wiped before
        scoring — the deterministic intake's explicit issue wins over both the
        accumulation engine and chat-history inference."""
        import src.app.services.ticketing as svc

        persisted = await self._patch_persistence(monkeypatch)
        result = await svc.create_support_ticket(
            tenant_id="t1",
            session_id="s1",
            conversation_history=[
                {"role": "user", "content": "can you help with anything?"},
            ],
            store_client=None,
            issue_summary="Item arrived broken",
            user_intents=["connect to customer care"],
            source="deterministic",
        )
        assert result["status"] == "success"
        assert persisted["issue_summary"] == "Item arrived broken"
        assert result["issue_summary"] == "Item arrived broken"


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
