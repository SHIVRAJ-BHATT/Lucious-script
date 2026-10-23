# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Role-protected fun commands for BombSquad 1.8.x / API 9."""

from __future__ import annotations

import logging
import math
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers

_LOG = logging.getLogger("Lucifer.FunCommands")
_ERROR = "\ue062"

# Keep only the commands requested for this server.
Commands = ["celebrate", "speed", "floater", "snow"]
CommandAliases = ["celeb", "flo"]
_COMMAND_SET = frozenset(Commands)
_ALIAS_TO_COMMAND = {
    "celeb": "celebrate",
    "flo": "floater",
}


def canonical_command(command: str) -> str:
    """Return the canonical command name for an alias."""
    normalized = str(command).strip().casefold().lstrip("/")
    return _ALIAS_TO_COMMAND.get(normalized, normalized)


def _error(message: str, clientid: int) -> None:
    """Send a consistently styled command error without hiding send failures."""
    styled_message = f"{_ERROR}  \ue04c {message} \ue04c  {_ERROR}"
    try:
        handlers.send(styled_message, clientid, color=(1.0, 0.18, 0.18))
    except Exception:
        _LOG.exception("Could not send command error to client %s.", clientid)


def _activity() -> Any | None:
    """Return the foreground host activity, or None when unavailable."""
    try:
        return bs.get_foreground_host_activity()
    except Exception:
        _LOG.warning("Could not get foreground host activity.", exc_info=True)
        return None


def _safe_actor(player: Any) -> Any | None:
    """Return a player's live actor, if one is available."""
    try:
        actor = player.actor
        if actor is None:
            return None
        node = actor.node
        if node is None or not node.exists():
            return None
        return actor
    except Exception:
        return None


def _targets(
    arguments: list[str], command: str
) -> tuple[Any | None, list[Any], str | None]:
    """Resolve a zero-based activity player ID or 'all'."""
    activity = _activity()
    if activity is None:
        return None, [], "There is no active game."
    if len(arguments) != 1:
        return activity, [], f"Usage: /{command} <playerID|all>"

    token = str(arguments[0]).strip().casefold()
    if not token:
        return activity, [], f"Usage: /{command} <playerID|all>"
    try:
        players = list(getattr(activity, "players", ()))
    except Exception:
        _LOG.exception("Could not read activity players for /%s.", command)
        return activity, [], "Could not read the current player list."

    if token == "all":
        return activity, players, None
    try:
        index = int(token)
    except (TypeError, ValueError, OverflowError):
        return activity, [], "Player ID must be a number or 'all'."
    if index < 0 or index >= len(players):
        return activity, [], "Player ID not found; use /list to check IDs."
    return activity, [players[index]], None


def _run_celebrate(arguments: list[str], clientid: int) -> bool:
    activity, players, error = _targets(arguments, "celebrate")
    if error:
        _error(error, clientid)
        return False

    # Resolve actors in one pass; dead/transitioning players are skipped.
    valid: list[tuple[Any, Any]] = []
    for player in players:
        actor = _safe_actor(player)
        if actor is not None:
            valid.append((player, actor))
    if not valid:
        _error("No living player characters were found for that command.", clientid)
        return False

    succeeded = 0
    try:
        with activity.context:
            for _player, actor in valid:
                try:
                    actor.handlemessage(bs.CelebrateMessage())
                    succeeded += 1
                except Exception:
                    _LOG.exception("Could not run /celebrate for a player.")
    except Exception:
        _LOG.exception("Could not run /celebrate in the current activity.")
        _error("Could not apply that command in the current game.", clientid)
        return False

    if succeeded == 0:
        _error("The command could not affect any selected player.", clientid)
        return False
    _LOG.info("/celebrate completed for %s player(s), requested by client %s.", succeeded, clientid)
    return True


def _set_speed(arguments: list[str], clientid: int) -> bool:
    """Set custom speed when available, otherwise use normal/slow motion."""
    if len(arguments) != 1 or not arguments[0]:
        _error(
            "Usage: /speed <value> (0<value<1 = slow motion, 1 = normal)",
            clientid,
        )
        return False
    try:
        value = float(arguments[0])
    except (TypeError, ValueError, OverflowError):
        _error("Speed must be a number.", clientid)
        return False
    if not math.isfinite(value) or value <= 0.0:
        _error("Speed must be greater than zero.", clientid)
        return False

    # Use the existing custom speed controller if this server has one. Keep the
    # optional import separate from the call so an ImportError raised *inside*
    # the controller is not silently mistaken for an absent optional module.
    try:
        from tools import corelib  # type: ignore[import-not-found]
    except ImportError:
        corelib = None
    except Exception:
        _LOG.exception("Could not import optional tools.corelib speed controller.")
        _error("The custom speed controller could not be loaded; check the server terminal.", clientid)
        return False

    if corelib is not None:
        setter = getattr(corelib, "set_speed", None)
        if callable(setter):
            try:
                setter(value)
            except Exception:
                _LOG.exception("Optional tools.corelib.set_speed failed.")
                _error("The custom speed controller failed; check the server terminal.", clientid)
                return False
            _LOG.debug("Custom speed set to %s by client %s.", value, clientid)
            return True

    if value > 1.0:
        _error(
            "This API 9 setup supports normal speed or standard slow motion only; "
            "values above 1 need a custom speed controller.",
            clientid,
        )
        return False

    activity = _activity()
    if activity is None:
        _error("There is no active game.", clientid)
        return False
    try:
        with activity.context:
            activity.globalsnode.slow_motion = value < 1.0
    except Exception:
        _LOG.exception("Could not change game speed mode.")
        _error("Could not change the current game speed.", clientid)
        return False

    _LOG.info("Slow-motion mode set to %s by client %s.", value < 1.0, clientid)
    return True


