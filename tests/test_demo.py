"""Offline protocol tests: all keys, callers, audio, and provider sockets are fake.

Run: python -m unittest discover -s tests -v
These tests verify our event routing and cleanup, not model access, speech
quality, real phone playback, hosted search, or what a live caller hears.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from app import create_app
from bridge import BridgeError, CallBridge, connect_live
from config import Settings
from demo_tools import run_tool, session_start


# No test calls Settings.from_env; fake settings keep real .env secrets unused.
SETTINGS = Settings(openai_api_key="fake-local-test-key", twilio_auth_token="fake-token",
                    public_base_url="https://demo.example", enable_web_search=False,
                    startup_timeout_seconds=0.1, close_timeout_seconds=0.05,
                    tool_delay_seconds=0)
START = {"event": "start", "start": {
    "streamSid": "MZ_test", "callSid": "CA_test",
    "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1}}}


def function_event(call_id="call_test", arguments='{"city":"Karachi"}'):
    """Match the nested event envelope documented for Responses delegation."""
    return {"type": "response.event", "delegation_id": "delegation_test", "event": {
        "type": "response.output_item.done", "item": {
            "type": "function_call", "status": "completed", "call_id": call_id,
            "name": "get_top_headline", "arguments": arguments}}}


class FakeLive:
    """A queue-backed OpenAI socket that acknowledges startup and graceful close."""

    def __init__(self, *, acknowledge_start=True, acknowledge_close=True, initial_events=()):
        self.events = asyncio.Queue()
        self.sent = []
        self.acknowledge_start = acknowledge_start
        self.acknowledge_close = acknowledge_close
        self.initial_events = initial_events
        self.transport_closed = False

    async def send(self, raw):
        """Generate just enough server events to exercise the real bridge."""
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.start" and self.acknowledge_start:
            await self.events.put({"type": "session.started", "session": {"id": "live_test"}})
            for incoming in self.initial_events:
                await self.events.put(incoming)
        elif event["type"] == "session.input_audio.append":
            await self.events.put({"type": "session.output_audio.delta", "delta": event["audio"]})
        elif event["type"] == "session.close" and self.acknowledge_close:
            await self.events.put({"type": "session.closed", "usage": {"seconds": 15}})

    async def recv(self):
        """Block until an event arrives, as a real WebSocket receiver would."""
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return json.dumps(event)

    async def close(self, code=1000):
        """Expose transport closure so cleanup assertions can detect leaks."""
        self.transport_closed = True
        await self.events.put(ConnectionError("fake socket closed"))


class FakeTwilio:
    """A caller whose input and received audio are independently controllable."""

    def __init__(self):
        self.events = asyncio.Queue()
        self.audio = asyncio.Queue()

    async def receive_json(self):
        """Receive fake caller events or simulate a disconnected phone."""
        event = await self.events.get()
        if isinstance(event, Exception):
            raise event
        return event

    async def send_json(self, event):
        """Record bytes delivered toward the fake caller's speaker."""
        await self.audio.put(event)


def fake_connector(provider):
    """Replace only network establishment; keep the application's bridge intact."""
    @asynccontextmanager
    async def connection(settings):
        try:
            yield provider
        finally:
            await provider.close()
    return connection


async def eventually(predicate):
    """Bound asynchronous assertions so a broken bridge fails instead of hanging."""
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0.001)


class ToolTests(unittest.IsolatedAsyncioTestCase):
    """Check behavior at the model-to-business-function boundary."""

    async def test_fictional_result_is_deterministic(self):
        first = await run_tool("get_top_headline", '{"city":"Karachi"}', 0)
        self.assertEqual(first, await run_tool("get_top_headline", '{"city":"Karachi"}', 0))
        self.assertTrue(first["fictional"])

    async def test_bad_arguments_and_unknown_function_return_errors(self):
        for args in ("not json", "[]", '{"city":""}', '{"city":12}', '{"city":"x","extra":1}'):
            self.assertIn("error", await run_tool("get_top_headline", args, 0))
        self.assertIn("error", await run_tool("missing", "{}", 0))


