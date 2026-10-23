# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Owner/Editor: Lucifer
"""Public commands for online player listings and current network latency."""

from __future__ import annotations

import logging
import math
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers

_LOG = logging.getLogger("Lucifer.NormalCommands")
_ERROR_COLOR = (1.0, 0.2, 0.2)
_INFO_COLOR = (0.45, 0.82, 1.0)
_GOOD_COLOR = (0.25, 1.0, 0.45)

Commands = ["list", "ping"]
CommandAliases = ["l"]
_ALIAS_TO_COMMAND = {"l": "list"}


def _player_name(session_player: Any) -> str:
    """Return a safe readable session-player name across API-compatible forms."""
    getter = getattr(session_player, "getname", None)
    if callable(getter):
        try:
            name = getter(full=False, icon=False)
        except TypeError:
            # Support older/session wrapper signatures without treating a
            # signature mismatch as a real player-name failure.
            try:
                name = getter(icon=False)
            except TypeError:
                try:
                    name = getter()
                except Exception:
                    return "Unknown"
            except Exception:
                return "Unknown"
        except Exception:
            return "Unknown"
        text = str(name).replace("\n", " ").replace("\r", " ").replace("|", "¦").strip()
        return text or "Unknown"
    return "Unknown"


def _get_session_players() -> list[Any] | None:
    """Return a snapshot of current session players, or None on API failure."""
    try:
        session = bs.get_foreground_host_session()
    except Exception:
        _LOG.exception("Could not get the foreground host session.")
        return None
    if session is None:
        return None
    try:
        return list(getattr(session, "sessionplayers", ()))
    except Exception:
        _LOG.exception("Could not enumerate session players.")
        return None


def _client_id(session_player: Any) -> int | None:
    """Read and validate a session player's network client ID."""
    try:
        value = int(session_player.inputdevice.client_id)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _get_client_ping(client_id: int) -> float | None:
    """Return the engine-reported round-trip latency in milliseconds."""
    getter = getattr(bs, "get_client_ping", None)
    if not callable(getter):
        _LOG.error("This Ballistica build does not expose bascenev1.get_client_ping().")
        return None
    try:
        ping_ms = float(getter(client_id))
    except Exception:
        _LOG.exception("Could not read ping for client %s.", client_id)
        return None

    # Ballistica returns -1.0 for invalid/disconnected client IDs.
    if not math.isfinite(ping_ms) or ping_ms < 0.0:
        return None
    return ping_ms


def list_players(clientid: int) -> bool:
    """Send the current session-player list privately to the requesting client."""
    players = _get_session_players()
    if players is None:
        handlers.send(
            "Could not read the online player list or there is no active session.",
            clientid,
            color=_ERROR_COLOR,
        )
        return False

    lines = [
        f"ONLINE PLAYERS: {len(players)}",
        "Name | Client ID | Player ID",
        "--------------------------------",
    ]
    for player_index, player in enumerate(players):
        player_client_id = _client_id(player)
        lines.append(
            f"{_player_name(player)} | "
            f"{player_client_id if player_client_id is not None else 'N/A'} | "
            f"{player_index}"
        )

    if not players:
        lines.append("No players are currently in the session.")

    handlers.send_chat("\n".join(lines), clientid)
    return True


def ping_player(clientid: int) -> bool:
    """Privately show the command sender their current network latency."""
    ping_ms = _get_client_ping(clientid)
    if ping_ms is None:
        handlers.send(
            "Your current ping is unavailable. Try again while connected.",
            clientid,
            color=_ERROR_COLOR,
        )
        return False

    handlers.send(
        f"\ue043 YOUR PING: {ping_ms:.0f} ms \ue043",
        clientid,
        color=_GOOD_COLOR,
    )
    return True


def ping_all(clientid: int) -> bool:
    """Privately list current latency for each connected session player."""
    players = _get_session_players()
    if players is None:
        handlers.send(
            "Could not read player latency because there is no active session.",
            clientid,
            color=_ERROR_COLOR,
        )
        return False

    lines = [f"PLAYER PING ({len(players)} session player(s))", "Name | Client ID | Ping"]
    lines.append("--------------------------------")
    visible_players = 0
    for player in players:
        target_id = _client_id(player)
        # Local/headless players do not have a remote-client latency to query.
        if target_id is None:
            continue
        visible_players += 1
        ping_ms = _get_client_ping(target_id)
        ping_text = f"{ping_ms:.0f} ms" if ping_ms is not None else "Unavailable"
        lines.append(f"{_player_name(player)} | {target_id} | {ping_text}")

    if visible_players == 0:
        lines.append("No connected remote players are currently in the session.")

    handlers.send_chat("\n".join(lines), clientid)
    return True


def ExcelCommand(
    command: str,
    arguments: list[str],
    clientid: int,
    accountid: str | None,
) -> bool:
    """Dispatch public commands; return True only when a command completed."""
    del accountid  # Reserved for future public commands.
    normalized = str(command).strip().casefold()
    normalized = _ALIAS_TO_COMMAND.get(normalized, normalized)

    if normalized == "list":
        if arguments:
            handlers.send("Usage: /list", clientid, color=_INFO_COLOR)
            return False
        return list_players(clientid)

    if normalized == "ping":
        if not arguments:
            return ping_player(clientid)
        if len(arguments) == 1 and str(arguments[0]).casefold() == "all":
            return ping_all(clientid)
        handlers.send("Usage: /ping or /ping all", clientid, color=_INFO_COLOR)
        return False

    return False
