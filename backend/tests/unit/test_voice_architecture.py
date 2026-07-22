import asyncio
import json
import pytest
from typing import Any, AsyncIterator

from src.app.agent.voice.coordinator import VoiceTurnCoordinator
from src.app.agent.voice.providers.base import BaseVoiceProvider
from src.app.agent.voice.providers.openai_realtime import resample_pcm16_16k_to_24k


# ── Upsampling Test ──────────────────────────────────────────────────────────

def test_resample_pcm16_16k_to_24k():
    # 160 samples of 16-bit PCM = 320 bytes
    input_pcm = b"\x01\x00" * 160
    output_pcm = resample_pcm16_16k_to_24k(input_pcm)
    # Ratio is 1.5, so 160 * 1.5 = 240 samples = 480 bytes
    assert len(output_pcm) == 480


# ── OpenAI Voice Provider Test ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_openai_voice_provider(monkeypatch):
    from src.app.agent.voice.providers.openai_realtime import OpenAIVoiceProvider, ResponseState

    sent_payloads = []
    class MockOpenAISocket:
        def __init__(self):
            self.closed = False
            self.events = [
                {"type": "session.updated"},
                {"type": "input_audio_buffer.speech_started"},
                {"type": "conversation.item.input_audio_transcription.completed", "transcript": "test transcript"},
                {"type": "response.output_audio_transcript.delta", "delta": "hello"},
                {"type": "response.output_audio.delta", "delta": "YQ=="}, # base64 for 'a'
                {"type": "response.function_call_arguments.done", "call_id": "call_1", "name": "ask_brain", "arguments": "{\"query\":\"find shoes\"}"},
                {"type": "response.done"},
            ]
        async def send(self, data: str):
            sent_payloads.append(json.loads(data))
        async def recv(self):
            await asyncio.sleep(0.01)
            if self.events:
                return json.dumps(self.events.pop(0))
            while not self.closed:
                await asyncio.sleep(0.01)
            raise RuntimeError("Socket closed")
        async def __aiter__(self):
            while not self.closed:
                try:
                    yield await self.recv()
                except Exception:
                    break
        async def close(self):
            self.closed = True

    async def mock_connect(url, **kwargs):
        return MockOpenAISocket()

    # Mock settings / environment
    monkeypatch.setattr("src.app.config.settings.openai_api_key", "mock-key")
    monkeypatch.setattr("websockets.connect", mock_connect)

    async def mock_get_store_config_for_tenant(tenant_id):
        return {"store_name": "Test Store"}
    monkeypatch.setattr(
        "src.app.modules.tenants.service.get_store_config_for_tenant",
        mock_get_store_config_for_tenant
    )

    provider = OpenAIVoiceProvider(session_service=None)
    await provider.connect(session_id="test_session", store_client=None, tenant_id="_dev")

    # Verify session.update was sent
    assert len(sent_payloads) == 1
    assert sent_payloads[0]["type"] == "session.update"
    assert sent_payloads[0]["session"]["audio"]["output"]["voice"] == "alloy"

    # Test send_audio_chunk
    await provider.send_audio_chunk(b"\x01\x00" * 160)
    assert len(sent_payloads) == 2
    assert sent_payloads[1]["type"] == "input_audio_buffer.append"
    # Audio should be base64-encoded
    assert isinstance(sent_payloads[1]["audio"], str)

    # Test receive_events
    provider._response_state = ResponseState.RESPONSE_CREATED
    provider._audio_delta_received = True
    events_received = []
    events_iter = provider.receive_events()
    for _ in range(6):
        events_received.append(await anext(events_iter))

    assert len(events_received) == 6
    assert events_received[0]["type"] == "flush_audio"
    assert events_received[1]["type"] == "user_transcript"
    assert events_received[1]["text"] == "test transcript"
    assert events_received[2]["type"] == "transcript"
    assert events_received[2]["text"] == "hello"
    assert events_received[3]["type"] == "audio"
    assert events_received[3]["data"] == b"a"
    assert events_received[4]["type"] == "tool_call"
    assert events_received[4]["call_id"] == "call_1"
    assert events_received[4]["arguments"] == {"query": "find shoes"}
    assert events_received[5]["type"] == "turn_complete"

    # Test send_tool_response
    await provider.send_tool_response(call_id="call_1", name="ask_brain", response="done")
    assert len(sent_payloads) == 4
    assert sent_payloads[2]["type"] == "conversation.item.create"
    assert sent_payloads[2]["item"]["call_id"] == "call_1"
    assert sent_payloads[3]["type"] == "response.create"

    # Test manual cancel
    provider._response_state = ResponseState.STREAMING_AUDIO
    await provider.cancel_response()
    assert len(sent_payloads) == 5
    assert sent_payloads[4]["type"] == "response.cancel"

    # Test close
    await provider.close()
    assert provider.ws is None