class RouteTests(unittest.TestCase):
    """Exercise real FastAPI routing/signature checks with no provider calls."""

    def setUp(self):
        """Fail loudly if a supposedly local route tries to open OpenAI."""
        self.connector_calls = 0

        def forbidden_connector(settings):
            self.connector_calls += 1
            raise AssertionError("No provider connection expected")

        self.app = create_app(SETTINGS, connector=forbidden_connector)
        self.validator = RequestValidator(SETTINGS.twilio_auth_token)

    def test_health_is_local(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get("/").json()["status"], "ok")
        self.assertEqual(self.connector_calls, 0)

    def test_signed_webhook_returns_expected_twiml(self):
        fields = {"CallSid": "CA_test", "From": "+15555550123"}
        signature = self.validator.compute_signature("https://demo.example/incoming-call", fields)
        with TestClient(self.app) as client:
            response = client.post("/incoming-call", data=fields,
                                   headers={"X-Twilio-Signature": signature, "Host": "untrusted.example"})
        self.assertEqual(response.status_code, 200)
        stream = ElementTree.fromstring(response.text).find("Connect/Stream")
        self.assertEqual(stream.attrib["url"], "wss://demo.example/media-stream")
        self.assertEqual(self.connector_calls, 0)

    def test_unsigned_or_tampered_webhook_is_rejected(self):
        signature = self.validator.compute_signature("https://demo.example/incoming-call", {"CallSid": "CA_original"})
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/incoming-call", data={}).status_code, 403)
            response = client.post("/incoming-call", data={"CallSid": "CA_changed"},
                                   headers={"X-Twilio-Signature": signature})
            self.assertEqual(response.status_code, 403)

    def test_unsigned_websocket_never_opens_openai(self):
        with TestClient(self.app) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect("/media-stream"):
                    pass
        self.assertEqual(self.connector_calls, 0)

    def test_missing_public_url_returns_configuration_error(self):
        with TestClient(create_app(replace(SETTINGS, public_base_url=""))) as client:
            self.assertEqual(client.post("/incoming-call").status_code, 503)

    def test_hangup_before_live_ready_releases_transport(self):
        provider = FakeLive(acknowledge_start=False)
        signature = self.validator.compute_signature(SETTINGS.media_url, {})
        with TestClient(create_app(SETTINGS, fake_connector(provider))) as client:
            with client.websocket_connect("/media-stream", headers={"X-Twilio-Signature": signature}) as ws:
                ws.send_json({"event": "connected"})
                ws.send_json(START)
                ws.send_json({"event": "stop"})
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
        self.assertTrue(provider.transport_closed)
        self.assertFalse(any(event["type"] == "session.close" for event in provider.sent))

    def test_signed_route_delivers_provider_audio(self):
        provider = FakeLive(initial_events=({"type": "session.output_audio.delta", "delta": "////"},))
        signature = self.validator.compute_signature(SETTINGS.media_url, {})
        with TestClient(create_app(SETTINGS, fake_connector(provider))) as client:
            with client.websocket_connect("/media-stream", headers={"X-Twilio-Signature": signature}) as ws:
                ws.send_json(START)
                self.assertEqual(ws.receive_json()["media"]["payload"], "////")
                ws.send_json({"event": "stop"})
                with self.assertRaises(WebSocketDisconnect):
                    ws.receive_json()
        self.assertTrue(any(event["type"] == "session.close" for event in provider.sent))


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    """Test ordering and concurrency using the complete two-reader lifecycle."""

    async def start_bridge(self, settings=SETTINGS, provider=None):
        """Start a valid call and wait for readiness without touching the network."""
        provider = provider or FakeLive()
        caller = FakeTwilio()
        await caller.events.put(START)
        bridge = CallBridge(caller, settings, fake_connector(provider))
        task = asyncio.create_task(bridge.run())
        # Always clean up even if an assertion fails halfway through a test.
        async def cleanup():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        await asyncio.wait_for(bridge.ready.wait(), 1)
        return bridge, caller, provider, task

    async def test_audio_round_trip_and_graceful_hangup(self):
        bridge, caller, provider, task = await self.start_bridge()
        await caller.events.put({"event": "media", "media": {"payload": "//8="}})
        audio = await asyncio.wait_for(caller.audio.get(), 1)
        self.assertEqual(audio, {"event": "media", "streamSid": "MZ_test", "media": {"payload": "//8="}})
        await caller.events.put({"event": "stop"})
        await asyncio.wait_for(task, 1)
        kinds = [event["type"] for event in provider.sent]
        self.assertEqual(kinds[0], "session.start")
        self.assertIn("session.commentary.append", kinds)
        self.assertTrue(bridge.closed.is_set())
        self.assertEqual(bridge.voice_seconds, 15)
        self.assertTrue(provider.transport_closed)

    async def test_slow_tool_does_not_block_audio_and_duplicate_is_ignored(self):
        bridge, caller, provider, task = await self.start_bridge()
        entered, release = asyncio.Event(), asyncio.Event()

        async def held_tool(*args):
            entered.set()
            await release.wait()
            return {"fictional": True}

        with patch("bridge.run_tool", side_effect=held_tool) as tool:
            await provider.events.put(function_event())
            await asyncio.wait_for(entered.wait(), 1)
            await provider.events.put(function_event())
            # This output must reach Twilio WHILE the tool is still blocked.
            await provider.events.put({"type": "session.output_audio.delta", "delta": "//8="})
            self.assertEqual((await asyncio.wait_for(caller.audio.get(), 1))["media"]["payload"], "//8=")
            release.set()
            await eventually(lambda: any(event["type"] == "response.create" for event in provider.sent))
            self.assertEqual(tool.call_count, 1)
        result = next(event for event in provider.sent if event["type"] == "response.item.create")
        self.assertEqual(result["item"]["call_id"], "call_test")
        self.assertTrue(json.loads(result["item"]["output"])["fictional"])
        self.assertLess(provider.sent.index(result), next(i for i, event in enumerate(provider.sent) if event["type"] == "response.create"))
        await caller.events.put({"event": "stop"})
        await task

    async def test_hangup_cancels_local_tool(self):
        bridge, caller, provider, task = await self.start_bridge()
        entered, canceled = asyncio.Event(), asyncio.Event()

        async def held_tool(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        with patch("bridge.run_tool", side_effect=held_tool):
            await provider.events.put(function_event())
            await asyncio.wait_for(entered.wait(), 1)
            await caller.events.put({"event": "stop"})
            await asyncio.wait_for(task, 1)
        self.assertTrue(canceled.is_set())
        self.assertFalse(any(event["type"] == "response.item.create" for event in provider.sent))
        self.assertTrue(bridge.closed.is_set())

    async def test_provider_close_ends_call(self):
        bridge, caller, provider, task = await self.start_bridge()
        await provider.events.put({"type": "session.closed", "usage": {"seconds": 4}})
        await asyncio.wait_for(task, 1)
        self.assertTrue(bridge.closed.is_set())

    async def test_startup_timeout_releases_transport(self):
        caller, provider = FakeTwilio(), FakeLive(acknowledge_start=False)
        await caller.events.put(START)
        bridge = CallBridge(caller, SETTINGS, fake_connector(provider))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(bridge.run(), 1)
        self.assertTrue(provider.transport_closed)

    async def test_missing_final_event_is_bounded_and_unconfirmed(self):
        bridge, caller, provider, task = await self.start_bridge(provider=FakeLive(acknowledge_close=False))
        await caller.events.put({"event": "stop"})
        with self.assertLogs("uvicorn.error", level="WARNING") as logs:
            await asyncio.wait_for(task, 1)
        self.assertIn("finalization incomplete", logs.output[0])
        self.assertFalse(bridge.closed.is_set())
        self.assertTrue(provider.transport_closed)

    async def test_provider_error_terminates_and_releases_transport(self):
        bridge, caller, provider, task = await self.start_bridge()
        with self.assertLogs("uvicorn.error", level="ERROR") as logs:
            await provider.events.put({"type": "error", "error": {
                "code": "responses_validation_failed", "message": "private provider details"}})
            with self.assertRaises(BridgeError):
                await asyncio.wait_for(task, 1)
        self.assertIn("responses_validation_failed", logs.output[0])
        self.assertNotIn("private provider details", " ".join(logs.output))
        self.assertTrue(provider.transport_closed)

    async def test_unexpected_provider_disconnect_is_unconfirmed(self):
        bridge, caller, provider, task = await self.start_bridge()
        await provider.events.put(ConnectionError("fake network failure"))
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(task, 1)
        self.assertFalse(bridge.closed.is_set())
        self.assertTrue(provider.transport_closed)

    async def test_two_calls_keep_their_audio_and_stream_ids_separate(self):
        first, caller1, provider1, task1 = await self.start_bridge()
        second, caller2, provider2, task2 = await self.start_bridge()
        second.stream_sid = "MZ_other"
        await caller1.events.put({"event": "media", "media": {"payload": "AQ=="}})
        await caller2.events.put({"event": "media", "media": {"payload": "Ag=="}})
        audio1 = await asyncio.wait_for(caller1.audio.get(), 1)
        audio2 = await asyncio.wait_for(caller2.audio.get(), 1)
        self.assertEqual((audio1["streamSid"], audio1["media"]["payload"]), ("MZ_test", "AQ=="))
        self.assertEqual((audio2["streamSid"], audio2["media"]["payload"]), ("MZ_other", "Ag=="))
        await caller1.events.put({"event": "stop"})
        await caller2.events.put({"event": "stop"})
        await asyncio.wait_for(asyncio.gather(task1, task2), 1)

    async def test_wrong_codec_is_rejected_before_openai(self):
        caller = FakeTwilio()
        await caller.events.put({"event": "start", "start": {
            **START["start"], "mediaFormat": {"encoding": "audio/pcm", "sampleRate": 24000, "channels": 1}}})
        def forbidden(settings):
            raise AssertionError("Wrong codec must never open a provider session")
        with self.assertRaises(BridgeError):
            await CallBridge(caller, SETTINGS, forbidden).run()

    async def test_session_configuration_preserves_live_protocol(self):
        event = session_start(SETTINGS)
        self.assertEqual(event["session"]["model"], "gpt-live-1")
        self.assertEqual(event["session"]["delegation"]["responses"]["model"], "gpt-5.6-terra")
        self.assertEqual(event["session"]["audio"]["format"], {"type": "audio/pcmu", "rate": 8000})
        self.assertFalse(event["session"]["delegation"]["responses"]["parallel_tool_calls"])
        with patch("bridge.connect") as connect:
            connect_live(SETTINGS)
            self.assertEqual(connect.call_args.args[0], "wss://api.openai.com/v1/live/sessions")
            self.assertIn("Authorization", connect.call_args.kwargs["additional_headers"])


if __name__ == "__main__":
    unittest.main()
