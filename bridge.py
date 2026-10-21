"""Step 3: one call, two sockets, two concurrent readers, and a small tool worker.

This module contains the conversation protocol. The web framework only supplies
the accepted Twilio WebSocket. Raw JSON makes each boundary visible for teaching.
See docs/WALKTHROUGH.md for the event sequence and deliberate demo limitations.
"""

import asyncio
import json
import logging

from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect

from config import Settings
from demo_tools import OPENING, run_tool, session_start


logger = logging.getLogger("uvicorn.error")
LIVE_URL = "wss://api.openai.com/v1/live/sessions"


class BridgeError(Exception):
    """An invalid event or failed startup prevents this call from continuing."""


def connect_live(settings: Settings):
    """Create an async connection context; entering it opens the OpenAI socket.

    Authentication happens on our trusted server. Compression is disabled to
    avoid unnecessary compression work on audio. Timeouts bound failed startup
    and cleanup; a small receive queue applies backpressure rather than allowing
    unlimited memory growth. No automatic reconnection can replay a tool call.
    """
    return connect(
        LIVE_URL,
        additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        user_agent_header="twilio-live-teaching-demo/Python 1.0.0",
        compression=None,
        open_timeout=settings.startup_timeout_seconds,
        close_timeout=settings.close_timeout_seconds,
        max_queue=8,
    )


