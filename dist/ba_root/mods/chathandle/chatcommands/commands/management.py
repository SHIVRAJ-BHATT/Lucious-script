# ba_meta require api 9
"""Protected management commands for the API 9 server."""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import math
import pkgutil
import re
import textwrap
from functools import lru_cache

import babase
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers
from playersdata import pdata
from spazmod import tag as tag_system
from tools.account_ids import is_v2_account_id
from tools import playlist as playlist_manager

_LOG = logging.getLogger("Lucifer.ManagementCommands")
_ERROR = (1.0, 0.18, 0.18)

# Consistent on-screen styling for management-command feedback.
# These glyphs are already used by the server's working tag/branding messages.
_UI_GLYPHS = ("\ue043", "\ue048", "\ue049", "\ue062")


def _style_display_text(message: Any) -> str:
    """Format command feedback with the server's decorative glyphs."""
    text = str(message).strip()
    if not text:
        return text

    # Preserve messages that already contain the project's glyph styling.
    if any(glyph in text for glyph in _UI_GLYPHS):
        return text

    # For lists/details, turn the first line into a heading and add a footer.
    if "\n" in text:
        rows = text.splitlines()
        if not rows:
            return f"\ue043 {text} \ue043"
        title = rows[0].strip()
        if title:
            rows[0] = f"\ue048 ═══ {title} ═══ \ue048"
        rows.append("\ue049 ═══════════════════════ \ue049")
        return "\n".join(rows)

    return f"\ue043  {text}  \ue043"


def _send_pretty(message: Any, clientid: int, **kwargs: Any) -> Any:
    """Send a privately addressed message with consistent decorative styling."""
    return handlers.send(_style_display_text(message), clientid, **kwargs)


def _send_pretty_chat(message: Any, clientid: int, **kwargs: Any) -> Any:
    """Send a chat-style message with consistent decorative styling."""
    return handlers.send_chat(_style_display_text(message), clientid, **kwargs)

# Prevent duplicate restart schedules if multiple commands arrive together.
_RESTART_PENDING = False
_RESTART_NOTICE_SECONDS = 4.0
_RESTART_DISCONNECT_GRACE_SECONDS = 1.5

Commands = [
    # Existing moderation, role, and tag commands.
    "end", "kick", "ban", "unban", "mute", "unmute",
    "quit", "restart", "party", "remove",
    "mutechat", "unmutechat",
    "createrole", "addrole", "removerole", "getroles",
    "customtag", "removetag", "changetag",
    "effects", "customeffect", "removeeffect",
    # Phase 6 game/session controls.
    "pause", "resume", "slowmo", "nv", "day", "tint", "cameramode",
    "maxplayers", "playlist",
    # Phase 8: dynamic role-command permissions.
    "addcommand", "removecommand",
    # Phase 9: player information and recent history.
    "info", "gp", "lm", "recents",
]

CommandAliases = [
    "mutechat", "unmutechat",
    "next", "pausegame", "sm", "slow", "night", "dv",
    "max", "rm",
    "addcmd", "removecmd", "effect",
]


def canonical_command(command: str) -> str:
    """Return the canonical protected command for a supported alias."""
    normalized = str(command).strip().casefold().lstrip("/")
    return "customeffect" if normalized == "effect" else normalized


def _send_error(message: str, clientid: int) -> None:
    styled = message.strip()
    if not any(glyph in styled for glyph in _UI_GLYPHS):
        styled = f"\ue062  {styled}  \ue062"
    _send_pretty(styled, clientid, color=_ERROR)


def _parse_client_id(value: str) -> int | None:
    try:
        client_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return client_id if client_id >= 0 else None


def _target_account_id(client_id: int) -> str | None:
    """Resolve a connected target only through the validated V2-ID helper."""
    return handlers.clientid_to_accountid(client_id)


def _refresh_all_connected_tags() -> None:
    """Update visible tags for connected players after a role tag changes."""
    try:
        session = bs.get_foreground_host_session()
        if session is None:
            return
        for session_player in getattr(session, "sessionplayers", ()):
            try:
                client_id = int(session_player.inputdevice.client_id)
            except Exception:
                continue
            tag_system.refresh_client_tag(client_id)
    except Exception:
        _LOG.exception("Could not refresh connected tags after a role-tag change.")


def _customtag(arguments: list[str], clientid: int) -> bool:
    if len(arguments) < 2:
        _send_error("Usage: /customtag <tagname> <clientID>", clientid)
        return False

    target_id = _parse_client_id(arguments[-1])
    tag_text = " ".join(arguments[:-1]).strip()
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False
    if not tag_text or len(tag_text) > 32:
        _send_error("Tag must contain 1-32 characters.", clientid)
        return False

    account_id = _target_account_id(target_id)
    if not account_id:
        _send_error("Target is not connected with a valid V2 account ID.", clientid)
        return False
    if not pdata.set_custom_tag(account_id, tag_text):
        _send_error("Could not save custom tag. Check the tag and V2 account ID.", clientid)
        return False

    refreshed = tag_system.refresh_client_tag(target_id)
    detail = f"\ue043 Tag set • {tag_text} • {target_id} \ue043"
    if not refreshed:
        detail += " | \ue049 Saved for next spawn"
    _send_pretty(detail, clientid)
    return True


def _removetag(arguments: list[str], clientid: int) -> bool:
    if len(arguments) != 1:
        _send_error("Usage: /removetag <clientID>", clientid)
        return False

    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False

    account_id = _target_account_id(target_id)
    if not account_id:
        _send_error("Target is not connected with a valid V2 account ID.", clientid)
        return False
    if not pdata.remove_custom_tag(account_id):
        _send_error("This player has no saved custom tag.", clientid)
        return False

    refreshed = tag_system.refresh_client_tag(target_id)
    detail = f"\ue043 Custom tag cleared • {target_id} \ue043"
    if not refreshed:
        detail += " | \ue049 Updates on next spawn"
    else:
        detail += " | Role tag preserved"
    _send_pretty(detail, clientid)
    return True


def _parse_client_id_list(values: list[str]) -> list[int] | None:
    """Parse comma-separated non-negative session client IDs without ambiguity."""
    parsed: list[int] = []
    seen: set[int] = set()
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                return None
            client_id = _parse_client_id(token)
            if client_id is None:
                return None
            if client_id not in seen:
                seen.add(client_id)
                parsed.append(client_id)
    return parsed or None


def _resolve_actor_candidate(candidate: Any) -> Any | None:
    """Resolve actor-like properties or weak/proxy accessors."""
    if callable(candidate) and not hasattr(candidate, "node"):
        try:
            return candidate()
        except Exception:
            return None
    return candidate


def _is_live_character(spaz: Any) -> bool:
    """Return True only when a character actor has a live scene node."""
    node = getattr(spaz, "node", None) if spaz is not None else None
    try:
        if node is None or not node.exists():
            return False
    except Exception:
        return False
    alive = getattr(spaz, "is_alive", None)
    if callable(alive):
        try:
            return bool(alive())
        except Exception:
            return False
    return True


def _refresh_effects_for_client(client_id: int, account_id: str) -> bool:
    """Immediately refresh an online live character, if one currently exists."""
    try:
        activity = bs.get_foreground_host_activity()
    except Exception:
        try:
            activity = bs.getactivity()
        except babase._error.ActivityNotFoundError:
            return False
        except Exception:
            return False
    if activity is None:
        return False

    try:
        from spazmod import spaz_effects
        session_player = _find_session_player(client_id)
        candidates: list[Any] = []

        # Prefer activity-owned PlayerSpaz actors; SessionPlayer may expose a
        # stale actor reference during character transitions.
        for activity_player in getattr(activity, "players", ()):
            owner = getattr(activity_player, "sessionplayer", None)
            try:
                owner_client_id = int(owner.inputdevice.client_id)
            except Exception:
                continue
            if owner_client_id == client_id:
                candidates.append(getattr(activity_player, "actor", None))

        # Fall back only after trying the current gameplay Player's actor.
        if session_player is not None:
            candidates.extend((
                getattr(session_player, "actor", None),
                getattr(getattr(session_player, "player", None), "actor", None),
            ))

        seen: set[int] = set()
        for candidate in candidates:
            spaz = _resolve_actor_candidate(candidate)
            if spaz is None or id(spaz) in seen:
                continue
            seen.add(id(spaz))
            if not _is_live_character(spaz):
                continue

            # Chat callbacks may be running in a session/lobby context. Refresh
            # nodes and stop the old controller in the character's own activity.
            actor_activity = None
            activity_getter = getattr(spaz, "_activity", None)
            if callable(activity_getter):
                try:
                    actor_activity = activity_getter()
                except Exception:
                    actor_activity = None
            refresh_activity = actor_activity or activity
            try:
                with refresh_activity.context:
                    if not _is_live_character(spaz):
                        continue
                    return spaz_effects.refresh_player_effects(spaz, account_id)
            except babase._error.ActivityNotFoundError:
                return False
    except Exception:
        _LOG.exception("Could not refresh custom effects for client %s.", client_id)
    return False
