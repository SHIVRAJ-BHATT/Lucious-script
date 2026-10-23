# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""In-memory spam warnings and temporary chat mute for BombSquad API 9.

Spam never kicks a player. Each spam event gives a warning; reaching
MAX_SPAM_WARNINGS applies a temporary chat mute. Profanity escalation is
tracked independently by record_violation().
"""

from __future__ import annotations

from collections import deque
import math
import threading
import time
from typing import Any

RATE_WINDOW_SECONDS = 8.0
RATE_LIMIT = 4
REPEAT_WINDOW_SECONDS = 20.0
REPEAT_LIMIT = 4
REPEAT_MIN_LENGTH = 6

MAX_SPAM_WARNINGS = 3
# Backward-compatible constant name used by earlier patches.
MAX_STRIKES = MAX_SPAM_WARNINGS
STRIKE_DECAY_SECONDS = 120.0
# Escalating mute durations: first mute 10s, second 30s, third and later 60s.
MUTE_DURATIONS_SECONDS = (10.0, 30.0, 60.0)
# Backward-compatible default for callers that still use the old constant.
MUTE_DURATION_SECONDS = MUTE_DURATIONS_SECONDS[0]
MUTED_NOTICE_INTERVAL_SECONDS = 10.0

MAX_ABUSE_WARNINGS = 3

_LOCK = threading.RLock()
_USERS: dict[str, dict[str, Any]] = {}


def _key(identity: str | None, client_id: int | None = None) -> str:
    """Use a valid-looking V2 ID when available; otherwise the live client ID."""
    if isinstance(identity, str) and identity.startswith("a-") and len(identity) > 2:
        return identity
    if client_id is not None:
        try:
            return f"client:{int(client_id)}"
        except (TypeError, ValueError):
            pass
    return "unknown-client"


def _get_state(key: str) -> dict[str, Any]:
    state = _USERS.get(key)
    if state is None:
        state = {
            "message_times": deque(),
            "recent_messages": deque(),
            "spam_strikes": 0,
            "last_spam_strike": 0.0,
            "muted_until": 0.0,
            "last_muted_notice": 0.0,
            "mute_count": 0,
            "profanity_warnings": 0,
        }
        _USERS[key] = state
    return state


def _clear_spam_windows(state: dict[str, Any]) -> None:
    state["message_times"].clear()
    state["recent_messages"].clear()


def get_mute_status(
    identity: str | None,
    client_id: int | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Return current mute status and whether a reminder should be displayed.

    Muted messages should be suppressed by the caller. A reminder is returned
    at most once per MUTED_NOTICE_INTERVAL_SECONDS.
    """
    timestamp = time.time() if now is None else float(now)
    with _LOCK:
        state = _get_state(_key(identity, client_id))
        muted_until = float(state.get("muted_until", 0.0))

        if muted_until > timestamp:
            remaining = max(0.0, muted_until - timestamp)
            last_notice = float(state.get("last_muted_notice", 0.0))
            notify = last_notice <= 0.0 or timestamp - last_notice >= MUTED_NOTICE_INTERVAL_SECONDS
            if notify:
                state["last_muted_notice"] = timestamp
            return {
                "muted": True,
                "remaining_seconds": int(math.ceil(remaining)),
                "notify": notify,
            }

        # On expiry, reset the spam counter and windows so the player can
        # participate again without immediately being muted on their next chat.
        if muted_until > 0.0 and muted_until <= timestamp:
            state["muted_until"] = 0.0
            state["last_muted_notice"] = 0.0
            state["spam_strikes"] = 0
            state["last_spam_strike"] = 0.0
            _clear_spam_windows(state)

        return {"muted": False, "remaining_seconds": 0, "notify": False}


def _add_spam_strike(state: dict[str, Any], now: float) -> int:
    """Count one spam event, with strike decay after inactivity."""
    if now - float(state.get("last_spam_strike", 0.0)) > STRIKE_DECAY_SECONDS:
        state["spam_strikes"] = 0
    state["spam_strikes"] = int(state.get("spam_strikes", 0)) + 1
    state["last_spam_strike"] = now
    return state["spam_strikes"]


