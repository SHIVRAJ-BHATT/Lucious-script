# ba_meta require api 9
"""Small bounded history of allowed public chat for the protected /lm command."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

_LOG = logging.getLogger("Lucifer.ChatHistory")
_LOCK = threading.RLock()
_HISTORY: deque[dict[str, Any]] = deque(maxlen=100)


def _player_name(client_id: int) -> str:
    """Resolve a display name from the current roster, falling back to client ID."""
    try:
        import bascenev1 as bs
        for entry in bs.get_game_roster():
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("client_id", -1)) != client_id:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            value = entry.get("display_string", entry.get("name"))
            if isinstance(value, (list, tuple)):
                value = " ".join(str(part) for part in value if part is not None)
            if isinstance(value, str) and value.strip():
                return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:48]
    except Exception:
        _LOG.debug("Could not resolve sender name for chat-history entry.", exc_info=True)
    return f"Client {client_id}"


def record_message(client_id: int, message: str) -> None:
    """Record an allowed, non-command chat message after moderation."""
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 0:
        return
    if not isinstance(message, str):
        message = str(message)
    text = message.strip()
    if not text or text.startswith("/"):
        return
    entry = {
        "client_id": client_id,
        "name": _player_name(client_id),
        "message": " ".join(text.replace("\r", " ").replace("\n", " ").split())[:300],
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    with _LOCK:
        _HISTORY.append(entry)


def get_recent_messages(limit: int = 15) -> list[dict[str, Any]]:
    """Return a copy of the most recent messages, oldest-to-newest."""
    try:
        count = max(1, min(int(limit), 100))
    except (TypeError, ValueError, OverflowError):
        count = 15
    with _LOCK:
        return [dict(entry) for entry in list(_HISTORY)[-count:]]
