# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Maintained by Lucifer.
"""Shared V2 identity, role-permission, and private-response helpers.

All response text and RGB colors are configurable in this file's settings
section below; no separate response_config.py module is required.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from typing import Any

import bascenev1 as bs

from tools.account_ids import get_v2_account_id, is_v2_account_id

_LOG = logging.getLogger("Lucifer.CommandHandlers")


# ============================================================================
# USER CONFIGURATION: edit these values to customize response text and colors.
# Use normalized RGB floats only: each channel must be between 0.0 and 1.0.
# Examples: red=(1.0, 0.0, 0.0), green=(0.0, 1.0, 0.0), blue=(0.0, 0.0, 1.0).
# All message templates are ordinary Python text. For COMMAND_ACCEPTED_TEXT,
# use {role} where the role label should appear.
# ============================================================================
DEFAULT_COLOR_RGB = (0.78, 0.90, 1.0)

COMMAND_ACCEPTED_COLOR_RGB = (0.20, 1.0, 0.25)
COMMAND_ACCEPTED_TEXT = "\ue043{role} COMMAND ACCEPTED \ue043"

COMMAND_DENIED_COLOR_RGB = (1.0, 0.18, 0.18)
COMMAND_DENIED_TEXT = "\ue062 COMMAND NOT FOR KIDS \ue062"

# Display labels are separate from role keys in roles.json. Unknown role keys
# still receive a readable uppercase label.
_ROLE_LABELS = {
    "owner": "OWNER",
    "co-owner": "CO-OWNER",
    "co_owner": "CO-OWNER",
    "coowner": "CO-OWNER",
    "moderator": "MODERATOR",
    "leadstaff": "LEADSTAFF",
    "lead-staff": "LEADSTAFF",
    "cs": "COMPLAINT STAFF",
    "complaint-staff": "COMPLAINT STAFF",
    "complaint_staff": "COMPLAINT STAFF",
    "tcs": "T-COMPLAINT-STAFF",
    "admin": "ADMIN",
    "vip": "VIP",
}

# Keep this table at module scope instead of recreating it on every role lookup.
_ROLE_PRIORITY = {
    "owner": 100,
    "co-owner": 90,
    "co_owner": 90,
    "coowner": 90,
    "leadstaff": 80,
    "lead-staff": 80,
    "admin": 70,
    "moderator": 60,
    "cs": 50,
    "complaint-staff": 50,
    "complaint_staff": 50,
    "tcs": 40,
    "vip": 20,
}


def _normalise_rgb(
    value: Any,
    fallback: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Validate normalized RGB channels in Ballistica's 0.0..1.0 range."""
    try:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise TypeError("RGB color must be a three-item sequence")
        channels = tuple(float(component) for component in value)
        if len(channels) != 3:
            raise ValueError("RGB color must contain exactly three channels")
        if not all(math.isfinite(channel) and 0.0 <= channel <= 1.0
                   for channel in channels):
            raise ValueError("Normalized RGB channels must be between 0.0 and 1.0")
        return channels  # type: ignore[return-value]
    except (TypeError, ValueError, OverflowError):
        _LOG.warning(
            "Invalid normalized RGB color %r in handlers.py; using fallback %r.",
            value,
            fallback,
        )
        return fallback


# The configuration values above are source-level settings. Validate them once
# during module initialization instead of converting the same RGB values for
# every chat response.
_DEFAULT_COLOR = _normalise_rgb(DEFAULT_COLOR_RGB, (0.78, 0.90, 1.0))
_COMMAND_ACCEPTED_COLOR = _normalise_rgb(
    COMMAND_ACCEPTED_COLOR_RGB, (0.20, 1.0, 0.25)
)
_COMMAND_DENIED_COLOR = _normalise_rgb(
    COMMAND_DENIED_COLOR_RGB, (1.0, 0.18, 0.18)
)


def _format_text(template: Any, **values: str) -> str:
    """Safely format a user-editable response template."""
    if not isinstance(template, str):
        _LOG.warning("Response text must be a string; using an empty response.")
        return ""
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        _LOG.exception("Could not format response template %r.", template)
        return template


def clientid_to_accountid(clientid: int) -> str | None:
    """Resolve exactly one V2 ID for a connected client; never use V1 IDs."""
    if isinstance(clientid, bool):
        return None
    try:
        target_client_id = int(clientid)
    except (TypeError, ValueError):
        return None
    if target_client_id < 0:
        return None

    try:
        session = bs.get_foreground_host_session()
        if session is None:
            return None
        matches: set[str] = set()
        for session_player in getattr(session, "sessionplayers", ()):
            try:
                if int(session_player.inputdevice.client_id) != target_client_id:
                    continue
            except Exception:
                continue
            account_id = get_v2_account_id(session_player)
            if is_v2_account_id(account_id):
                matches.add(account_id)
        return next(iter(matches)) if len(matches) == 1 else None
    except Exception:
        _LOG.exception("Could not resolve client %s to a V2 account ID.", clientid)
        return None


