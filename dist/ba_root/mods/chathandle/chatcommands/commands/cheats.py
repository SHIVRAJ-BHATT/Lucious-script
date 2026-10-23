# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Owner/Editor: Lucifer
"""Role-protected player cheat/powerup commands for BombSquad API 9."""

from __future__ import annotations

import logging
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers

_LOG = logging.getLogger("Lucifer.CheatCommands")

# BombSquad glyphs used for consistent, decorative command feedback.
_GLYPH_OK = "\ue048"
_GLYPH_DETAIL = "\ue04c"
_GLYPH_ERROR = "\ue062"
_ERROR_COLOR = (1.0, 0.18, 0.18)
_ENABLED_COLOR = (0.15, 1.0, 0.25)
_DISABLED_COLOR = (1.0, 0.2, 0.2)

def _send_error(message: str, clientid: int) -> None:
    """Send a consistently framed error to the command sender."""
    handlers.send(
        f"{_GLYPH_ERROR}  {message}  {_GLYPH_ERROR}",
        clientid,
        color=_ERROR_COLOR,
    )


def _send_success(message: str, clientid: int) -> None:
    """Send a decorative private success message."""
    handlers.send_chat(f"{_GLYPH_OK}  {message}  {_GLYPH_OK}", clientid)


Commands = [
    "kill", "heal", "curse", "sleep", "superpunch",
    "gloves", "shield", "freeze", "unfreeze", "godmode",
]
_VALID_COMMANDS = frozenset(Commands)
CommandAliases = [
    "die", "heath", "cur", "sp", "punch",
    "protect", "ice", "thaw", "gm",
]
_ALIAS_TO_COMMAND = {
    "die": "kill",
    "heath": "heal",
    "cur": "curse",
    "sp": "superpunch",
    "punch": "gloves",
    "protect": "shield",
    "ice": "freeze",
    "thaw": "unfreeze",
    "gm": "godmode",
}


def canonical_command(command: str) -> str:
    """Return the primary command name for permission checks and dispatch."""
    name = str(command).strip().casefold().lstrip("/")
    return _ALIAS_TO_COMMAND.get(name, name)


def _get_activity() -> Any | None:
    try:
        return bs.get_foreground_host_activity()
    except Exception:
        _LOG.exception("Could not obtain the foreground activity.")
        return None


def _client_id_for_player(player: Any) -> int | None:
    """Resolve an activity Player to its connected session player's client ID."""
    try:
        session_player = getattr(player, "sessionplayer", None)
        session_device = getattr(session_player, "inputdevice", None)
    except Exception:
        session_device = None
    try:
        player_device = getattr(player, "inputdevice", None)
    except Exception:
        player_device = None

    for device in (session_device, player_device):
        if device is None:
            continue
        try:
            raw_value = getattr(device, "client_id", None)
            if isinstance(raw_value, bool):
                continue
            value = int(raw_value)
            if value >= 0:
                return value
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        except Exception:
            # Device access can race with a disconnect or activity transition.
            continue
    return None


def _player_name(player: Any) -> str:
    """Return a single-line display name for status broadcasts."""
    getter = getattr(player, "getname", None)
    if callable(getter):
        try:
            value = getter(full=False, icon=False)
        except TypeError:
            try:
                value = getter()
            except Exception:
                value = None
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            # Names are presentation only; keep a name from injecting extra lines.
            return " ".join(value.replace("\\r", " ").replace("\\n", " ").split())[:64]
    return "Unknown"


def _safe_actor(player: Any) -> Any | None:
    """Return an actor only if it still owns a live scene node.

    Character actors can be absent during respawn or disappear between command
    receipt and execution. Check references before dereferencing them.
    """
    try:
        actor = getattr(player, "actor", None)
        if actor is None:
            return None
        node = getattr(actor, "node", None)
        if node is None:
            return None
        exists = getattr(node, "exists", None)
        if not callable(exists) or not exists():
            return None
        return actor
    except Exception:
        # Scene-node access may race with character destruction/respawn.
        return None


def _resolve_targets(
    arguments: list[str], clientid: int
) -> tuple[Any | None, list[Any], str | None]:
    """Resolve an optional zero-based activity Player ID, 'all', or self."""
    activity = _get_activity()
    if activity is None:
        return None, [], "There is no active game to target."

    if len(arguments) > 1:
        return activity, [], "Usage: /<command> [playerID|all]"
    raw = arguments[0].strip() if arguments else ""

    # Snapshot once so "all", self-targeting, and numeric indexes use the same
    # player collection and do not repeatedly access the activity property.
    try:
        players = tuple(getattr(activity, "players", ()))
    except Exception:
        _LOG.exception("Could not enumerate players for a protected cheat command.")
        return activity, [], "Could not read the active player list right now."

    if not raw:
        try:
            target_client_id = int(clientid)
        except (TypeError, ValueError, OverflowError):
            return activity, [], "Could not identify the command sender."
        for player in players:
            if _client_id_for_player(player) == target_client_id:
                return activity, [player], None
        return activity, [], "Your character is not currently in the active game."

    if raw.casefold() == "all":
        return activity, list(players), None

    try:
        player_id = int(raw)
    except (TypeError, ValueError, OverflowError):
        return activity, [], "Player ID must be a number or 'all'."

    if player_id < 0 or player_id >= len(players):
        return activity, [], "Player ID not found. Use /list to check player IDs."
    return activity, [players[player_id]], None


