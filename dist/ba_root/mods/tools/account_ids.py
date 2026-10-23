"""Reusable V2 account-ID helpers for BombSquad API 9 mods.

Import these functions from any module under the configured mods directory:
    from account_ids import get_v2_account_id, is_v2_account_id

This module deliberately never falls back to V1 account IDs.
"""

from __future__ import annotations

from typing import Any


def is_v2_account_id(value: Any) -> bool:
    """Return True only for a non-empty, whitespace-free ``a-`` account ID."""
    return (
        isinstance(value, str)
        and value.startswith("a-")
        and len(value) > 2
        and not any(char.isspace() for char in value)
    )


def get_v2_account_id(player_or_session_player: Any) -> str | None:
    """Retrieve a V2 account ID from a Player or SessionPlayer.

    Calls API 9's ``get_account_id()`` only. If it returns a V1 ID, no ID,
    or an invalid value, this function returns None. It never calls the old
    ``get_v1_account_id()`` API.
    """
    if player_or_session_player is None:
        return None

    candidates: list[Any] = []
    try:
        session_player = getattr(player_or_session_player, "sessionplayer", None)
        if session_player is not None:
            candidates.append(session_player)
    except Exception:
        pass
    candidates.append(player_or_session_player)

    seen: set[int] = set()
    for candidate in candidates:
        if id(candidate) in seen:
            continue
        seen.add(id(candidate))
        try:
            getter = getattr(candidate, "get_account_id", None)
            if not callable(getter):
                continue
            account_id = getter()
        except Exception:
            continue
        if is_v2_account_id(account_id):
            return account_id

    return None
