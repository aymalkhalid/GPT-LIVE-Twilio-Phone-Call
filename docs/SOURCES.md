# Sources and verification boundaries

Documentation checked on **2026-10-18**. Model availability and account permissions
are not established by reading documentation or finding a local key.

## Implementation references

- [Twilio's GPT-Live Node.js tutorial](https://www.twilio.com/en-us/blog/developers/tutorials/integrations/voice-ai-assistant-openai-gpt-live-1-node)
  — the starting example: inbound call webhook, bidirectional media bridge,
  managed Responses delegation, a mock headline function, and hosted search.
- [OpenAI: GPT-Live WebSockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=live)
  — endpoint, session handshake, audio formats, event envelopes, and close behavior.
- [OpenAI: delegation and tools](https://developers.openai.com/api/docs/guides/live-delegation)
  — nested function-call events, application-executed functions, tool-result items,
  and the explicit continuation command.
- [OpenAI: managing GPT-Live sessions](https://developers.openai.com/api/docs/guides/live-conversations)
  — cumulative voice usage and graceful finalization. The implementation requests
  finalization rather than immediately dropping the provider connection.
- [OpenAI: telephony and SIP / audio bridges](https://developers.openai.com/api/docs/guides/voice-sip?api=live)
  — matching G.711 codec passthrough and provider playback responsibilities.
- [Twilio: Media Streams](https://www.twilio.com/docs/voice/media-streams)
  — bidirectional streams and `<Connect><Stream>` call instructions.
- [Twilio: WebSocket messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages)
  — start metadata, mono μ-law at 8 kHz, base64 media, and playback mark/clear messages.
- [Twilio: request signatures](https://www.twilio.com/docs/usage/security)
  — SDK validation using the public URL/form data, and the documented WSS
  trailing-slash consideration. The demo accepts HTTPS/WSS representations at
  the configured origin; real handshake behavior remains a live-test checkpoint.
- [FastAPI: WebSockets](https://fastapi.tiangolo.com/advanced/websockets/)
  — accepted connections and disconnect handling.
- [websockets: asyncio client](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)
  — modern `additional_headers`, connection contexts, and network timeouts.

## Deliberate choices

This is an independently annotated Python implementation of the tutorial's
architecture. The fictional tool is deterministic and explicitly labeled; it
does not return live headlines. The model defaults retain the tutorial's
`gpt-live-1` and `gpt-5.6-terra`; they are not a claim that either is the newest
choice or that the local account has access.

Official OpenAI documentation recommends WebRTC for direct Live audio
connections, including servers; WebSockets are also supported. This demo uses
WebSockets to match Twilio Media Streams and keep both message boundaries visible.

Request authentication, bounded startup/finalization, a separate tool task, and
offline protocol tests make the teaching example more inspectable. They do not
turn it into a finished production receptionist.

## What local verification establishes

The offline tests use fake keys, fake provider events, and in-process app routing.
They establish our application behavior for the tested sequences. No OpenAI
session, Twilio call, hosted web search, public tunnel, or phone-number update is
performed by the suite.

A real phone test is still required for model permissions, actual event delivery,
audible greeting/response, tool use chosen by the model, search quality, Twilio's
real handshake signature, network latency, interruptions, and final usage delivery.

## Local verification record — 2026-10-18

| Check | Result |
| --- | --- |
| Python | 3.14.0, Windows, project-local virtual environment. |
| Offline suite | 20 tests passed, including two-call isolation and continued audio during a blocked tool. |
| Actual server startup | Uvicorn started on an ephemeral `127.0.0.1` port using the real app factory and existing `.env`. |
| Actual HTTP liveness | `GET /` returned HTTP 200 with `status: ok`; the temporary server was then shut down. |
| Python compilation | Application modules and tests compiled successfully. |
| Dependency consistency | `pip check`: no broken requirements. Tested versions are pinned in `requirements-lock.txt`. |
| Local credential presence | The app's dotenv loader found nonempty OpenAI key and Twilio Auth Token values; no values were printed. |
| Public configuration | `PUBLIC_BASE_URL` was absent; no tunnel or number configuration was created. |
| Real provider verification | Not performed: no OpenAI sessions, hosted searches, or Twilio phone calls. |

The installed Starlette TestClient emitted a deprecation warning about its HTTPX
adapter. It did not affect the passing tests; the lock file preserves the tested
combination. This warning is separate from application/provider behavior.