class CallBridge:
    """Own mutable state for exactly one caller; never share this object across calls.

    ``ready`` means OpenAI acknowledged session.start, not merely that TCP opened.
    ``closed`` means final Live usage arrived, not merely that a socket closed.
    ``closing`` suppresses new audio/tool work while final events are drained.
    Socket sends are serialized because audio and tool tasks share one connection.
    """

    def __init__(self, twilio_socket, settings: Settings, connector=connect_live):
        """Inject transports so local tests can substitute sockets with queues."""
        self.twilio = twilio_socket
        self.settings = settings
        self.connector = connector
        self.openai = None
        self.stream_sid = None
        self.call_sid = None
        self.session_id = None
        self.ready = asyncio.Event()
        self.closed = asyncio.Event()
        self.closing = False
        self.send_lock = asyncio.Lock()
        self.tool_lock = asyncio.Lock()
        self.tool_tasks = set()
        self.seen_calls = set()
        self.voice_seconds = None

    async def send_openai(self, event: dict) -> None:
        """Encode one JSON event and keep concurrent writers from interleaving."""
        async with self.send_lock:
            await self.openai.send(json.dumps(event))

    async def wait_for_twilio_start(self) -> None:
        """Read connection metadata before paying to open the Live connection.

        Twilio sends `connected`, then `start`, which supplies a Stream SID and
        audio format. Call SID identifies the call; Stream SID identifies its
        media stream. Only the latter routes outgoing media to Twilio.
        """
        while True:
            event = await self.twilio.receive_json()
            if event.get("event") == "connected":
                continue
            if event.get("event") != "start":
                raise BridgeError("Expected Twilio start metadata.")
            start = event.get("start", {})
            audio = start.get("mediaFormat", {})
            if (audio.get("encoding") != "audio/x-mulaw"
                    or audio.get("sampleRate") != 8000 or audio.get("channels") != 1):
                raise BridgeError("Expected mono G.711 mu-law audio at 8000 Hz.")
            self.stream_sid = start.get("streamSid")
            self.call_sid = start.get("callSid")
            if not self.stream_sid or not self.call_sid:
                raise BridgeError("Twilio start is missing call/stream identifiers.")
            return

    async def read_twilio(self) -> None:
        """Forward caller audio after Live is ready; return when the caller stops.

        Pre-ready audio is deliberately dropped, matching the source tutorial.
        Ask callers to wait for the greeting; buffering early speech is a later
        enhancement. DTMF and playback marks are unused by this minimum demo.
        """
        try:
            while True:
                event = await self.twilio.receive_json()
                if event.get("event") == "stop":
                    return
                if event.get("event") == "start":
                    raise BridgeError("A call may only start its stream once.")
                if event.get("event") == "media" and self.ready.is_set():
                    payload = event.get("media", {}).get("payload")
                    if not isinstance(payload, str):
                        raise BridgeError("Twilio media payload must be base64 text.")
                    await self.send_openai({"type": "session.input_audio.append", "audio": payload})
        except WebSocketDisconnect:
            # A network disconnect has the same local cleanup path as `stop`.
            return

    async def read_openai(self) -> None:
        """Receive Live events continuously, even while our demo tool is sleeping.

        The startup deadline doesn't reset when unrelated events arrive. Once
        ready, the call stays open until a provider/caller closes it. GPT-Live
        sends continuous speech/silence audio, not Realtime response audio turns.
        """
        deadline = asyncio.get_running_loop().time() + self.settings.startup_timeout_seconds
        while True:
            timeout = None if self.ready.is_set() else max(0, deadline - asyncio.get_running_loop().time())
            raw = await asyncio.wait_for(self.openai.recv(), timeout=timeout)
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "session.closed":
                self.voice_seconds = event.get("usage", {}).get("seconds")
                self.closed.set()
                return
            if kind == "error":
                # Log the error code only: raw events/messages may contain data
                # supplied by the caller. Never dump authorization or audio.
                code = str(event.get("error", {}).get("code", "unknown"))
                safe_code = code if code.isidentifier() and len(code) < 80 else "unknown"
                logger.error("OpenAI session error code=%s", safe_code)
                raise BridgeError("OpenAI rejected a session event.")
            if self.closing:
                continue
            if kind == "session.started" and not self.ready.is_set():
                self.session_id = event.get("session", {}).get("id")
                self.ready.set()
                logger.info("Live ready call=%s stream=%s session=%s",
                            self.call_sid, self.stream_sid, self.session_id)
                # Instructions request the wording; commentary triggers speech.
                # We don't use response.create here: it resumes backend work.
                await self.send_openai({"type": "session.instructions.append", "delegation_id": None,
                                        "content": f"Your first spoken line is: {json.dumps(OPENING)}"})
                await self.send_openai({"type": "session.commentary.append", "delegation_id": None,
                                        "content": OPENING})
            elif kind == "session.output_audio.delta" and self.ready.is_set():
                # Preserve codec bytes/base64; translate only the JSON envelope.
                await self.twilio.send_json({"event": "media", "streamSid": self.stream_sid,
                                             "media": {"payload": event["delta"]}})
            elif kind == "response.event":
                inner = event.get("event", {})
                if inner.get("type") in {"response.failed", "response.incomplete"}:
                    raise BridgeError("Delegated response failed or was incomplete.")
                self.schedule_tool(inner)
            elif kind == "session.usage.updated":
                # Usage updates are cumulative snapshots, so never add them.
                self.voice_seconds = event.get("usage", {}).get("seconds")
            # Transcripts are available but intentionally not logged or stored.

    def schedule_tool(self, inner_event: dict) -> None:
        """Dispatch completed function items from inside the Responses envelope.

        Hosted web search produces other item types and needs no local result.
        Scheduling a task keeps a slow function out of the audio receive loop.
        Deduplication is per call and prevents executing a repeated item twice.
        """
        item = inner_event.get("item", {})
        if (inner_event.get("type") != "response.output_item.done"
                or item.get("type") != "function_call" or item.get("status") != "completed"):
            return
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise BridgeError("Completed function call has no call_id.")
        if call_id in self.seen_calls:
            return
        self.seen_calls.add(call_id)
        task = asyncio.create_task(self.execute_tool(item))
        self.tool_tasks.add(task)
        task.add_done_callback(self.tool_tasks.discard)

    async def execute_tool(self, item: dict) -> None:
        """Run one function, return its JSON result, then continue the backend.

        Parallel calls are disabled in session configuration. A lock also keeps
        local function/continuation pairs ordered. A real blocking SDK would need
        an async client or asyncio.to_thread; async def alone doesn't unblock it.
        """
        try:
            async with self.tool_lock:
                output = await run_tool(item.get("name"), item.get("arguments"),
                                        self.settings.tool_delay_seconds)
                if self.closing:
                    return
                logger.info("Demo function completed call_id=%s", item["call_id"])
                await self.send_openai({"type": "response.item.create", "item": {
                    "type": "function_call_output", "call_id": item["call_id"],
                    "output": json.dumps(output)}})
                # Adding a result alone doesn't resume the reasoning model.
                await self.send_openai({"type": "response.create"})
        except Exception as error:
            logger.error("Tool transport failed (%s)", type(error).__name__)
            # End the receiver too; don't leave a silent orphaned tool task.
            await self.openai.close(code=1011)

    async def run(self) -> None:
        """Connect, run both readers, and finalize when either side ends.

        On caller hangup we cancel local fake work, stop sending input, request
        session.close, and keep the OpenAI reader alive for session.closed. A
        bounded wait avoids hanging forever after a broken network connection.
        """
        await asyncio.wait_for(self.wait_for_twilio_start(), self.settings.startup_timeout_seconds)
        async with self.connector(self.settings) as self.openai:
            await self.send_openai(session_start(self.settings))
            incoming = asyncio.create_task(self.read_twilio())
            outgoing = asyncio.create_task(self.read_openai())
            try:
                done, _ = await asyncio.wait({incoming, outgoing}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()  # Surface protocol/network errors to the route.
            finally:
                self.closing = True
                incoming.cancel()
                workers = list(self.tool_tasks)
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(incoming, *workers, return_exceptions=True)
                if self.ready.is_set() and not outgoing.done() and not self.closed.is_set():
                    try:
                        await self.send_openai({"type": "session.close"})
                        await asyncio.wait_for(asyncio.shield(outgoing), self.settings.close_timeout_seconds)
                    except Exception as error:
                        logger.warning("Live finalization incomplete (%s)", type(error).__name__)
                outgoing.cancel()
                await asyncio.gather(outgoing, return_exceptions=True)
                logger.info("Call ended session=%s final_usage_confirmed=%s voice_seconds=%s",
                            self.session_id, self.closed.is_set(), self.voice_seconds)
