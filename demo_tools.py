"""Step 2: prompts, a function schema, and one intentionally fictional tool.

The schema advertises a capability to the reasoning model. ``run_tool`` actually
executes it in our application. Hosted web search is different: OpenAI executes
that tool, so it never goes through this local dispatcher.
"""

import asyncio
import json

from config import Settings


OPENING = (
    "Hi! I'm an AI voice assistant in a Twilio and OpenAI teaching demo. "
    "How can I help?"
)
VOICE_PROMPT = (
    "You are a friendly AI voice assistant in a teaching demo. Be concise. "
    "Delegate requests needing facts, search, or tools to the backend. "
    "The headline function returns fictional demo data; always say it is fictional. "
    "Never present a demo headline as real news or claim a real business action occurred."
)
BACKEND_PROMPT = (
    "Use get_top_headline only for fictional demonstration headlines. "
    "For real facts and current news, use web_search when available; otherwise "
    "explain that current information cannot be verified. Return concise results. "
    "Clearly label fictional data. No booking, messaging, or other business actions exist."
)

# JSON Schema describes exactly what our function accepts. `strict` and the
# matching runtime validation help make malformed tool calls easy to explain.
HEADLINE_TOOL = {
    "type": "function",
    "name": "get_top_headline",
    "description": "Return a FICTIONAL headline for a city to demonstrate a tool call.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    },
}


def session_start(settings: Settings) -> dict:
    """Build the first Live message; send it once the Twilio stream is identified.

    G.711 PCMU at 8 kHz matches Twilio's mono audio. We can preserve base64 audio
    payloads while changing their JSON envelope. There is no WAV file, resampler,
    STT service, TTS service, or separate Responses connection in this bridge.
    Parallel function calls are disabled to keep the teaching tool loop small.
    """
    tools = [HEADLINE_TOOL]
    if settings.enable_web_search:
        tools = [{"type": "web_search"}, *tools]
    return {
        "type": "session.start",
        "session": {
            "model": settings.live_model,
            "instructions": VOICE_PROMPT,
            "audio": {"format": {"type": "audio/pcmu", "rate": 8000},
                      "output": {"voice": settings.voice}},
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": settings.delegated_model,
                    "instructions": BACKEND_PROMPT,
                    "tools": tools,
                    "parallel_tool_calls": False,
                },
            },
        },
    }


async def run_tool(name: str, arguments: str, delay_seconds: float) -> dict:
    """Validate a completed call and return JSON-compatible data, never real news.

    The asynchronous sleep models a slow database/API without blocking the audio
    reader. A stable choice makes the lesson and local tests reproducible. Errors
    are returned as data so the reasoning model can explain them to the caller.
    """
    if name != "get_top_headline":
        return {"error": "unknown_tool"}
    try:
        args = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {"error": "invalid_json"}
    if (not isinstance(args, dict) or set(args) != {"city"}
            or not isinstance(args["city"], str) or not 1 <= len(args["city"].strip()) <= 120):
        return {"error": "city must be a nonempty string of at most 120 characters"}
    city = args["city"].strip()
    await asyncio.sleep(delay_seconds)
    headlines = (
        "City replaces every traffic light with a very persuasive pigeon.",
        "Local library opens a silent disco; nobody can tell when it starts.",
        "Mayor declares a public holiday after finding the missing office stapler.",
    )
    return {"city": city, "headline": headlines[sum(map(ord, city)) % len(headlines)],
            "fictional": True, "source": "local teaching fixture; not real news"}
