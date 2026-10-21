# Teach the demo one boundary at a time

The goal is to explain what crosses each connection, who owns each operation,
and why the program remains responsive while tools run. Read the functions in
the order below. The numbered steps are teaching checkpoints, not deployments.

## 1. Separate configuration from per-call state

Start at `Settings` in `config.py`.

- A frozen dataclass groups configuration into a named object and prevents
  accidental reassignment. `repr=False` excludes credential fields from its
  generated string representation; it does not encrypt secrets in memory.
- `from_env()` reads the adjacent `.env` and constructs the object. Loading the
  module alone does nothing. `override=False` preserves process-level overrides.
- `require_call_settings()` checks local prerequisites when a call arrives. It
  does not make a paid request to discover whether the key has model access.
- The public origin is explicit. A reverse proxy/tunnel terminates HTTPS and
  forwards traffic to localhost; its public URL differs from the local URL.
- `media_url` changes `https://` to `wss://` and adds `/media-stream`. HTTP sets
  up the call; WSS carries the continuing conversation.

`LIVE_MODEL` is for audio. `DELEGATED_MODEL` is for reasoning/tool selection.
The provided defaults preserve the linked Twilio example. Account access must
be checked during a deliberately authorized live test.

Checkpoint: explain why a process can answer `GET /` without opening any AI session.

## 2. Explain instructions, tool descriptions, and executable code

Read `demo_tools.py` next.

| Name | Role |
| --- | --- |
| `OPENING` | The greeting we request after startup is acknowledged. |
| `VOICE_PROMPT` | Spoken style, when to delegate, and fictional-data labeling. |
| `BACKEND_PROMPT` | How the reasoning model should use its available tools. |
| `HEADLINE_TOOL` | A JSON Schema describing the local function's arguments. |
| `run_tool()` | The application code that validates and executes that function. |

The schema is a contract, not executable Python. A function call supplies a name
and JSON arguments; the dispatcher checks both. `additionalProperties: false`
rejects unadvertised fields at the schema boundary, and local checks validate the
actual received data too. Bad input returns a structured error to the backend.

The headline lookup deliberately uses fixed local data and labels its result
`fictional: true`. A deterministic selection avoids a changing result while
teaching. `await asyncio.sleep(2)` represents waiting on I/O without blocking the
event loop. It must not be replaced by `time.sleep(2)` inside an async reader.

`web_search` is hosted by OpenAI. Our application advertises it but does not
implement a Python function for it. Only application function calls need our
local dispatcher.

Checkpoint: explain why the schema alone cannot query a calendar or create a booking.

## 3. Follow the incoming HTTP webhook

Read `create_app()` and `incoming_call()` in `app.py`.

1. The app factory receives settings and a connector. The default connector is
   real; tests inject a fake one. This is dependency injection with two arguments,
   rather than a separate framework or mock server.
2. Twilio sends a form-encoded POST to `/incoming-call` when someone dials the number.
3. `request.form()` preserves the received fields. The Twilio SDK checks their
   signature against the configured public URL and the account Auth Token.
4. `VoiceResponse` and `Connect` build XML, escaping attribute values correctly.
5. The response tells Twilio to open a bidirectional stream to the WSS endpoint.

Returning XML doesn't transport the call audio. It directs Twilio to establish
the next connection. The POST returns while the WebSocket remains open.

Checkpoint: point to the exact instruction that makes Twilio open the media stream.

## 4. Establish one isolated bridge per caller

Read `media_stream()` in `app.py`, then `CallBridge.__init__()` and
`wait_for_twilio_start()` in `bridge.py`.

- The WebSocket handshake is authenticated before `accept()` and before any
  OpenAI session is opened. The accepted signature URLs all use the explicitly
  configured origin; request Host/forwarding headers do not select the target.
- The route constructs a new `CallBridge`. Caller identifiers, ready/closed
  events, locks, and tool tasks therefore belong to this caller only.
- Twilio's `connected` event describes the connection. Its `start` event supplies
  the call SID, stream SID, and media format. A timeout bounds missing metadata.
- Call SID identifies the telephone call. Stream SID identifies this audio
  stream and must appear on outgoing Twilio media messages. OpenAI's session ID
  later identifies the voice session on the other side of the bridge.
- The format must be mono G.711 μ-law at 8 kHz. This implementation rejects a
  mismatch rather than silently feeding incorrectly encoded bytes to a model.

Only after valid stream metadata does `connect_live()` open the second socket.
Its Authorization header stays on the server. Its User-Agent identifies the app.

Checkpoint: draw the two persistent connections and label their separate IDs.

## 5. Start Live once, then wait for acknowledgment

Read `session_start()` in `demo_tools.py`, and the beginning of `CallBridge.run()`.