def check_message(
    identity: str | None,
    message: str,
    client_id: int | None = None,
    *,
    exempt_from_strikes: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Detect message flooding/repetition and warn or mute, never kick.

    Four messages in eight seconds or four identical messages (each at least
    REPEAT_MIN_LENGTH characters) in twenty seconds trigger a spam event.
    Non-exempt players receive three spam warnings. The next spam event applies
    a progressive chat mute: 10 seconds first, 30 seconds second, then 60 seconds
    for the third and later mutes. The trigger message is suppressed. Owner exemption
    skips spam warning/mute escalation but not message suppression.
    """
    if not isinstance(message, str):
        message = str(message)
    timestamp = time.time() if now is None else float(now)
    user_key = _key(identity, client_id)
    normalized = " ".join(message.casefold().split())

    with _LOCK:
        state = _get_state(user_key)
        muted_until = float(state.get("muted_until", 0.0))
        if muted_until > timestamp:
            return {
                "spam": True,
                "reason": "muted",
                "kick": False,
                "muted": True,
                "mute_seconds": int(math.ceil(muted_until - timestamp)),
                "strikes": int(state.get("spam_strikes", 0)),
            }

        message_times = state["message_times"]
        recent_messages = state["recent_messages"]

        while message_times and timestamp - message_times[0] > RATE_WINDOW_SECONDS:
            message_times.popleft()
        while recent_messages and timestamp - recent_messages[0][0] > REPEAT_WINDOW_SECONDS:
            recent_messages.popleft()

        message_times.append(timestamp)
        recent_messages.append((timestamp, normalized))

        repeated_count = (
            sum(1 for _sent_at, old_message in recent_messages if old_message == normalized)
            if len(normalized) >= REPEAT_MIN_LENGTH
            else 0
        )

        reason = ""
        if repeated_count >= REPEAT_LIMIT:
            reason = "repeated-message"
            # Clear both windows so the same burst is counted as one violation.
            _clear_spam_windows(state)
        elif len(message_times) >= RATE_LIMIT:
            reason = "message-flood"
            message_times.clear()

        if not reason:
            return {
                "spam": False,
                "reason": "",
                "kick": False,
                "muted": False,
                "strikes": int(state.get("spam_strikes", 0)),
            }

        if exempt_from_strikes:
            # Preserve the owner's configured spam exemption; still suppress
            # the event-triggering message, but do not warn or mute.
            return {
                "spam": True,
                "reason": reason,
                "kick": False,
                "muted": False,
                "strikes": int(state.get("spam_strikes", 0)),
            }

        strikes = _add_spam_strike(state, timestamp)
        # Give all three configured warnings first. Mute only on the next
        # spam event after the final warning (warning limit exceeded).
        should_mute = strikes > MAX_SPAM_WARNINGS
        mute_seconds = 0
        mute_level = 0
        if should_mute:
            previous_mutes = int(state.get("mute_count", 0))
            mute_index = min(previous_mutes, len(MUTE_DURATIONS_SECONDS) - 1)
            mute_seconds = int(MUTE_DURATIONS_SECONDS[mute_index])
            mute_level = previous_mutes + 1
            state["mute_count"] = mute_level
            state["muted_until"] = timestamp + mute_seconds
            state["last_muted_notice"] = timestamp
            # Reset detector windows while muted. Mute escalation is intentionally
            # retained after expiry so subsequent mutes progress to 30s and 60s.
            _clear_spam_windows(state)

        return {
            "spam": True,
            "reason": reason,
            "kick": False,  # Spam must never disconnect/kick a player.
            "muted": should_mute,
            "mute_seconds": mute_seconds,
            "mute_level": mute_level,
            "strikes": strikes,
        }


def record_violation(
    identity: str | None,
    client_id: int | None = None,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Record a profanity violation (separate from spam warnings).

    Warnings 1/3, 2/3, and 3/3 are given. The next abusive message returns
    kick=True, preserving the separately requested profanity policy.
    """
    del now  # Kept for call-site/test compatibility; abuse count is per process.
    with _LOCK:
        state = _get_state(_key(identity, client_id))
        strikes = int(state.get("profanity_warnings", 0)) + 1
        state["profanity_warnings"] = strikes
        return {"strikes": strikes, "kick": strikes > MAX_ABUSE_WARNINGS}


def isspam(
    message: str,
    account_id: str | None = None,
    client_id: int | None = None,
) -> bool:
    """Compatibility boolean helper; call only once per incoming message."""
    return bool(check_message(account_id, message, client_id)["spam"])


def reset_player(identity: str | None = None, client_id: int | None = None) -> None:
    """Forget one player's transient moderation state, if needed."""
    with _LOCK:
        _USERS.pop(_key(identity, client_id), None)


def clear_mute(identity: str | None, client_id: int | None = None) -> bool:
    """Clear an active transient anti-spam mute without erasing prior strikes.

    Staff `/unmute` uses this alongside the persistent muted.json record so a
    player is not left under a separate short spam mute after a moderator acts.
    """
    with _LOCK:
        state = _USERS.get(_key(identity, client_id))
        if state is None or float(state.get("muted_until", 0.0)) <= time.time():
            return False
        state["muted_until"] = 0.0
        state["last_muted_notice"] = 0.0
        _clear_spam_windows(state)
        return True

