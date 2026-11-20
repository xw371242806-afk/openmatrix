"""Provider-specific routing headers without storing model credentials."""
from urllib.parse import urlsplit
from uuid import uuid4


def model_request_headers(base_url: str, session_id: str | None = None) -> dict[str, str]:
    """Identify OpenMatrix and keep one Go routing session per client/run.

    OpenCode Go requires an honest client User-Agent and stable conversation ID.
    Other providers retain their SDK defaults.
    """
    try:
        parsed = urlsplit(base_url)
        is_go = (
            parsed.scheme.lower() == "https"
            and parsed.hostname == "opencode.ai"
            and parsed.port in {None, 443}
            and parsed.path.rstrip("/") == "/zen/go/v1"
            and not parsed.username and not parsed.password
        )
    except ValueError:
        return {}
    if not is_go:
        return {}
    return {
        "User-Agent": "OpenMatrix/1.0",
        "x-opencode-session": session_id or "openmatrix-" + uuid4().hex,
    }