def _effects(arguments: list[str], clientid: int) -> bool:
    """Display the supported custom effects in readable categories."""
    if arguments:
        _send_error("Usage: /effects", clientid)
        return False

    try:
        from spazmod.spaz_effects import EFFECT_NAMES, normalize_effect_name

        available = {
            normalized
            for item in EFFECT_NAMES
            if (normalized := normalize_effect_name(item)) is not None
        }
    except Exception:
        _LOG.exception("Could not load the custom-effects registry for /effects.")
        _send_error("The effects list is unavailable; check the server log.", clientid)
        return False

    categories = (
        ("PARTICLES", ("spark", "sparkground", "sweat", "sweatground", "fairydust")),
        ("COLOR", ("rainbow", "new_rainbow", "shine", "highlightshine")),
        ("MATERIAL", ("ice", "iceground", "slime", "metal", "splinter")),
        ("VISUAL", ("glow", "distortion", "scorch", "fire", "stars")),
        ("SPECIAL", ("surrounder", "footprint", "chispitas", "darkmagic")),
        ("COMBINED", ("colorfullspark", "ring", "brust", "ringstars")),
        ("OTHER", ("noeffect",)),
    )

    lines = [f"*** CUSTOM EFFECTS ({len(available)}) ***"]
    categorized: set[str] = set()
    for title, names in categories:
        present = [name for name in names if name in available]
        if not present:
            continue
        categorized.update(present)
        lines.append(f"[{title}] " + " | ".join(present))

    # Keep the list complete if future effects are added to EFFECT_NAMES before
    # they receive a dedicated display category here.
    additional = sorted(available - categorized)
    if additional:
        lines.append("[MORE] " + " | ".join(additional))

    lines.extend((
        "",
        "Use: /customeffect <effect> <clientID>",
        "Multiple: /customeffect spark rainbow 2",
        "Remove: /removeeffect <effect|all> <clientID>",
    ))
    _send_pretty_chat("\n".join(lines), clientid)
    return True


def _customeffect(arguments: list[str], clientid: int) -> bool:
    """Assign one or more supported effects to one or more connected clients."""
    if len(arguments) < 2:
        _send_error(
            "Usage: /customeffect <effect> [effect2 ...] <clientID[,clientID...]>.",
            clientid,
        )
        return False

    effect_arguments = arguments[:-1]
    target_ids = _parse_client_id_list([arguments[-1]])
    if target_ids is None:
        _send_error("Provide valid client IDs, separated by commas if needed.", clientid)
        return False

    try:
        from spazmod.spaz_effects import normalize_effect_name
        effect_names: list[str] = []
        for token in effect_arguments:
            effect_name = normalize_effect_name(token)
            if effect_name is None:
                _send_error(
                    f"Unknown effect '{token}'. Check the supported effect names in spaz_effects.py.",
                    clientid,
                )
                return False
            if effect_name not in effect_names:
                effect_names.append(effect_name)
    except Exception:
        _LOG.exception("Could not load the custom-effects registry.")
        _send_error("Custom effects are unavailable; check the server log.", clientid)
        return False

    targets: list[tuple[int, str]] = []
    for target_id in target_ids:
        account_id = _target_account_id(target_id)
        if not is_v2_account_id(account_id):
            _send_error(
                f"Client {target_id} is not connected with a valid V2 account ID; no V1 fallback is used.",
                clientid,
            )
            return False
        targets.append((target_id, account_id))

    try:
        for target_id, account_id in targets:
            for effect_name in effect_names:
                if not pdata.set_effect(effect_name, account_id):
                    _send_error(
                        f"Could not save effect '{effect_name}' for client {target_id}.",
                        clientid,
                    )
                    return False
    except Exception:
        _LOG.exception("Failed to persist a custom effect assignment.")
        _send_error("Could not save effects.json; check its JSON syntax and the server log.", clientid)
        return False

    refresh_notes: list[str] = []
    for target_id, account_id in targets:
        if not _refresh_effects_for_client(target_id, account_id):
            refresh_notes.append(str(target_id))

    target_text = ", ".join(str(target_id) for target_id, _ in targets)
    detail = f"\ue043 Effects added • {', '.join(effect_names)} • {target_text} \ue043"
    if refresh_notes:
        detail += f" | \ue049 Next spawn: {', '.join(refresh_notes)}"
    _send_pretty(detail, clientid)
    return True


def _removeeffect(arguments: list[str], clientid: int) -> bool:
    """Remove selected custom effects, or all effects, from connected clients."""
    if len(arguments) < 2:
        _send_error(
            "Usage: /removeeffect <effect|all> [effect2 ...] <clientID[,clientID...]>. ",
            clientid,
        )
        return False

    remove_all = arguments[0].strip().casefold() == "all"
    if remove_all:
        target_ids = _parse_client_id_list(arguments[1:])
        effect_names: list[str] = []
    else:
        target_ids = _parse_client_id_list([arguments[-1]])
        try:
            from spazmod.spaz_effects import normalize_effect_name
            effect_names = []
            for token in arguments[:-1]:
                effect_name = normalize_effect_name(token)
                if effect_name is None:
                    _send_error(
                        f"Unknown effect '{token}'. Check the supported effect names in spaz_effects.py.",
                        clientid,
                    )
                    return False
                if effect_name not in effect_names:
                    effect_names.append(effect_name)
        except Exception:
            _LOG.exception("Could not load the custom-effects registry.")
            _send_error("Custom effects are unavailable; check the server log.", clientid)
            return False

    if target_ids is None:
        _send_error("Provide valid client IDs, separated by commas if needed.", clientid)
        return False

    targets: list[tuple[int, str]] = []
    for target_id in target_ids:
        account_id = _target_account_id(target_id)
        if not is_v2_account_id(account_id):
            _send_error(
                f"Client {target_id} is not connected with a valid V2 account ID; no V1 fallback is used.",
                clientid,
            )
            return False
        targets.append((target_id, account_id))

    changed_by_client: dict[int, list[str]] = {}
    try:
        for target_id, account_id in targets:
            if remove_all:
                removed = pdata.remove_all_effects(account_id)
                if removed:
                    changed_by_client[target_id] = ["all"]
            else:
                removed_names = [
                    effect_name for effect_name in effect_names
                    if pdata.remove_effect(account_id, effect_name)
                ]
                if removed_names:
                    changed_by_client[target_id] = removed_names
    except Exception:
        _LOG.exception("Failed to remove custom effects.")
        _send_error("Could not update effects.json; check its JSON syntax and the server log.", clientid)
        return False

    if not changed_by_client:
        _send_error("No matching saved effects were found for the selected client(s).", clientid)
        return False

    refresh_notes: list[str] = []
    for target_id, account_id in targets:
        if not _refresh_effects_for_client(target_id, account_id):
            refresh_notes.append(str(target_id))

    if remove_all:
        detail = "\ue043 Effects cleared • " + ", ".join(map(str, changed_by_client)) + " \ue043"
    else:
        # Report only effects actually removed from each target.
        parts = [
            f"{target_id}: {', '.join(names)}"
            for target_id, names in changed_by_client.items()
        ]
        detail = "\ue043 Effects removed • " + " | ".join(parts) + " \ue043"
    if refresh_notes:
        detail += f" | \ue049 Next spawn: {', '.join(refresh_notes)}"
    _send_pretty(detail, clientid)
    return True


def _changetag(arguments: list[str], clientid: int) -> bool:
    if len(arguments) < 2:
        _send_error("Usage: /changetag <tag> <rolename>", clientid)
        return False

    tag_text = " ".join(arguments[:-1]).strip()
    role_name = arguments[-1].strip()
    if not tag_text or len(tag_text) > 32:
        _send_error("Tag must contain 1-32 characters.", clientid)
        return False

    try:
        changed_role = pdata.change_role_tag(role_name, tag_text)
    except Exception:
        _LOG.exception("Failed to persist role tag change.")
        _send_error("Could not save roles.json; check the server log.", clientid)
        return False

    if changed_role is None:
        _send_error(f"Role '{role_name}' was not found or tag is invalid.", clientid)
        return False

    _refresh_all_connected_tags()
    _send_pretty(f"\ue043 Role tag updated • {changed_role} \ue043", clientid)
    return True



def _actor_is_owner(account_id: str | None) -> bool:
    """Fail closed when checking whether the command sender is the owner."""
    try:
        return pdata.is_owner_account_id(account_id)
    except Exception:
        _LOG.exception("Could not verify owner identity for role management.")
        return False


def _resolve_role_name(role_name: str) -> str | None:
    """Resolve an existing role name case-insensitively."""
    try:
        roles = pdata.get_roles(force_reload=True)
    except TypeError:
        # Compatibility if an older pdata.py version lacks force_reload.
        roles = pdata.get_roles()
    folded = role_name.strip().casefold()
    return next(
        (key for key in roles if isinstance(key, str) and key.casefold() == folded),
        None,
    )


def _createrole(arguments: list[str], clientid: int, actor_account_id: str | None) -> bool:
    if not arguments:
        _send_error("Usage: /createrole <rolename>", clientid)
        return False
    role_name = " ".join(arguments).strip()
    if not role_name or len(role_name) > 32 or any(ord(ch) < 32 for ch in role_name):
        _send_error("Role name must contain 1-32 printable characters.", clientid)
        return False
    if role_name.casefold() == "owner" and not _actor_is_owner(actor_account_id):
        _send_error("Only the owner can create the reserved owner role.", clientid)
        return False
    try:
        created = pdata.create_role(role_name)
    except Exception:
        _LOG.exception("Could not create role %r.", role_name)
        _send_error("Could not save roles.json; check the server log.", clientid)
        return False
    if created is None:
        _send_error("Role name is invalid or already exists (case-insensitive check).", clientid)
        return False
    _send_pretty(
        f"Created role '{created}'. Its command permissions are empty by default.",
        clientid,
    )
    return True


