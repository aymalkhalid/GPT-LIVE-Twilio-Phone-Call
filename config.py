"""Step 1: read configuration without exposing or changing local secrets.

The voice model speaks; the delegated model reasons and selects tools. Keeping
their names separate makes that architectural distinction visible to learners.
Importing this module neither loads credentials nor opens network connections.
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Immutable configuration, shared by calls; each call gets its own bridge.

    ``repr=False`` prevents accidental printing of credential fields in logs.
    ``public_base_url`` is the actual public HTTPS origin Twilio requests. It
    avoids trusting Host/forwarding headers and keeps signature checks stable
    behind a localhost tunnel. An empty origin permits health checks only.
    """

    openai_api_key: str = field(default="", repr=False)
    twilio_auth_token: str = field(default="", repr=False)
    public_base_url: str = ""
    live_model: str = "gpt-live-1"
    delegated_model: str = "gpt-5.6-terra"
    voice: str = "marin"
    enable_web_search: bool = True
    startup_timeout_seconds: float = 15
    close_timeout_seconds: float = 5
    tool_delay_seconds: float = 2

    @classmethod
    def from_env(cls) -> "Settings":
        """Load the adjacent .env, with existing process variables taking priority.

        Anchoring the path to this file makes startup independent of the shell's
        current folder. dotenv reads the file; it never rewrites its contents.
        Key syntax isn't guessed: authorization is established by OpenAI only
        when a real call opens a session, not by a local prefix/length check.
        """
        load_dotenv(Path(__file__).with_name(".env"), override=False)
        search = os.getenv("ENABLE_WEB_SEARCH", "true").strip().lower()
        if search not in {"true", "false"}:
            raise ValueError("ENABLE_WEB_SEARCH must be true or false.")
        settings = cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            live_model=os.getenv("LIVE_MODEL", "gpt-live-1"),
            delegated_model=os.getenv("DELEGATED_MODEL", "gpt-5.6-terra"),
            voice=os.getenv("VOICE", "marin"),
            enable_web_search=search == "true",
            startup_timeout_seconds=float(os.getenv("STARTUP_TIMEOUT_SECONDS", "15")),
            close_timeout_seconds=float(os.getenv("CLOSE_TIMEOUT_SECONDS", "5")),
            tool_delay_seconds=float(os.getenv("TOOL_DELAY_SECONDS", "2")),
        )
        if min(settings.startup_timeout_seconds, settings.close_timeout_seconds) <= 0:
            raise ValueError("Startup and close timeouts must be positive.")
        if settings.tool_delay_seconds < 0:
            raise ValueError("TOOL_DELAY_SECONDS cannot be negative.")
        return settings

    def require_call_settings(self) -> None:
        """Fail before answering a call if credentials or the public URL are absent.

        This is a local configuration check, not a provider access test. Public
        URL paths, query strings, and embedded passwords are excluded so every
        webhook and WebSocket has one unambiguous signed URL.
        """
        if not self.openai_api_key or not self.twilio_auth_token:
            raise ValueError("Set OPENAI_API_KEY and TWILIO_AUTH_TOKEN in .env.")
        url = urlsplit(self.public_base_url)
        if (url.scheme != "https" or not url.hostname or url.path
                or url.query or url.fragment or url.username or url.password):
            raise ValueError("Set PUBLIC_BASE_URL to an HTTPS origin with no path.")

    @property
    def media_url(self) -> str:
        """Return the public WSS endpoint that Twilio should connect to."""
        return self.public_base_url.replace("https://", "wss://", 1) + "/media-stream"