def get_role_for_account(account_id: str | None) -> tuple[str, Mapping[str, Any]] | None:
    """Resolve configured roles and the deployment's protected owner."""
    if not is_v2_account_id(account_id):
        return None

    try:
        from playersdata import pdata
    except Exception:
        _LOG.exception("Could not import playersdata.pdata.")
        return None

    try:
        is_owner = pdata.is_owner_account_id(account_id)
    except Exception:
        _LOG.exception("Could not verify the protected owner.")
        is_owner = False

    if is_owner:
        try:
            roles = pdata.get_roles()
        except Exception:
            _LOG.exception("Could not load role metadata for the owner.")
            roles = {}

        if isinstance(roles, Mapping):
            for role_name, role_data in roles.items():
                if (
                    isinstance(role_name, str)
                    and role_name.casefold() == "owner"
                    and isinstance(role_data, Mapping)
                ):
                    owner_data = dict(role_data)
                    owner_data["commands"] = ["ALL"]
                    return role_name, owner_data

        return "owner", {"commands": ["ALL"]}

    try:
        roles = pdata.get_roles()
    except Exception:
        _LOG.exception("Could not load roles from playersdata.pdata.")
        return None

    if not isinstance(roles, Mapping):
        return None

    best_rank = -1
    best_match: tuple[str, Mapping[str, Any]] | None = None

    for role_name, role_data in roles.items():
        if not isinstance(role_data, Mapping):
            continue

        ids = role_data.get("ids", [])
        if not isinstance(ids, (list, tuple, set, frozenset)):
            continue
        if account_id not in ids:
            continue

        name = str(role_name)
        rank = _ROLE_PRIORITY.get(name.casefold(), 10)

        if rank > best_rank:
            best_rank = rank
            best_match = (name, role_data)

    return best_match


def get_role_label(account_id: str | None) -> str | None:
    """Return a readable uppercase role label for user-facing confirmations."""
    role = get_role_for_account(account_id)
    if role is None:
        return None
    key = role[0].casefold()
    return _ROLE_LABELS.get(key, key.replace("_", " ").replace("-", " ").upper())


def check_permissions(account_id: str | None, command: str) -> bool:
    """Allow a protected command only when the role grants it or ALL."""
    role = get_role_for_account(account_id)
    if role is None:
        return False
    commands = role[1].get("commands", [])
    if not isinstance(commands, (list, tuple, set, frozenset)):
        return False
    allowed = {str(item).casefold() for item in commands}
    return "all" in allowed or str(command).casefold() in allowed


def send_chat(message: str, clientid: int) -> None:
    """Send one or more lines to the command sender's private chat history."""
    try:
        target = int(clientid)
    except (TypeError, ValueError):
        return
    if target < 0:
        return
    try:
        for line in str(message).splitlines() or [""]:
            bs.chatmessage(line, clients=[target])
    except Exception:
        _LOG.exception("Could not send private chat output to client %s.", target)
        send(str(message), target)


def send(
    message: str,
    clientid: int,
    color: tuple[float, float, float] | Sequence[float] | None = None,
) -> None:
    """Send a transient colored response privately to one client."""
    try:
        target = int(clientid)
    except (TypeError, ValueError):
        return
    if target < 0:
        return

    final_color = (
        _normalise_rgb(color, _DEFAULT_COLOR) if color is not None else _DEFAULT_COLOR
    )

    try:
        bs.broadcastmessage(
            str(message), color=final_color, transient=True, clients=[target]
        )
    except Exception:
        _LOG.exception("Could not send private notification to client %s.", target)


def send_denied(clientid: int) -> None:
    """Show the user-configurable protected-command denial message."""
    color = _COMMAND_DENIED_COLOR
    message = _format_text(COMMAND_DENIED_TEXT)
    send(message, clientid, color=color)


def send_command_accepted(account_id: str | None, clientid: int) -> None:
    """Show a user-configurable confirmation to configured role holders."""
    role_label = get_role_label(account_id)
    if role_label:
        color = _COMMAND_ACCEPTED_COLOR
        message = _format_text(COMMAND_ACCEPTED_TEXT, role=role_label)
        send(message, clientid, color=color)