def _addrole(
    arguments: list[str], clientid: int, actor_account_id: str | None
) -> bool:
    if len(arguments) < 2:
        _send_error("Usage: /addrole <rolename> <clientID>", clientid)
        return False

    target_id = _parse_client_id(arguments[-1])
    role_name = " ".join(arguments[:-1]).strip()
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False
    if not role_name:
        _send_error("Role name cannot be empty.", clientid)
        return False

    actual_role = _resolve_role_name(role_name)
    if actual_role is None:
        _send_error(f"Role '{role_name}' does not exist. Use /createrole first.", clientid)
        return False
    if actual_role.casefold() == "owner" and not _actor_is_owner(actor_account_id):
        _send_error("Only the owner can assign the reserved owner role.", clientid)
        return False

    target_account_id = _target_account_id(target_id)
    if not target_account_id:
        _send_error("Target must be connected with a valid V2 account ID.", clientid)
        return False

    try:
        existing_roles = pdata.get_player_roles(target_account_id)
        already_assigned = any(role.casefold() == actual_role.casefold() for role in existing_roles)
        saved_role = pdata.add_player_role(actual_role, target_account_id)
    except Exception:
        _LOG.exception("Could not assign role %s to client %s.", actual_role, target_id)
        _send_error("Could not update roles.json; check the server log.", clientid)
        return False

    if saved_role is None:
        _send_error("Role assignment failed; verify the role and V2 account ID.", clientid)
        return False
    if already_assigned:
        _send_pretty(f"\ue043 {target_id} already has role • {saved_role} \ue043", clientid)
        return False

    refreshed = tag_system.refresh_client_tag(target_id)
    response = f"\ue043 Role added • {saved_role} • {target_id} \ue043"
    if not refreshed:
        response += " | \ue049 Tag updates on next spawn"
    _send_pretty(response, clientid)
    return True


def _removerole(
    arguments: list[str], clientid: int, actor_account_id: str | None
) -> bool:
    if len(arguments) < 2:
        _send_error("Usage: /removerole <rolename> <clientID>", clientid)
        return False

    target_id = _parse_client_id(arguments[-1])
    role_name = " ".join(arguments[:-1]).strip()
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False
    if not role_name:
        _send_error("Role name cannot be empty.", clientid)
        return False

    actual_role = _resolve_role_name(role_name)
    if actual_role is None:
        _send_error(f"Role '{role_name}' does not exist.", clientid)
        return False
    if actual_role.casefold() == "owner" and not _actor_is_owner(actor_account_id):
        _send_error("Only the owner can remove the reserved owner role.", clientid)
        return False

    target_account_id = _target_account_id(target_id)
    if not target_account_id:
        _send_error("Target must be connected with a valid V2 account ID.", clientid)
        return False

    try:
        target_is_owner = pdata.is_owner_account_id(target_account_id)
    except Exception:
        _LOG.exception("Could not verify whether client %s is the owner.", target_id)
        _send_error("Could not verify owner protection; action cancelled.", clientid)
        return False
    if target_is_owner and not _actor_is_owner(actor_account_id):
        _send_error("The owner account's roles are protected from role removal.", clientid)
        return False

    try:
        removed_role = pdata.remove_player_role(actual_role, target_account_id)
    except Exception:
        _LOG.exception("Could not remove role %s from client %s.", actual_role, target_id)
        _send_error("Could not update roles.json; check the server log.", clientid)
        return False
    if removed_role is None:
        _send_error(f"Client {target_id} does not have role '{actual_role}'.", clientid)
        return False

    refreshed = tag_system.refresh_client_tag(target_id)
    response = f"\ue043 Role removed • {removed_role} • {target_id} \ue043"
    if not refreshed:
        response += " | \ue049 Tag updates on next spawn"
    _send_pretty(response, clientid)
    return True


def _getroles(arguments: list[str], clientid: int) -> bool:
    if len(arguments) != 1:
        _send_error("Usage: /getroles <clientID>", clientid)
        return False
    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False
    target_account_id = _target_account_id(target_id)
    if not target_account_id:
        _send_error("Target must be connected with a valid V2 account ID.", clientid)
        return False
    try:
        roles = pdata.get_player_roles(target_account_id)
    except Exception:
        _LOG.exception("Could not read roles for client %s.", target_id)
        _send_error("Could not read roles.json; check the server log.", clientid)
        return False
    if not roles:
        _send_pretty(f"Client {target_id} has no assigned roles.", clientid)
        return True
    _send_pretty(f"Client {target_id} roles: {', '.join(roles)}", clientid)
    return True


@lru_cache(maxsize=1)
def _installed_command_modules() -> tuple[Any, ...]:
    """Return cached references to this and importable sibling command modules."""
    modules: list[Any] = [importlib.import_module(__name__)]
    seen = {getattr(modules[0], "__name__", __name__)}
    try:
        from chathandle.chatcommands import commands as command_package
        for module_info in pkgutil.iter_modules(getattr(command_package, "__path__", ())):
            if module_info.name.startswith("_"):
                continue
            module_name = f"{command_package.__name__}.{module_info.name}"
            if module_name in seen:
                continue
            try:
                module = importlib.import_module(module_name)
            except Exception:
                _LOG.debug(
                    "Skipping unavailable command module %s during permission lookup.",
                    module_name,
                    exc_info=True,
                )
                continue
            modules.append(module)
            seen.add(module_name)
    except Exception:
        _LOG.exception("Could not inspect sibling command modules for permission lookup.")
    return tuple(modules)


def _normalise_permission_command(value: Any) -> str:
    """Normalize a command token for registration and role storage."""
    return str(value).strip().casefold().lstrip("/")


@lru_cache(maxsize=1)
def _registered_protected_command_names() -> frozenset[str]:
    """Return cached protected command names and aliases exported by modules."""
    names: set[str] = set()
    for module in _installed_command_modules():
        for attribute in ("Commands", "CommandAliases"):
            values = getattr(module, attribute, ()) or ()
            if not isinstance(values, (list, tuple, set, frozenset)):
                continue
            names.update(
                _normalise_permission_command(item)
                for item in values
                if _normalise_permission_command(item)
            )
    # ALL is a wildcard permission and not an executable command.
    names.discard("all")
    return frozenset(names)


@lru_cache(maxsize=1)
def _command_permission_families() -> dict[str, tuple[str, ...]]:
    """Discover aliases from each command module's alias map and dispatch logic.

    A command and its aliases are one permission family. Modules in this script
    express these relationships in several ways: `_ALIAS_TO_COMMAND` mappings,
    command checks such as `command in ('godmode', 'gm')`, and action dictionaries
    that point aliases to the same handler. Read those existing declarations so
    permission management follows the source-of-truth command implementations.
    """
    modules = _installed_command_modules()
    registered: set[str] = set()
    for module in modules:
        for attribute in ("Commands", "CommandAliases"):
            values = getattr(module, attribute, ()) or ()
            if isinstance(values, (list, tuple, set, frozenset)):
                registered.update(
                    _normalise_permission_command(item)
                    for item in values
                    if _normalise_permission_command(item)
                )
    registered.discard("all")
    parent = {name: name for name in registered}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(names: Any) -> None:
        cleaned = list(dict.fromkeys(
            _normalise_permission_command(item)
            for item in names
            if _normalise_permission_command(item) in registered
        ))
        if len(cleaned) < 2:
            return
        root = find(cleaned[0])
        for name in cleaned[1:]:
            other = find(name)
            if other != root:
                parent[other] = root

    for module in modules:
        # Explicit alias -> primary command maps are the clearest declaration.
        for attribute in (
            "_ALIAS_TO_COMMAND", "ALIAS_TO_COMMAND", "COMMAND_ALIASES",
            "COMMAND_ALIAS_MAP", "ALIASES", "aliases",
        ):
            mapping = getattr(module, attribute, None)
            if isinstance(mapping, dict):
                for alias, primary in mapping.items():
                    if isinstance(primary, str):
                        union((alias, primary))

        # Also infer groups directly from ExcelCommand dispatch branches and
        # action maps. This covers modules that do not expose an alias dictionary.
        try:
            source = inspect.getsource(module)
            tree = ast.parse(textwrap.dedent(source))
        except Exception:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name):
                variable = node.left.id.casefold()
                is_command_variable = (
                    "command" in variable or variable in {"cmd", "normalized"}
                )
                if not is_command_variable or not any(isinstance(op, ast.In) for op in node.ops):
                    continue
                for comparator in node.comparators:
                    if isinstance(comparator, (ast.List, ast.Tuple, ast.Set)):
                        literal_names = [
                            item.value for item in comparator.elts
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                        ]
                        union(literal_names)

            if isinstance(node, ast.Dict):
                # In dispatch dictionaries, aliases pointing at the same function
                # are equivalent (e.g. {'gm': god_mode, 'godmode': god_mode}).
                values_to_keys: dict[str, list[str]] = {}
                for key_node, value_node in zip(node.keys, node.values):
                    if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                        continue
                    if not isinstance(value_node, (ast.Name, ast.Attribute)):
                        continue
                    value_key = ast.dump(value_node, include_attributes=False)
                    values_to_keys.setdefault(value_key, []).append(key_node.value)
                for aliases in values_to_keys.values():
                    union(aliases)

    raw_groups: dict[str, list[str]] = {}
    for name in registered:
        raw_groups.setdefault(find(name), []).append(name)

    # Prefer a primary name appearing in Commands over one appearing only in
    # CommandAliases; preserve module/list order for predictable display text.
    primary_order: list[str] = []
    alias_order: list[str] = []
    for module in modules:
        primary_order.extend(
            _normalise_permission_command(item)
            for item in (getattr(module, "Commands", ()) or ())
        )
        alias_order.extend(
            _normalise_permission_command(item)
            for item in (getattr(module, "CommandAliases", ()) or ())
        )

    result: dict[str, tuple[str, ...]] = {}
    for members in raw_groups.values():
        unique = set(members)
        if len(unique) == 1:
            ordered = list(unique)
        else:
            primary = next((name for name in primary_order if name in unique), None)
            if primary is None:
                primary = next((name for name in alias_order if name in unique), None)
            if primary is None:
                primary = sorted(unique)[0]
            ordered = [primary] + sorted(unique - {primary})
        family = tuple(ordered)
        for name in unique:
            result[name] = family
    return result