def _broadcast_toggle(player: Any, feature: str, enabled: bool) -> None:
    """Preserve the old script's visible ON/OFF notice for toggle commands."""
    try:
        glyph = _GLYPH_OK if enabled else _GLYPH_DETAIL
        state = "ENABLED" if enabled else "DISABLED"
        bs.broadcastmessage(
            "{}  {} | {} {}  {}".format(
                glyph, _player_name(player), feature, state, glyph
            ),
            color=_ENABLED_COLOR if enabled else _DISABLED_COLOR,
            transient=True,
        )
    except Exception:
        # This is infrequent and actionable; keep it visible in terminal logs.
        _LOG.warning("Could not broadcast %s state.", feature, exc_info=True)


def _toggle_superpunch(actor: Any, player: Any) -> str:
    saved = getattr(actor, "_lucifer_superpunch_saved", None)
    if saved is None:
        saved = (
            getattr(actor, "_super_punch", False),
            getattr(actor, "_punch_power_scale", 1.2),
            getattr(actor, "_punch_cooldown", 400),
        )
        actor._lucifer_superpunch_saved = saved
        actor._super_punch = True
        actor._punch_power_scale = 15
        actor._punch_cooldown = 0
        _broadcast_toggle(player, "SUPERPUNCH", True)
        return "SUPERPUNCH ON"

    actor._super_punch, actor._punch_power_scale, actor._punch_cooldown = saved
    try:
        del actor._lucifer_superpunch_saved
    except AttributeError:
        pass
    _broadcast_toggle(player, "SUPERPUNCH", False)
    return "SUPERPUNCH OFF"


def _toggle_godmode(actor: Any, player: Any) -> str:
    node = actor.node
    saved = getattr(actor, "_lucifer_godmode_saved", None)
    if saved is None:
        try:
            old_hockey = node.hockey
        except Exception:
            old_hockey = None
        saved = {
            "hitpoints": getattr(actor, "hitpoints", 1000),
            "hitpoints_max": getattr(actor, "hitpoints_max", 1000),
            "punch_power": getattr(actor, "_punch_power_scale", 1.2),
            "punch_cooldown": getattr(actor, "_punch_cooldown", 400),
            "hockey": old_hockey,
        }
        actor._lucifer_godmode_saved = saved
        actor.hitpoints_max = 99999
        actor.hitpoints = 99999
        actor._punch_power_scale = 7
        actor._punch_cooldown = 0
        try:
            node.hockey = True
        except Exception:
            pass
        _broadcast_toggle(player, "GODMODE", True)
        return "GODMODE ON"

    actor.hitpoints_max = saved["hitpoints_max"]
    actor.hitpoints = min(saved["hitpoints"], saved["hitpoints_max"])
    actor._punch_power_scale = saved["punch_power"]
    actor._punch_cooldown = saved["punch_cooldown"]
    if saved["hockey"] is not None:
        try:
            node.hockey = saved["hockey"]
        except Exception:
            pass
    try:
        del actor._lucifer_godmode_saved
    except AttributeError:
        pass
    _broadcast_toggle(player, "GODMODE", False)
    return "GODMODE OFF"


def _apply_one(command: str, player: Any, actor: Any) -> str:
    """Apply one validated command to one live player actor."""
    if command == "kill":
        actor.handlemessage(bs.DieMessage())
        return "KILLED"
    if command == "heal":
        actor.handlemessage(bs.PowerupMessage(poweruptype="health"))
        return "HEALED"
    if command == "curse":
        actor.handlemessage(bs.PowerupMessage(poweruptype="curse"))
        return "CURSED"
    if command == "sleep":
        actor.node.handlemessage("knockout", 8000)
        return "SLEEP"
    if command == "superpunch":
        return _toggle_superpunch(actor, player)
    if command == "gloves":
        actor.handlemessage(bs.PowerupMessage(poweruptype="punch"))
        return "GLOVES"
    if command == "shield":
        actor.handlemessage(bs.PowerupMessage(poweruptype="shield"))
        return "SHIELD"
    if command == "freeze":
        actor.handlemessage(bs.FreezeMessage())
        return "FROZEN"
    if command == "unfreeze":
        actor.handlemessage(bs.ThawMessage())
        return "UNFROZEN"
    if command == "godmode":
        return _toggle_godmode(actor, player)
    raise ValueError("Unsupported cheat command: {}".format(command))


def ExcelCommand(
    command: str,
    arguments: list[str],
    clientid: int,
    accountid: str | None,
) -> bool:
    """Dispatch a protected cheat command; executor handles permissions."""
    del accountid  # Identity and command permissions are validated by the executor.
    normalized = canonical_command(command)
    if normalized not in _VALID_COMMANDS:
        return False

    activity, players, error = _resolve_targets(arguments, clientid)
    if error:
        message = error.replace("<command>", normalized)
        _send_error(message, clientid)
        return False
    if not players:
        _send_error(
            "No valid players were found for /{}.".format(normalized),
            clientid,
        )
        return False

    succeeded = 0
    failures = 0
    try:
        with activity.context:
            for player in players:
                actor = _safe_actor(player)
                if actor is None:
                    failures += 1
                    continue
                try:
                    _apply_one(normalized, player, actor)
                    succeeded += 1
                except Exception:
                    failures += 1
                    _LOG.exception(
                        "Command /%s failed for activity player %s.",
                        normalized,
                        _player_name(player),
                    )
    except Exception:
        _LOG.exception("Could not apply /%s in the foreground activity.", normalized)
        _send_error("Could not apply that command in the current game.", clientid)
        return False

    if succeeded == 0:
        _send_error(
            "Command /{} could not affect any active player.".format(normalized),
            clientid,
        )
        return False

    suffix = " ({} unavailable)".format(failures) if failures else ""
    _send_success(
        "APPLIED /{} TO {} PLAYER(S).{}".format(normalized, succeeded, suffix),
        clientid,
    )
    return True