# ── Mocks for Coordinator Test ────────────────────────────────────────────────

class MockWebSocket:
    def __init__(self, client_messages: list[dict]):
        self.client_messages = client_messages
        self.sent_messages = []
        self.sent_bytes = []
        self._msg_index = 0
        self.closed = False
        self.close_code = None

    async def receive(self) -> dict:
        while not self.closed:
            if self._msg_index < len(self.client_messages):
                msg = self.client_messages[self._msg_index]
                self._msg_index += 1
                await asyncio.sleep(0.01)
                return msg
            await asyncio.sleep(0.01)
        return {"type": "websocket.disconnect"}

    async def send_text(self, payload: str) -> None:
        self.sent_messages.append(json.loads(payload))

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int = 1000) -> None:
        self.closed = True
        self.close_code = code


class MockVoiceProvider(BaseVoiceProvider):
    def __init__(self):
        self.session_id = None
        self.tenant_id = None
        self.connected = False
        self.sent_chunks = []
        self.tool_responses = []
        self.closed = False
        self.events_queue = asyncio.Queue()

    async def connect(self, session_id: str, store_client: Any, tenant_id: str) -> None:
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.connected = True

    async def send_audio_chunk(self, chunk: bytes) -> None:
        self.sent_chunks.append(chunk)

    async def send_text_input(self, text: str, language: str, cart_context: Any) -> None:
        pass

    async def receive_events(self) -> AsyncIterator[dict]:
        while self.connected:
            event = await self.events_queue.get()
            if event is None:
                break
            yield event

    async def send_tool_response(self, call_id: str, name: str, response: str) -> None:
        self.tool_responses.append({"call_id": call_id, "name": name, "response": response})

    async def close(self) -> None:
        self.connected = False
        self.closed = True
        await self.events_queue.put(None)


class MockSessionService:
    pass


class MockOrchestrator:
    async def run(
        self,
        session_id: str,
        user_message: str,
        language: str,
        cart_context: Any,
        tenant_id: str,
        store_context: Any = None,
        page_context: Any = None,
    ):
        # Simulate brain latency
        await asyncio.sleep(0.05)
        return {
            "speech_text": f"Brain reply to {user_message}",
            "ui_actions": [{"type": "show_products", "payload": {"products": []}}],
            "suggested_replies": ["Yes", "No"],
        }