def refresh_management_command_registry() -> None:
    """Clear cached command metadata after changing registrations at runtime.

    The command modules in a normal server deployment are static, so cached
    discovery avoids repeatedly importing/scanning modules and parsing their
    source for aliases. Call this hook after deliberately changing Commands,
    CommandAliases, or alias maps in a live process.
    """
    _command_permission_families.cache_clear()
    _registered_protected_command_names.cache_clear()
    _installed_command_modules.cache_clear()
    _LOG.info("Management command registry cache cleared.")


def _command_family(command_name: str) -> tuple[str, ...]:
    """Return the requested command plus every discovered alias of it."""
    name = _normalise_permission_command(command_name)
    if name == "all":
        return ("all",)
    return _command_permission_families().get(name, (name,))


def _reversed_permission_arguments(
    arguments: list[str], command: str, clientid: int
) -> bool:
    """Detect `/addcmd <command> <role>` and explain the expected argument order."""
    if len(arguments) != 2:
        return False
    possible_command = _normalise_permission_command(arguments[0])
    possible_role = _resolve_role_name(arguments[1])
    supplied_role = _resolve_role_name(arguments[0])
    if (
        possible_command in _registered_protected_command_names()
        and possible_role is not None
        and supplied_role is None
    ):
        _send_error(
            f"Arguments are reversed. Correct usage: /{command} <role> <command>. "
            f"Example: /{command} {possible_role} {possible_command}.",
            clientid,
        )
        return True
    return False


def _update_command_permission(role_name: str, command_name: str, add: bool) -> tuple[str, bool] | None:
    """Update one permission using either supported pdata.py function naming."""
    method_names = (
        ("add_command_to_role", "add_command_role")
        if add else
        ("remove_command_from_role", "remove_command_role")
    )
    method = None
    for name in method_names:
        candidate = getattr(pdata, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        _LOG.error("pdata.py exposes none of the compatible permission methods: %s", method_names)
        return None

    result = method(role_name, command_name)
    if isinstance(result, tuple) and len(result) >= 2:
        return str(result[0]), bool(result[1])
    if isinstance(result, str):
        status = result.casefold()
        if add:
            changed = "command added" in status or status.startswith("added command")
            failed = any(token in status for token in (
                "already exists", "already has", "role not exists", "role does not exist",
                "command not exists", "command not found", "command is empty",
            ))
        else:
            changed = "command removed" in status or "removed command" in status
            failed = any(token in status for token in (
                "role not exists", "role does not exist", "command not exists",
                "command not found", "not found in role",
            ))
        return role_name, bool(changed and not failed)

    # Some legacy implementations mutate/save but return None. Verify the saved
    # command list rather than treating a None return as an automatic failure.
    try:
        try:
            roles = pdata.get_roles(force_reload=True)
        except TypeError:
            roles = pdata.get_roles()
        commands = roles.get(role_name, {}).get("commands", [])
        stored = {_normalise_permission_command(item) for item in commands if isinstance(item, str)}
        exists = command_name in stored
        return role_name, (exists if add else not exists)
    except Exception:
        _LOG.exception("Could not verify legacy pdata permission update.")
        return None


def _can_edit_role_permissions(
    actual_role: str, command_name: str, clientid: int,
    actor_account_id: str | None,
) -> bool:
    """Protect the owner role, ALL grants, and the actor's own assigned role."""
    is_owner = _actor_is_owner(actor_account_id)
    if actual_role.casefold() == "owner" and not is_owner:
        _send_error("Only the owner can modify the owner role's permissions.", clientid)
        return False
    if command_name.casefold() == "all" and not is_owner:
        _send_error("Only the owner can grant or revoke ALL command permissions.", clientid)
        return False
    if not is_owner:
        try:
            actor_roles = pdata.get_player_roles(actor_account_id)
        except Exception:
            _LOG.exception("Could not inspect the command editor's assigned roles.")
            _send_error("Could not verify your roles; permission change cancelled.", clientid)
            return False
        if any(role.casefold() == actual_role.casefold() for role in actor_roles):
            _send_error(
                "You cannot change permissions for a role assigned to your own account.",
                clientid,
            )
            return False
    return True


def _addcommand(
    arguments: list[str], clientid: int, actor_account_id: str | None
) -> bool:
    if len(arguments) < 2:
        _send_error(
            "Usage: /addcmd <role> <command>. Example: /addcmd admin gm.",
            clientid,
        )
        return False

    role_name = " ".join(arguments[:-1]).strip()
    command_name = arguments[-1].strip().lstrip("/").casefold()
    if _reversed_permission_arguments(arguments, "addcmd", clientid):
        return False
    if not role_name:
        _send_error("Role name cannot be empty.", clientid)
        return False
    if command_name not in _registered_protected_command_names() and command_name != "all":
        _send_error(
            f"Unknown command/alias '{arguments[-1]}'. It is not registered by an installed command module.",
            clientid,
        )
        return False

    actual_role = _resolve_role_name(role_name)
    if actual_role is None:
        _send_error(f"Role '{role_name}' does not exist.", clientid)
        return False
    if not _can_edit_role_permissions(actual_role, command_name, clientid, actor_account_id):
        return False

    try:
        roles = pdata.get_roles(force_reload=True)
    except TypeError:
        roles = pdata.get_roles()
    role_data = roles.get(actual_role, {})
    current_commands = role_data.get("commands", []) if isinstance(role_data, dict) else []
    if isinstance(current_commands, (list, tuple)) and any(
        isinstance(item, str) and item.strip().casefold().lstrip("/") == "all"
        for item in current_commands
    ):
        if command_name == "all":
            _send_pretty(f"Role '{actual_role}' already has ALL command permissions.", clientid)
            return True
        _send_error(
            f"Role '{actual_role}' already has ALL permissions; individual grants are redundant.",
            clientid,
        )
        return False

    if command_name == "all":
        family = ("all",)
    else:
        family = _command_family(command_name)

    added_names: list[str] = []
    try:
        for family_name in family:
            update = _update_command_permission(actual_role, family_name, add=True)
            if update is None:
                _send_error("Could not grant permission; check pdata.py and the server log.", clientid)
                return False
            _stored_role, added = update
            if added:
                added_names.append(family_name)
    except Exception:
        _LOG.exception("Could not grant command family %s to role %s.", family, actual_role)
        _send_error("Could not save roles.json; check the server log.", clientid)
        return False

    if not added_names:
        label = ", ".join("/" + name for name in family)
        _send_pretty(f"Role '{actual_role}' already has permission for {label}.", clientid)
        return True
    primary_name = family[0]
    aliases = family[1:]
    if aliases:
        _send_pretty(
            f"Granted /{primary_name} and its aliases "
            f"({', '.join('/' + name for name in aliases)}) to role '{actual_role}'.",
            clientid,
        )
    else:
        _send_pretty(f"Granted /{primary_name} permission to role '{actual_role}'.", clientid)
    return True


def _removecommand(
    arguments: list[str], clientid: int, actor_account_id: str | None
) -> bool:
    if len(arguments) < 2:
        _send_error(
            "Usage: /removecmd <role> <command>. Example: /removecmd admin gm.",
            clientid,
        )
        return False

    role_name = " ".join(arguments[:-1]).strip()
    command_name = _normalise_permission_command(arguments[-1])
    if _reversed_permission_arguments(arguments, "removecmd", clientid):
        return False
    if not role_name:
        _send_error("Role name cannot be empty. Example: /removecmd admin gm.", clientid)
        return False
    if command_name not in _registered_protected_command_names() and command_name != "all":
        _send_error(
            f"Unknown command/alias '{arguments[-1]}'. Use /cmdlist to check registered names, "
            "then use /removecmd <role> <command>.",
            clientid,
        )
        return False

    actual_role = _resolve_role_name(role_name)
    if actual_role is None:
        _send_error(
            f"Role '{role_name}' does not exist. Syntax is /removecmd <role> <command>; "
            "for example: /removecmd admin gm.",
            clientid,
        )
        return False
    if not _can_edit_role_permissions(actual_role, command_name, clientid, actor_account_id):
        return False
    if actual_role.casefold() == "owner" and command_name == "all":
        _send_error("The owner's ALL permission cannot be removed through chat commands.", clientid)
        return False

    try:
        try:
            roles = pdata.get_roles(force_reload=True)
        except TypeError:
            roles = pdata.get_roles()
        role_data = roles.get(actual_role, {})
        current_commands = role_data.get("commands", []) if isinstance(role_data, dict) else []
    except Exception:
        _LOG.exception("Could not inspect permissions for role %s.", actual_role)
        _send_error("Could not read roles.json; command removal was cancelled.", clientid)
        return False

    if isinstance(current_commands, (list, tuple)) and any(
        isinstance(item, str) and item.strip().casefold().lstrip("/") == "all"
        for item in current_commands
    ) and command_name != "all":
        _send_error(
            f"Role '{actual_role}' has ALL permissions. Remove ALL first before revoking "
            "a specific command or alias.",
            clientid,
        )
        return False

    family = _command_family(command_name)
    removed_names: list[str] = []
    errors: list[str] = []
    try:
        for family_name in family:
            update = _update_command_permission(actual_role, family_name, add=False)
            if update is None:
                errors.append(family_name)
                continue
            _stored_role, removed = update
            if removed:
                removed_names.append(family_name)
    except Exception:
        _LOG.exception("Could not revoke command family %s from role %s.", family, actual_role)
        _send_error("Could not update roles.json; check the server log.", clientid)
        return False

    if errors:
        _LOG.error("Permission removals failed for role %s: %s", actual_role, errors)
        _send_error(
            "Could not remove every alias permission. Check pdata.py and the server log; "
            f"the command family was: {', '.join('/' + name for name in family)}.",
            clientid,
        )
        return False
    if not removed_names:
        label = ", ".join("/" + name for name in family)
        _send_pretty(
            f"Role '{actual_role}' has no explicit permission for {label}; nothing was removed.",
            clientid,
        )
        return False

    primary_name = family[0]
    aliases = family[1:]
    if aliases:
        _send_pretty(
            f"Revoked /{primary_name} and its aliases "
            f"({', '.join('/' + name for name in aliases)}) from role '{actual_role}'.",
            clientid,
        )
    else:
        _send_pretty(f"Revoked /{primary_name} permission from role '{actual_role}'.", clientid)
    return True


def _target_for_moderation(target_id: int, actor_client_id: int) -> str | None:
    """Resolve target identity and enforce owner protection for kick/ban."""
    target_account_id = _target_account_id(target_id)
    if not target_account_id:
        _send_error(
            "Cannot verify target's V2 account ID; refusing this moderation action.",
            actor_client_id,
        )
        return None
    try:
        if pdata.is_owner_account_id(target_account_id):
            _send_error("The owner account is protected from kick and ban.", actor_client_id)
            return None
    except Exception:
        _LOG.exception("Could not verify owner protection for target %s.", target_id)
        _send_error("Could not verify target permissions; action cancelled.", actor_client_id)
        return None
    return target_account_id


def _kick(arguments: list[str], clientid: int) -> bool:
    if len(arguments) != 1:
        _send_error("Usage: /kick <clientID>", clientid)
        return False

    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False
    if _target_for_moderation(target_id, clientid) is None:
        return False

    try:
        bs.disconnect_client(target_id)
    except Exception:
        _LOG.exception("Could not disconnect client %s.", target_id)
        _send_error("Kick failed; check the server log.", clientid)
        return False

    _send_pretty(f"Kicked client {target_id}.", clientid)
    return True


def _ban(arguments: list[str], clientid: int, actor_account_id: str | None) -> bool:
    if not arguments:
        _send_error(
            "Usage: /ban <clientID> [reason] OR /ban <clientID> <hours> [reason]",
            clientid,
        )
        return False

    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False

    target_account_id = _target_for_moderation(target_id, clientid)
    if target_account_id is None:
        return False

    duration_hours: int | None = None
    reason_parts: list[str] = []
    if len(arguments) >= 2:
        duration_token = arguments[1]
        if re.fullmatch(r"[+-]?\d+", duration_token):
            try:
                parsed_hours = int(duration_token)
            except (ValueError, OverflowError):
                _send_error("Ban hours must be a positive integer starting from 1.", clientid)
                return False
            if parsed_hours < 1:
                _send_error("Ban hours must be a positive integer starting from 1.", clientid)
                return False
            duration_hours = parsed_hours
            reason_parts = arguments[2:]
        elif re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", duration_token):
            _send_error("Ban hours must be an integer; decimals are not accepted.", clientid)
            return False
        else:
            # If the second token is not numeric, all remaining text is a
            # permanent-ban reason, as requested.
            reason_parts = arguments[1:]

    reason = " ".join(reason_parts).strip()[:256] or "No reason provided"
    try:
        saved = pdata.ban_player(
            target_account_id,
            reason=reason,
            banned_by=actor_account_id,
            duration_hours=duration_hours,
            client_id_at_ban=target_id,
        )
    except Exception:
        _LOG.exception("Could not save ban for account %s.", target_account_id)
        _send_error("Could not save banned.json; ban was not completed.", clientid)
        return False

    if not saved:
        _send_error("Ban could not be saved; target may be protected or ID invalid.", clientid)
        return False

    disconnect_ok = True
    try:
        bs.disconnect_client(target_id)
    except Exception:
        disconnect_ok = False
        _LOG.exception(
            "Ban saved for %s but immediate disconnect failed; roster enforcement will retry.",
            target_account_id,
        )

    if duration_hours is None:
        message = f"Permanent ban saved for client {target_id}. Reason: {reason}"
    else:
        message = f"Banned client {target_id} for {duration_hours} hour(s). Reason: {reason}"
    if not disconnect_ok:
        message += " Disconnect will be retried by the ban monitor."
    _send_pretty(message, clientid)
    return True


def _client_id_is_connected(target_id: int) -> bool | None:
    """Return whether a client ID is currently in the game roster."""
    try:
        for entry in bs.get_game_roster():
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("client_id")) == target_id:
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        return False
    except Exception:
        _LOG.exception("Could not inspect the game roster while resolving /unban.")
        return None


