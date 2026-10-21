"""Step 4: HTTP call instructions and an authenticated WebSocket entry point.

Run with: python -m uvicorn app:create_app --factory --host 127.0.0.1 --port 5050
The factory loads .env only when an app is created, never when modules import.
This also lets offline tests provide fake settings and a fake OpenAI connector.
"""

import logging

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, VoiceResponse

from bridge import CallBridge, connect_live
from config import Settings


logger = logging.getLogger("uvicorn.error")


def create_app(settings: Settings | None = None, connector=connect_live) -> FastAPI:
    """Build a small ASGI app; opening a real media stream is the paid boundary.

    Health checks and returning TwiML do not contact OpenAI or Twilio APIs. A
    signed /media-stream connection followed by valid start metadata opens Live.
    We never create outbound calls or modify the Twilio phone number ourselves.
    """
    settings = settings if settings is not None else Settings.from_env()
    app = FastAPI(title="Twilio GPT-Live teaching demo", docs_url=None, redoc_url=None)
    validator = RequestValidator(settings.twilio_auth_token)

    @app.get("/")
    async def health() -> dict:
        """A local/public liveness check; it proves no provider/model access."""
        return {"status": "ok", "demo": "Twilio GPT-Live audio bridge"}

    @app.post("/incoming-call")
    async def incoming_call(request: Request) -> Response:
        """Validate Twilio's POST and return XML instructions for this call.

        The signature covers the public URL plus ALL form fields. Use the Twilio
        SDK rather than reimplementing its signing algorithm. An explicit public
        origin survives TLS termination at a tunnel without trusting Host headers.
        TwiML generation is local: it doesn't place a call or send credentials.
        """
        try:
            settings.require_call_settings()
        except ValueError as error:
            raise HTTPException(status_code=503, detail=str(error)) from None
        public_url = settings.public_base_url + request.url.path
        if request.url.query:
            public_url += "?" + request.url.query
        fields = await request.form()
        if not validator.validate(public_url, fields, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature.")
        response = VoiceResponse()
        connection = Connect()
        connection.stream(url=settings.media_url)
        response.append(connection)
        return Response(content=str(response), media_type="application/xml")

    @app.websocket("/media-stream")
    async def media_stream(socket: WebSocket) -> None:
        """Authenticate before accepting a stream or opening a billable AI session.

        A WebSocket starts as an HTTP upgrade. Validate only our configured public
        origin, allowing HTTPS/WSS URL representations and Twilio's documented
        trailing-slash variant. We don't accept arbitrary forwarded hostnames.
        The TwiML endpoint uses no query parameters on this stream URL.
        """
        try:
            settings.require_call_settings()
        except ValueError:
            await socket.close(code=1008)
            return
        signature = socket.headers.get("x-twilio-signature", "")
        urls = (settings.media_url, settings.public_base_url + "/media-stream")
        valid = not socket.url.query and any(
            validator.validate(url + suffix, {}, signature)
            for url in urls for suffix in ("", "/")
        )
        if not valid:
            await socket.close(code=1008)
            return
        await socket.accept()
        code = 1000
        try:
            await CallBridge(socket, settings, connector).run()
        except WebSocketDisconnect:
            pass
        except Exception as error:
            # Avoid exception bodies/tracebacks that could expose request data.
            logger.error("Call bridge stopped (%s)", type(error).__name__)
            code = 1011
        finally:
            if socket.client_state == WebSocketState.CONNECTED:
                try:
                    await socket.close(code=code)
                except (RuntimeError, WebSocketDisconnect):
                    pass  # The caller may disconnect while cleanup is running.

    return app