# ── Turn Coordinator Tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_coordinator_flow(monkeypatch):
    # Mock AgentOrchestrator to avoid database / API dependencies in unit tests
    monkeypatch.setattr(
        "src.app.agent.voice.coordinator.AgentOrchestrator",
        lambda *args, **kwargs: MockOrchestrator()
    )
    async def mock_get_store_config(tenant_id):
        return {"store_name": "Test Store", "currency_symbol": "$", "store_url": "https://example.com"}
    monkeypatch.setattr(
        "src.app.modules.tenants.service.get_store_config_for_tenant",
        mock_get_store_config
    )

    ws_messages = [
        {"type": "websocket.receive", "bytes": b"\x01\x02\x03"},  # Mic frame
        {"type": "websocket.receive", "bytes": b"\x04\x05\x06"},  # Mic frame
        {"type": "websocket.receive", "text": json.dumps({"type": "text_input", "text": "find shirts"})},
    ]

    ws = MockWebSocket(ws_messages)
    provider = MockVoiceProvider()
    session_service = MockSessionService()

    coordinator = VoiceTurnCoordinator(ws, provider, session_service)
    
    # Start coordinator run loop in background
    run_task = asyncio.create_task(coordinator.run("test_session", None, "_dev"))

    # Let the coordinator connect and start receiving client messages
    await asyncio.sleep(0.1)

    # Verify connection was established
    assert provider.connected
    assert provider.session_id == "test_session"
    assert provider.tenant_id == "_dev"

    # Verify mic audio chunks were forwarded to provider
    assert len(provider.sent_chunks) == 2
    assert provider.sent_chunks[0] == b"\x01\x02\x03"

    # Verify text input echoed and ran brain turn
    # Echo event is user_transcript
    user_echoes = [m for m in ws.sent_messages if m.get("type") == "user_transcript"]
    assert len(user_echoes) == 1
    assert user_echoes[0]["text"] == "find shirts"

    # Give brain execution time to complete
    await asyncio.sleep(0.1)

    # Verify UI actions, suggestions, and bot transcript sent back to client
    ui_actions = [m for m in ws.sent_messages if m.get("type") == "ui_action"]
    assert len(ui_actions) == 1
    assert ui_actions[0]["action"]["type"] == "show_products"

    suggestions = [m for m in ws.sent_messages if m.get("type") == "suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["items"] == ["Yes", "No"]

    bot_transcripts = [m for m in ws.sent_messages if m.get("type") == "transcript"]
    assert len(bot_transcripts) == 1
    assert bot_transcripts[0]["text"] == "Brain reply to find shirts"

    # Clean up coordinator run task
    ws.closed = True
    await provider.close()
    await run_task


@pytest.mark.asyncio
async def test_coordinator_mic_gating(monkeypatch):
    monkeypatch.setattr(
        "src.app.agent.voice.coordinator.AgentOrchestrator",
        lambda *args, **kwargs: MockOrchestrator()
    )
    async def mock_get_store_config(tenant_id):
        return {"store_name": "Test Store", "currency_symbol": "$", "store_url": "https://example.com"}
    monkeypatch.setattr(
        "src.app.modules.tenants.service.get_store_config_for_tenant",
        mock_get_store_config
    )

    ws_messages = [
        {"type": "websocket.receive", "bytes": b"\x01"},
    ]
    ws = MockWebSocket(ws_messages)
    provider = MockVoiceProvider()
    coordinator = VoiceTurnCoordinator(ws, provider, MockSessionService())

    run_task = asyncio.create_task(coordinator.run("test_session", None, "_dev"))
    await asyncio.sleep(0.02)

    # Mic is active initially, so chunk is forwarded
    assert len(provider.sent_chunks) == 1

    # Disable mic (simulates bot speaking/synthesis)
    coordinator._mic_enabled = False

    # Simulate sending another audio chunk directly via WebSocket while mic disabled
    ws.client_messages.append({"type": "websocket.receive", "bytes": b"\x99"})
    await asyncio.sleep(0.05)

    # The new \x99 chunk should be IGNORED and not forwarded to provider
    assert b"\x99" not in provider.sent_chunks

    ws.closed = True
    await provider.close()
    await run_task


@pytest.mark.asyncio
async def test_coordinator_barge_in_cancel(monkeypatch):
    # Mock brain with longer execution latency so we can interrupt it
    class DelayedOrchestrator:
        async def run(self, *args, **kwargs):
            await asyncio.sleep(2.0)
            return {"speech_text": "Slow response"}

    monkeypatch.setattr(
        "src.app.agent.voice.coordinator.AgentOrchestrator",
        lambda *args, **kwargs: DelayedOrchestrator()
    )
    async def mock_get_store_config(tenant_id):
        return {"store_name": "Test Store", "currency_symbol": "$", "store_url": "https://example.com"}
    monkeypatch.setattr(
        "src.app.modules.tenants.service.get_store_config_for_tenant",
        mock_get_store_config
    )

    ws = MockWebSocket([])
    provider = MockVoiceProvider()
    coordinator = VoiceTurnCoordinator(ws, provider, MockSessionService())

    run_task = asyncio.create_task(coordinator.run("test_session", None, "_dev"))
    await asyncio.sleep(0.02)

    # Start slow brain task
    await coordinator.run_brain_turn("test query", "en", "call_1", "ask_brain")
    await asyncio.sleep(0.1)

    # Brain task is currently running
    assert coordinator._active_brain_task is not None
    assert not coordinator._active_brain_task.done()

    # Provider signals flush_audio (user barge-in/interruption)
    await provider.events_queue.put({"type": "flush_audio"})
    await asyncio.sleep(0.05)

    # Verify brain task was cancelled
    assert coordinator._active_brain_task.cancelled() or coordinator._active_brain_task.done()
    
    # Verify flush_audio message was sent to client
    flushes = [m for m in ws.sent_messages if m.get("type") == "flush_audio"]
    assert len(flushes) == 1

    ws.closed = True
    await provider.close()
    await run_task