def _unban(arguments: list[str], clientid: int) -> bool:
    if len(arguments) != 1:
        _send_error("Usage: /unban <clientID or V2_accountID>", clientid)
        return False

    value = arguments[0].strip()
    target_client_id: int | None = None
    account_id: str | None = None

    if is_v2_account_id(value):
        account_id = value
    else:
        target_client_id = _parse_client_id(value)
        if target_client_id is None:
            _send_error(
                "Use /unban <clientID> or /unban <V2_accountID beginning with a->.",
                clientid,
            )
            return False

        connected = _client_id_is_connected(target_client_id)
        if connected is None:
            _send_error("Could not inspect connected players; unban cancelled.", clientid)
            return False

        if connected:
            # Prefer the current occupant of a reused client ID. Never accidentally
            # unban a historical account just because it once used this client ID.
            current_account_id = _target_account_id(target_client_id)
            if not current_account_id:
                _send_error(
                    "That client is connected, but its V2 account ID cannot be verified.",
                    clientid,
                )
                return False
            try:
                active = pdata.get_active_ban(current_account_id)
            except Exception:
                _LOG.exception("Could not check active ban for %s.", current_account_id)
                _send_error("Could not read banned.json; unban cancelled.", clientid)
                return False
            if active is None:
                _send_error(
                    f"Connected client {target_client_id} has no active ban. "
                    "Use the V2 account ID for a previously disconnected player.",
                    clientid,
                )
                return False
            account_id = current_account_id
        else:
            try:
                matches = pdata.find_active_bans_by_client_id(target_client_id)
            except Exception:
                _LOG.exception(
                    "Could not find a ban for previously disconnected client %s.",
                    target_client_id,
                )
                _send_error("Could not read banned.json; unban cancelled.", clientid)
                return False

            if not matches:
                _send_error(
                    f"No active ban record found for client ID {target_client_id}. "
                    "Use the player's V2 account ID if the ban was created before this update.",
                    clientid,
                )
                return False
            if len(matches) > 1:
                _send_error(
                    f"Client ID {target_client_id} matches multiple active bans. "
                    "Use the exact V2 account ID to avoid unbanning the wrong player.",
                    clientid,
                )
                return False
            account_id = matches[0][0]

    try:
        removed = pdata.unban_player(account_id)
    except Exception:
        _LOG.exception("Could not remove ban for account %s.", account_id)
        _send_error("Could not update banned.json; check the server log.", clientid)
        return False

    if not removed:
        _send_error(f"No saved ban was found for {account_id}.", clientid)
        return False

    if target_client_id is not None:
        _send_pretty(
            f"Ban removed for client {target_client_id} (V2 account {account_id}).",
            clientid,
        )
    else:
        _send_pretty(f"Ban removed for {account_id}.", clientid)
    return True



def _target_for_staff_mute(target_id: int, actor_client_id: int) -> str | None:
    """Resolve a connected target and preserve owner protection for staff mutes."""
    target_account_id = _target_account_id(target_id)
    if not target_account_id:
        _send_error(
            "Target must be connected with a valid V2 account ID; mute cancelled.",
            actor_client_id,
        )
        return None
    try:
        if pdata.is_owner_account_id(target_account_id):
            _send_error("The owner account is protected from staff mutes.", actor_client_id)
            return None
    except Exception:
        _LOG.exception("Could not verify owner protection for mute target %s.", target_id)
        _send_error("Could not verify target permissions; mute cancelled.", actor_client_id)
        return None
    return target_account_id