| Session field | Meaning |
| --- | --- |
| `type: session.start` | First command on the new Live connection. |
| `session.model` | The voice model. |
| `session.instructions` | Instructions for spoken interaction. |
| `audio.format` | One shared input/output codec and sample rate. |
| `audio.output.voice` | The synthesized voice. |
| `delegation.type: responses` | OpenAI manages the reasoning backend connection/context. |
| `delegation.responses.model` | The reasoning model, separate from the voice model. |
| `delegation.responses.instructions` | Backend/tool workflow instructions. |
| `delegation.responses.tools` | The hosted search and/or our function definition. |
| `parallel_tool_calls: false` | Keeps the demo's function/result loop sequential. |

A socket being open doesn't mean the model session is ready. The bridge waits
for `session.started` before forwarding caller audio or sending the greeting.
The greeting uses `session.instructions.append` and `session.commentary.append`;
`delegation_id: null` makes these application updates outside a delegated task.
`response.create` has a different purpose: starting/continuing backend work.

Checkpoint: explain why sending the greeting immediately when TCP connects is a race.

## 6. Trace audio through both readers

Read `read_twilio()` and `read_openai()`.

```text
Twilio → application:
  {event: media, media: {payload: BASE64_AUDIO}}

Application → OpenAI:
  {type: session.input_audio.append, audio: BASE64_AUDIO}

OpenAI → application:
  {type: session.output_audio.delta, delta: BASE64_AUDIO}

Application → Twilio:
  {event: media, streamSid: STREAM_ID, media: {payload: BASE64_AUDIO}}
```

The payload is unchanged because the codecs match. Base64 is a text encoding of
the raw codec bytes, not a speech-recognition result and not an audio file.
The application changes the envelopes, not the audio content.

`asyncio.create_task()` starts each reader. Awaiting network input gives the event
loop a chance to run the other reader. `asyncio.wait(... FIRST_COMPLETED)` lets
either side end the call. `send_lock` serializes writes sharing the OpenAI socket.

GPT-Live manages continuous listening/speaking. This is its Live protocol; do not
paste in Realtime VAD, response cancellation, or response-audio-done assumptions.
Local tests prove message routing, not speech quality or interruption latency.

Checkpoint: follow one fictional base64 string through all four message shapes.

## 7. Follow a complete function round trip

Read `schedule_tool()` and `execute_tool()`.

1. OpenAI sends a `response.event` envelope. The nested event is
   `response.output_item.done`, containing a completed `function_call` item.
2. `call_id` links the eventual result to the requested function. A per-call set
   suppresses repeat delivery of the same completed item.
3. The dispatcher schedules `execute_tool()` as its own task. The audio reader
   keeps receiving audio while the function waits.
4. The function result is serialized into the **string** `item.output` inside a
   `response.item.create` message with `item.type: function_call_output`.
5. The application sends `response.create` to continue the reasoning backend.
6. The voice model communicates the result through subsequent audio events.

An arguments-done fragment is not our dispatch trigger. A hosted web-search item
is not a local function call. A completed backend response is not proof that the
caller heard the result; playback is a separate responsibility.

This demo disables parallel function calls. Supporting batches later requires
collecting all pending outputs before continuing, plus response/delegation-level
bookkeeping. The current per-call duplicate set is not durable idempotency for
client-system writes after a process restart.

Checkpoint: locate the two sends needed after a local function completes.

## 8. Finish the call without leaving work behind

Read the `finally` block in `CallBridge.run()`.

- A Twilio `stop` event or disconnect ends input. A provider closure ends output.
- `closing` prevents starting more audio/tool work during finalization.
- The input reader and local fake tool tasks are canceled and awaited.
- If Live was ready and its reader is still running, the bridge sends
  `session.close` and keeps that reader alive until `session.closed` or timeout.
- `session.closed` confirms final voice usage. A plain socket closure does not.
  Cumulative duration snapshots replace the previous value; they are not summed.
- Finally, both socket contexts/route cleanup release their connections.

We cancel a harmless local sleep at hangup. A future booking/payment request
needs its own policy: canceling a Python task does not reverse an external action.
Audio interruption and cancellation of business work are separate decisions.

Checkpoint: distinguish `ready`, `closing`, and `closed` without using them interchangeably.

## Teaching and client-demo progression

Record the foundation lesson in this order: architecture, HTTP/TwiML, Live
handshake, audio envelopes, slow tool, and cleanup. Show the offline concurrency
test before the real phone test so viewers can see what each form of evidence proves.

For the next demo, choose one concrete client outcome and build incrementally:

1. Replace fictional data with a read-only business lookup, preserving the tool boundary.
2. Validate the lookup against realistic requests and corrections during a call.
3. Add one state-changing action with explicit confirmation and durable idempotency.
4. Add human handoff, failure recovery, measured latency, and operating limits.
5. Present the actual tested outcome and limitations in an Upwork portfolio demo
   or YouTube walkthrough; avoid claiming capabilities the demo doesn't implement.