def _run_floater(arguments: list[str], clientid: int) -> bool:
    """Call the Floater feature and log its result for server-side diagnosis."""
    _LOG.debug("/floater requested by client %s with arguments=%r.", clientid, arguments)

    if len(arguments) > 1:
        _error("Usage: /floater [clientID]", clientid)
        return False

    target_client = clientid
    if arguments and str(arguments[0]).strip():
        try:
            target_client = int(str(arguments[0]).strip())
        except (TypeError, ValueError, OverflowError):
            _error("Client ID must be a number.", clientid)
            return False
        if target_client < 0:
            _error("Client ID must be non-negative.", clientid)
            return False

    try:
        from features import floater

        _LOG.debug(
            "Loaded Floater module from %s.",
            getattr(floater, "__file__", "<unknown path>"),
        )

        # Prefer the API 9 snake-case entry point; retain the legacy alias for
        # compatibility with older versions of this feature module.
        assign = getattr(floater, "assign_flo_inputs", None)
        if not callable(assign):
            assign = getattr(floater, "assignFloInputs", None)
        if not callable(assign):
            _LOG.error(
                "Floater module has neither assign_flo_inputs nor assignFloInputs."
            )
            _error("Floater feature has no compatible entry point; check the server terminal.", clientid)
            return False

        _LOG.debug(
            "Calling Floater activation for target client %s (requester=%s).",
            target_client,
            clientid,
        )
        result = assign(target_client)
        _LOG.debug(
            "Floater activation returned %r for target client %s.",
            result,
            target_client,
        )

        # Some legacy versions return None on success; only explicit False is
        # treated as an activation failure.
        if result is False:
            _error(
                "Floater could not be enabled; check that the target is alive and in-game.",
                clientid,
            )
            return False

        try:
            bs.broadcastmessage(
                "\ue048  \ue043 FLOATER ENABLED \ue043  \ue048",
                color=(0.0, 1.0, 1.0),
                transient=True,
            )
        except Exception:
            _LOG.exception("Could not broadcast Floater confirmation.")

    except Exception as exc:
        _LOG.exception(
            "Could not enable Floater for target client %s (requester=%s).",
            target_client,
            clientid,
        )
        _error(f"Floater error: {type(exc).__name__}; check the server terminal.", clientid)
        return False

    return True


def _toggle_snowfall(arguments: list[str], clientid: int) -> bool:
    """Toggle snowfall for this server process without changing setting.json."""
    if arguments:
        _error("Usage: /snow", clientid)
        return False

    if _activity() is None:
        _error("There is no active game.", clientid)
        return False

    try:
        from features import snowfall

        if not snowfall.install():
            _error("Snowfall could not be initialized; check the server terminal.", clientid)
            return False

        enabled = not snowfall.is_enabled()
        snowfall.set_runtime_enabled(enabled)

        message = (
            "\ue048  \ue043 SNOWFALL ENABLED \ue043  \ue048"
            if enabled
            else "\ue048  \ue062 SNOWFALL DISABLED \ue062  \ue048"
        )
        color = (0.35, 0.85, 1.0) if enabled else (1.0, 0.65, 0.35)
        try:
            bs.broadcastmessage(message, color=color, transient=True)
        except Exception:
            _LOG.exception("Could not broadcast snowfall toggle confirmation.")
        _LOG.info("Snowfall %s by client %s.", "enabled" if enabled else "disabled", clientid)
        return True
    except Exception:
        _LOG.exception("Could not toggle snowfall.")
        _error("Snowfall could not be toggled; check the server terminal.", clientid)
        return False


def ExcelCommand(
    command: str,
    arguments: list[str],
    clientid: int,
    accountid: str | None,
) -> bool:
    """Run a protected fun command; permissions are checked by the executor."""
    del accountid
    normalized = canonical_command(command)
    if normalized not in _COMMAND_SET:
        return False
    handler = _COMMAND_HANDLERS.get(normalized)
    if handler is None:
        return False
    return handler(arguments, clientid)


# Build the dispatch table once at module import instead of checking each branch
# on every command. Keep the public Commands list unchanged for the dispatcher.
_COMMAND_HANDLERS = {
    "celebrate": _run_celebrate,
    "speed": _set_speed,
    "floater": _run_floater,
    "snow": _toggle_snowfall,
}