def _mute(arguments: list[str], clientid: int, actor_account_id: str | None) -> bool:
    """Staff-mute a connected player indefinitely or for a number of hours."""
    if not arguments:
        _send_error("Usage: /mute <clientID> [hours] [reason]", clientid)
        return False

    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False

    target_account_id = _target_for_staff_mute(target_id, clientid)
    if target_account_id is None:
        return False

    duration_hours: int | None = None
    reason_parts: list[str] = []
    if len(arguments) >= 2:
        duration_token = arguments[1]
        if re.fullmatch(r"[+-]?\d+", duration_token):
            try:
                parsed_hours = int(duration_token)
            except (ValueError, OverflowError):
                _send_error("Mute hours must be a positive integer starting from 1.", clientid)
                return False
            if parsed_hours < 1:
                _send_error("Mute hours must be a positive integer starting from 1.", clientid)
                return False
            duration_hours = parsed_hours
            reason_parts = arguments[2:]
        elif re.fullmatch(r"[+-]?(?:\d+\.\d*|\.\d+)", duration_token):
            _send_error("Mute hours must be an integer; decimals are not accepted.", clientid)
            return False
        else:
            # Non-numeric text after the client ID is an indefinite-mute reason.
            reason_parts = arguments[1:]

    reason = " ".join(reason_parts).strip()[:256] or "No reason provided"
    try:
        saved = pdata.mute_player(
            target_account_id,
            reason=reason,
            muted_by=actor_account_id,
            duration_hours=duration_hours,
            client_id_at_mute=target_id,
        )
    except Exception:
        _LOG.exception("Could not save staff mute for V2 account %s.", target_account_id)
        _send_error("Could not save muted.json; mute was not completed.", clientid)
        return False

    if not saved:
        _send_error("Mute could not be saved; target may be protected or ID invalid.", clientid)
        return False

    if duration_hours is None:
        response = f"Muted client {target_id} until manually unmuted. Reason: {reason}"
    else:
        response = f"Muted client {target_id} for {duration_hours} hour(s). Reason: {reason}"
    _send_pretty(response, clientid)
    return True


def _clear_transient_spam_mute(account_id: str | None, client_id: int | None = None) -> bool:
    """Clear checkspam's separate in-memory mute without discarding its history."""
    try:
        from chathandle.chatspam import checkspam

        return bool(checkspam.clear_mute(account_id, client_id))
    except Exception:
        _LOG.exception("Could not clear transient anti-spam mute for %s.", account_id)
        return False


def _unmute(arguments: list[str], clientid: int) -> bool:
    """Remove a staff mute by live client ID or V2 account ID."""
    if len(arguments) != 1:
        _send_error("Usage: /unmute <clientID or V2_accountID>", clientid)
        return False

    value = arguments[0].strip()
    target_client_id: int | None = None
    account_id: str | None = None

    if is_v2_account_id(value):
        account_id = value
    else:
        target_client_id = _parse_client_id(value)
        if target_client_id is None:
            _send_error("Use /unmute <clientID> or /unmute <V2_accountID beginning with a->.", clientid)
            return False

        connected = _client_id_is_connected(target_client_id)
        if connected is None:
            _send_error("Could not inspect connected players; unmute cancelled.", clientid)
            return False
        if connected:
            account_id = _target_account_id(target_client_id)
            if not account_id:
                _send_error("Connected player has no verifiable V2 account ID.", clientid)
                return False
        else:
            try:
                matches = pdata.find_active_mutes_by_client_id(target_client_id)
            except Exception:
                _LOG.exception("Could not find mute by client ID %s.", target_client_id)
                _send_error("Could not read muted.json; unmute cancelled.", clientid)
                return False
            if not matches:
                _send_error(f"No active staff mute found for client ID {target_client_id}.", clientid)
                return False
            if len(matches) > 1:
                _send_error(
                    f"Client ID {target_client_id} matches multiple active mutes. Use the V2 account ID.",
                    clientid,
                )
                return False
            account_id = matches[0][0]

    try:
        active = pdata.get_active_mute(account_id)
    except Exception:
        _LOG.exception("Could not check mute for V2 account %s.", account_id)
        _send_error("Could not read muted.json; unmute cancelled.", clientid)
        return False

    if active is not None:
        try:
            removed = pdata.unmute_player(account_id)
        except Exception:
            _LOG.exception("Could not remove staff mute for %s.", account_id)
            _send_error("Could not update muted.json; check the server log.", clientid)
            return False
        if not removed:
            _send_error("Mute record changed while unmuting; please retry.", clientid)
            return False
        _clear_transient_spam_mute(account_id, target_client_id)
        label = f"client {target_client_id}" if target_client_id is not None else account_id
        _send_pretty(f"Staff mute removed for {label}.", clientid)
        return True

    # `/unmute` also lifts the separate short anti-spam mute, if one is active.
    if _clear_transient_spam_mute(account_id, target_client_id):
        label = f"client {target_client_id}" if target_client_id is not None else account_id
        _send_pretty(f"Temporary anti-spam mute cleared for {label}.", clientid)
        return True

    _send_error(f"No active mute found for {account_id}.", clientid)
    return False


def _mutechat(arguments: list[str], clientid: int) -> bool:
    """Mute ordinary public chat for the current server session."""
    if arguments:
        _send_error("Usage: /mutechat", clientid)
        return False
    try:
        from chathandle.chatfilter import chatfilter

        chatfilter.set_global_chat_muted(True)
    except Exception:
        _LOG.exception("Could not enable global chat mute.")
        _send_error("Could not enable global chat mute; check the server log.", clientid)
        return False
    _send_pretty("Global chat muted. Authorized slash commands remain available.", clientid)
    return True


def _unmutechat(arguments: list[str], clientid: int) -> bool:
    """Restore ordinary public chat for the current server session."""
    if arguments:
        _send_error("Usage: /unmutechat", clientid)
        return False
    try:
        from chathandle.chatfilter import chatfilter

        chatfilter.set_global_chat_muted(False)
    except Exception:
        _LOG.exception("Could not disable global chat mute.")
        _send_error("Could not disable global chat mute; check the server log.", clientid)
        return False
    _send_pretty("Global chat unmuted.", clientid)
    return True


def _foreground_activity(clientid: int):
    """Return the foreground host activity or privately report why unavailable."""
    try:
        activity = bs.get_foreground_host_activity()
    except Exception:
        _LOG.exception("Could not get the foreground host activity.")
        _send_error("Could not inspect the current game; check the server log.", clientid)
        return None
    if activity is None:
        _send_error("There is no active game to control right now.", clientid)
        return None
    return activity


def _no_arguments(arguments: list[str], clientid: int, usage: str) -> bool:
    if arguments:
        _send_error(f"Usage: {usage}", clientid)
        return False
    return True


def _end_game(arguments: list[str], clientid: int) -> bool:
    if not _no_arguments(arguments, clientid, "/end (alias: /next)"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            activity.end_game()
    except Exception:
        _LOG.exception("Could not end the current game.")
        _send_error("Could not end the current game; check the server log.", clientid)
        return False
    _send_pretty("Ending the current game.", clientid)
    return True


def _pause_game(arguments: list[str], clientid: int) -> bool:
    if not _no_arguments(arguments, clientid, "/pause (alias: /pausegame)"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            node = activity.globalsnode
            node.paused = not bool(node.paused)
            state = "paused" if node.paused else "unpaused"
    except Exception:
        _LOG.exception("Could not toggle game pause state.")
        _send_error("Could not toggle game pause; check the server log.", clientid)
        return False
    _send_pretty(f"Game {state}.", clientid)
    return True



def _resume_game(arguments: list[str], clientid: int) -> bool:
    """Resume the active game without toggling it back to paused."""
    if not _no_arguments(arguments, clientid, "/resume"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            activity.globalsnode.paused = False
    except Exception:
        _LOG.exception("Could not resume the game.")
        _send_error("Could not resume the game; check the server log.", clientid)
        return False
    _send_pretty("Game resumed.", clientid)
    return True

def _slow_motion(arguments: list[str], clientid: int) -> bool:
    if not _no_arguments(arguments, clientid, "/slowmo (aliases: /sm, /slow)"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            node = activity.globalsnode
            node.slow_motion = not bool(node.slow_motion)
            state = "enabled" if node.slow_motion else "disabled"
    except Exception:
        _LOG.exception("Could not toggle slow motion.")
        _send_error("Could not toggle slow motion; check the server log.", clientid)
        return False
    _send_pretty(f"Slow motion {state}.", clientid)
    return True


def _night_vision(arguments: list[str], clientid: int) -> bool:
    """Set night vision on; use /day or /dv to return to day vision."""
    if not _no_arguments(arguments, clientid, "/nv (alias: /night)"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            activity.globalsnode.tint = (0.5, 0.5, 1.0)
            activity.globalsnode.ambient_color = (1.5, 1.5, 1.5)
    except Exception:
        _LOG.exception("Could not enable night-vision tint.")
        _send_error("Could not enable night vision; check the server log.", clientid)
        return False
    _send_pretty("Night-vision mode enabled. Use /day or /dv to return to day vision.", clientid)
    return True


def _day_mode(arguments: list[str], clientid: int) -> bool:
    if not _no_arguments(arguments, clientid, "/day (alias: /dv)"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            activity.globalsnode.tint = (1.0, 1.0, 1.0)
            activity.globalsnode.ambient_color = (1.0, 1.0, 1.0)
    except Exception:
        _LOG.exception("Could not set day-mode tint.")
        _send_error("Could not set day mode; check the server log.", clientid)
        return False
    _send_pretty("Day mode enabled.", clientid)
    return True


def _tint(arguments: list[str], clientid: int) -> bool:
    if len(arguments) != 3:
        _send_error("Usage: /tint <r> <g> <b> (each channel 0.0-1.0)", clientid)
        return False
    try:
        rgb = tuple(float(value) for value in arguments)
    except (TypeError, ValueError, OverflowError):
        _send_error("Tint values must be numbers between 0.0 and 1.0.", clientid)
        return False
    if len(rgb) != 3 or not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in rgb):
        _send_error("Tint values must be finite numbers between 0.0 and 1.0.", clientid)
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            activity.globalsnode.tint = rgb
    except Exception:
        _LOG.exception("Could not apply requested tint %r.", rgb)
        _send_error("Could not apply tint; check the server log.", clientid)
        return False
    _send_pretty(f"Scene tint set to ({rgb[0]:g}, {rgb[1]:g}, {rgb[2]:g}).", clientid)
    return True


def _camera_mode(arguments: list[str], clientid: int) -> bool:
    if not _no_arguments(arguments, clientid, "/cameramode"):
        return False
    activity = _foreground_activity(clientid)
    if activity is None:
        return False
    try:
        with activity.context:
            node = activity.globalsnode
            current_mode = str(node.camera_mode).casefold()
            # API 9 supports "rotate" and "follow"; "normal" is invalid.
            new_mode = "follow" if current_mode == "rotate" else "rotate"
            node.camera_mode = new_mode
    except Exception:
        _LOG.exception("Could not toggle camera mode.")
        _send_error("Could not change camera mode; check the server log.", clientid)
        return False
    _send_pretty(f"Camera mode set to {new_mode}.", clientid)
    return True


def _maxplayers(arguments: list[str], clientid: int) -> bool:
    if len(arguments) != 1 or not re.fullmatch(r"\d+", arguments[0]):
        _send_error("Usage: /maxplayers <number> (1-64; alias: /max)", clientid)
        return False
    size = int(arguments[0])
    if not 1 <= size <= 64:
        _send_error("Maximum players must be between 1 and 64.", clientid)
        return False
    try:
        bs.set_public_party_max_size(size)
        session = bs.get_foreground_host_session()
        if session is not None:
            try:
                session.max_players = size
            except (AttributeError, TypeError, ValueError):
                # Some API builds expose max size only through the public-party setting.
                _LOG.debug("Current session did not accept max_players assignment.", exc_info=True)
    except Exception:
        _LOG.exception("Could not set maximum player count to %s.", size)
        _send_error("Could not set maximum players; check the server log.", clientid)
        return False
    _send_pretty(f"Maximum players set to {size}.", clientid)
    return True


def _playlist(arguments: list[str], clientid: int) -> bool:
    # Playlist code/name changes are session-only; config.json is preserved.
    return playlist_manager.change_playlist(arguments, client_id=clientid)

def _announce_server_restart() -> None:
    """Broadcast a decorative restart notice before disconnecting clients."""
    heading = "\ue048 \ue043 SERVER RESTART \ue043 \ue048"
    detail = "\ue049 Server is restarting. Please rejoin in a moment! \ue049"

    # Public chat lines remain visible in chat history; the transient banner is
    # an additional visual notice. Failures in one channel must not block restart.
    for line in (heading, detail):
        try:
            bs.chatmessage(line)
        except Exception:
            _LOG.exception("Could not send public restart notice to chat.")

    try:
        bs.broadcastmessage(
            "\ue048 SERVER RESTART \ue048  |  Please rejoin in a moment!",
            color=(0.30, 0.78, 1.0),
            transient=True,
        )
    except Exception:
        _LOG.exception("Could not show transient restart banner.")


def _disconnect_all_clients_for_restart() -> None:
    """Disconnect every currently connected player before clean process exit."""
    client_ids: set[int] = set()

    # Roster is the primary source of connected network clients.
    try:
        for entry in bs.get_game_roster() or ():
            if not isinstance(entry, dict):
                continue
            try:
                client_id = int(entry.get("client_id", -1))
            except (TypeError, ValueError, OverflowError):
                continue
            if client_id >= 0:
                client_ids.add(client_id)
    except Exception:
        _LOG.exception("Could not enumerate the game roster for restart disconnect.")

    # Include session players too in case a build omits an entry from the roster.
    try:
        session = bs.get_foreground_host_session()
        if session is not None:
            for player in list(getattr(session, "sessionplayers", ())):
                try:
                    client_id = int(player.inputdevice.client_id)
                except Exception:
                    continue
                if client_id >= 0:
                    client_ids.add(client_id)
    except Exception:
        _LOG.exception("Could not enumerate session players for restart disconnect.")

    for client_id in sorted(client_ids):
        try:
            bs.disconnect_client(client_id)
            _LOG.info("Disconnected client %s for scheduled server restart.", client_id)
        except Exception:
            _LOG.exception("Could not disconnect client %s before restart.", client_id)

    # Give disconnect callbacks and socket cleanup time to complete before
    # asking the headless process to exit. The parent launcher can then start
    # a fresh child without racing the old process for the UDP port.
    try:
        babase.apptimer(
            _RESTART_DISCONNECT_GRACE_SECONDS,
            _quit_cleanly_for_supervisor_restart,
        )
    except Exception:
        _LOG.exception("Could not schedule clean server shutdown after disconnect.")
        _quit_cleanly_for_supervisor_restart()


def _quit_cleanly_for_supervisor_restart() -> None:
    """Exit cleanly so the external server supervisor can relaunch the server."""
    _LOG.warning(
        "All available player disconnects have been requested; exiting cleanly "
        "for the server supervisor to relaunch BombSquad."
    )
    try:
        # Do not os.execv() the headless binary in place: that can rebind the UDP
        # port before the previous network reader/socket has fully shut down.
        # The user's launcher already supervises this process and restarts it.
        babase.quit()
    except Exception:
        _LOG.exception("Clean BombSquad shutdown request failed.")


def _restart_server(arguments: list[str], clientid: int, actor_account_id: str | None) -> bool:
    """Notify all players, disconnect them, then exit for supervisor relaunch."""
    global _RESTART_PENDING

    if not _no_arguments(arguments, clientid, "/restart"):
        return False
    if not _actor_is_owner(actor_account_id):
        _send_error("Only the owner can restart the server.", clientid)
        return False
    if _RESTART_PENDING:
        _send_pretty("Server restart is already in progress.", clientid)
        return True

    _RESTART_PENDING = True
    _LOG.warning("Owner requested a graceful BombSquad server restart.")
    _announce_server_restart()

    try:
        # Leave enough time for clients to see the message before disconnects.
        babase.apptimer(
            _RESTART_NOTICE_SECONDS,
            _disconnect_all_clients_for_restart,
        )
    except Exception:
        _RESTART_PENDING = False
        _LOG.exception("Could not schedule graceful server restart.")
        _send_error("Could not schedule server restart; check the server log.", clientid)
        return False

    _send_pretty("Restart notice sent; players will be disconnected before shutdown.", clientid)
    return True


def _party(arguments: list[str], clientid: int) -> bool:
    """Set party visibility without modifying config.json."""
    if len(arguments) != 1 or arguments[0].casefold() not in ("public", "private"):
        _send_error("Usage: /party <public|private>", clientid)
        return False
    public = arguments[0].casefold() == "public"
    try:
        bs.set_public_party_enabled(public)
    except Exception:
        _LOG.exception("Could not set party visibility to %s.", arguments[0])
        _send_error("Could not change party visibility; check the server log.", clientid)
        return False
    _send_pretty(f"Party is now {'public' if public else 'private'}.", clientid)
    return True


def _remove_from_match(arguments: list[str], clientid: int) -> bool:
    """Remove session player(s) from the match without disconnecting clients."""
    if len(arguments) != 1:
        _send_error("Usage: /remove <clientID|all> (alias: /rm)", clientid)
        return False

    session = bs.get_foreground_host_session()
    if session is None:
        _send_error("There is no active host session.", clientid)
        return False

    session_players = list(getattr(session, "sessionplayers", ()))
    target_token = arguments[0].casefold()
    if target_token == "all":
        removed = 0
        failures = 0
        # Snapshot first because remove_from_game can alter session state.
        for session_player in session_players:
            remover = getattr(session_player, "remove_from_game", None)
            if not callable(remover):
                failures += 1
                continue
            try:
                remover()
                removed += 1
            except Exception:
                failures += 1
                _LOG.exception("Could not remove a session player from the match.")
        if removed == 0:
            _send_error("No players could be removed from the match.", clientid)
            return False
        message = f"Removed {removed} player(s) from the match; server connections remain active."
        if failures:
            message += f" {failures} player(s) could not be removed; see server log."
        _send_pretty(message, clientid)
        return True

    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Usage: /remove <non-negative clientID|all> (alias: /rm)", clientid)
        return False

    target_player = None
    for session_player in session_players:
        try:
            if int(session_player.inputdevice.client_id) == target_id:
                target_player = session_player
                break
        except Exception:
            continue
    if target_player is None:
        _send_error(f"Client {target_id} is not in the current host session.", clientid)
        return False

    remover = getattr(target_player, "remove_from_game", None)
    if not callable(remover):
        _send_error("This player cannot be removed from the match by the current API.", clientid)
        return False
    try:
        remover()
    except Exception:
        _LOG.exception("Could not remove client %s from the match.", target_id)
        _send_error("Could not remove the player from the match; check the server log.", clientid)
        return False

    _send_pretty(
        f"Client {target_id} was removed from the match but remains connected to the server.",
        clientid,
    )
    return True


def _find_session_player(client_id: int) -> Any | None:
    """Return the connected SessionPlayer for an exact client ID."""
    try:
        session = bs.get_foreground_host_session()
        if session is None:
            return None
        for session_player in getattr(session, "sessionplayers", ()):
            try:
                if int(session_player.inputdevice.client_id) == client_id:
                    return session_player
            except Exception:
                continue
    except Exception:
        _LOG.exception("Could not inspect session while resolving client %s.", client_id)
    return None


def _roster_entry(client_id: int) -> dict[str, Any] | None:
    """Return the engine roster entry for a connected client, if present."""
    try:
        for entry in bs.get_game_roster():
            if not isinstance(entry, dict):
                continue
            try:
                if int(entry.get("client_id", -1)) == client_id:
                    return entry
            except (TypeError, ValueError, OverflowError):
                continue
    except Exception:
        _LOG.exception("Could not read game roster while resolving client %s.", client_id)
    return None


def _display_name(value: Any, fallback: str) -> str:
    """Normalize a roster display-name field into a single safe line."""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(part) for part in value if part is not None)
    if not isinstance(value, str) or not value.strip():
        return fallback
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:80] or fallback


def _info(arguments: list[str], clientid: int) -> bool:
    """Show only the target V2 account tag and account creation date/time."""
    if len(arguments) != 1:
        _send_error("Usage: /info <clientID>", clientid)
        return False
    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False

    roster = _roster_entry(target_id)
    if roster is None or _find_session_player(target_id) is None:
        _send_error(f"Client {target_id} is not currently connected.", clientid)
        return False

    account_id = _target_account_id(target_id)
    if not account_id:
        _send_pretty_chat(
            "Account info unavailable: this player has no verified V2 account ID.",
            clientid,
        )
        return True

    try:
        from playersdata import account_info
        info = account_info.get_account_info(account_id)
    except Exception:
        _LOG.exception("Could not read account details for /info client %s.", target_id)
        _send_error("Account details could not be read; check the server log.", clientid)
        return False

    if not info or not info.get("tag") or not info.get("created_at"):
        _send_pretty_chat(
            "Account details not captured yet. Have this player reconnect once after the update.",
            clientid,
        )
        return True

    account_tag = " ".join(
        str(info["tag"]).replace("\r", " ").replace("\n", " ").split()
    )[:64]
    created_text = account_info.format_created_time(info.get("created_at"))
    _send_pretty_chat(
        f"Account: {account_tag}\nAccount created on: {created_text}",
        clientid,
    )
    return True

def _gp(arguments: list[str], clientid: int) -> bool:
    """Display profile names exposed by the target's current API 9 input device."""
    if len(arguments) != 1:
        _send_error("Usage: /gp <clientID>", clientid)
        return False
    target_id = _parse_client_id(arguments[0])
    if target_id is None:
        _send_error("Client ID must be a non-negative integer.", clientid)
        return False

    session_player = _find_session_player(target_id)
    if session_player is None:
        _send_error(f"Client {target_id} is not currently in the host session.", clientid)
        return False

    input_device = getattr(session_player, "inputdevice", None)
    getter = getattr(input_device, "get_player_profiles", None)
    if not callable(getter):
        _send_error("This API 9 input device does not expose player profiles.", clientid)
        return False
    try:
        profiles = getter()
        if profiles is None:
            profiles = []
        elif isinstance(profiles, dict):
            profiles = list(profiles.keys())
        else:
            profiles = list(profiles)
    except Exception:
        _LOG.exception("Could not get profiles for client %s.", target_id)
        _send_error("Could not read player profiles; check the server log.", clientid)
        return False

    if not profiles:
        _send_pretty_chat(f"No player profiles were exposed for client {target_id}.", clientid)
        return True

    lines = [f"PLAYER PROFILES - CLIENT {target_id}"]
    for index, profile in enumerate(profiles[:30], 1):
        if isinstance(profile, dict):
            label = profile.get("name", profile.get("character", profile))
        else:
            label = profile
        text = " ".join(str(label).replace("\r", " ").replace("\n", " ").split())
        lines.append(f"{index}) {text[:120]}")
    if len(profiles) > 30:
        lines.append(f"...and {len(profiles) - 30} more")
    _send_pretty_chat("\n".join(lines), clientid)
    return True


def _lm(arguments: list[str], clientid: int) -> bool:
    """Display the last 15 allowed public chat messages privately."""
    if arguments:
        _send_error("Usage: /lm", clientid)
        return False
    try:
        from chathandle.chat_history import get_recent_messages
        messages = get_recent_messages(limit=15)
    except Exception:
        _LOG.exception("Could not read recent chat history.")
        _send_error("Chat history is unavailable; check the server log.", clientid)
        return False

    if not messages:
        _send_pretty_chat("No recorded public chat messages yet. Send a new public message after the updated chat filter is installed and the server is restarted.", clientid)
        return True

    lines = ["RECENT CHAT (oldest to newest):"]
    for item in messages:
        try:
            name = str(item.get("name", "Unknown"))[:48]
            sender_id = int(item.get("client_id", -1))
            message = str(item.get("message", ""))[:180]
            lines.append(f"{name} [{sender_id}]: {message}")
        except Exception:
            continue
    _send_pretty_chat("\n".join(lines), clientid)
    return True


def _recents(arguments: list[str], clientid: int) -> bool:
    """Display the last 15 V2-verified players observed by the roster monitor."""
    if arguments:
        _send_error("Usage: /recents", clientid)
        return False
    try:
        from playersdata.recent_players import get_recent_players
        players = get_recent_players(limit=15)
    except Exception:
        _LOG.exception("Could not read recent-player history.")
        _send_error("Recent-player history is unavailable; check the server log.", clientid)
        return False

    if not players:
        _send_pretty_chat("No verified recent-player records yet. The roster monitor records players as it observes them.", clientid)
        return True

    lines = ["RECENT PLAYERS (newest first):"]
    for index, item in enumerate(players, 1):
        name = _display_name(item.get("name"), "Unknown player")
        client_id = item.get("client_id", "?")
        account_id = str(item.get("account_id", ""))
        last_seen = str(item.get("last_seen", "unknown time"))
        lines.append(f"{index}. {name} | client {client_id} | {account_id} | {last_seen}")
    _send_pretty_chat("\n".join(lines), clientid)
    return True


def ExcelCommand(
    command: str,
    arguments: list[str],
    clientid: int,
    accountid: str | None,
) -> bool:
    """Execute implemented commands; return True only after successful action."""
    command = str(command).casefold()

    if command in ("quit", "restart"):
        return _restart_server(arguments, clientid, accountid)
    if command == "party":
        return _party(arguments, clientid)
    if command in ("remove", "rm"):
        return _remove_from_match(arguments, clientid)
    if command in ("end", "next"):
        return _end_game(arguments, clientid)
    if command in ("pause", "pausegame"):
        return _pause_game(arguments, clientid)
    if command == "resume":
        return _resume_game(arguments, clientid)
    if command in ("slowmo", "sm", "slow"):
        return _slow_motion(arguments, clientid)
    if command in ("nv", "night"):
        return _night_vision(arguments, clientid)
    if command in ("day", "dv"):
        return _day_mode(arguments, clientid)
    if command == "tint":
        return _tint(arguments, clientid)
    if command == "cameramode":
        return _camera_mode(arguments, clientid)
    if command in ("maxplayers", "max"):
        return _maxplayers(arguments, clientid)
    if command == "playlist":
        return _playlist(arguments, clientid)

    if command == "info":
        return _info(arguments, clientid)
    if command == "gp":
        return _gp(arguments, clientid)
    if command == "lm":
        return _lm(arguments, clientid)
    if command == "recents":
        return _recents(arguments, clientid)

    if command == "createrole":
        return _createrole(arguments, clientid, accountid)
    if command == "addrole":
        return _addrole(arguments, clientid, accountid)
    if command == "removerole":
        return _removerole(arguments, clientid, accountid)
    if command == "getroles":
        return _getroles(arguments, clientid)
    if command == "customtag":
        return _customtag(arguments, clientid)
    if command == "removetag":
        return _removetag(arguments, clientid)
    if command == "changetag":
        return _changetag(arguments, clientid)
    if command == "effects":
        return _effects(arguments, clientid)
    if command in ("customeffect", "effect"):
        return _customeffect(arguments, clientid)
    if command == "removeeffect":
        return _removeeffect(arguments, clientid)
    if command in ("addcommand", "addcmd"):
        return _addcommand(arguments, clientid, accountid)
    if command in ("removecommand", "removecmd"):
        return _removecommand(arguments, clientid, accountid)
    if command == "kick":
        return _kick(arguments, clientid)
    if command == "ban":
        return _ban(arguments, clientid, accountid)
    if command == "unban":
        return _unban(arguments, clientid)
    if command == "mute":
        return _mute(arguments, clientid, accountid)
    if command == "unmute":
        return _unmute(arguments, clientid)
    if command == "mutechat":
        return _mutechat(arguments, clientid)
    if command == "unmutechat":
        return _unmutechat(arguments, clientid)

    _send_pretty(
        f"/{command} is recognized but its action is not implemented yet.",
        clientid,
        color=(1.0, 0.7, 0.2),
    )
    return False
