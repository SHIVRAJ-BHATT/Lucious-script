"""V2-only role data access for the BombSquad 1.8.0/API 9 RoleTags mod."""
# Owner/Editor: Lucifer

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from tools.account_ids import get_v2_account_id, is_v2_account_id as _is_v2_id

_LOG = logging.getLogger("RoleTags")
_ROLES_FILE = os.path.join(os.path.dirname(__file__), "roles.json")

# Protected owner configured for this server.
_PROTECTED_OWNER_ACCOUNT_ID = 'a-a4226328'
_CUSTOM_TAGS_FILE = os.path.join(os.path.dirname(__file__), "customtag.json")
_EFFECTS_FILE = os.path.join(os.path.dirname(__file__), "effects.json")
_BANNED_FILE = os.path.join(os.path.dirname(__file__), "banned.json")
_MUTED_FILE = os.path.join(os.path.dirname(__file__), "muted.json")
_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}
_SIGNATURE: tuple[int, int] | None = None
_CUSTOM_TAGS_CACHE: dict[str, dict[str, str]] | None = None
_CUSTOM_TAGS_SIGNATURE: tuple[int, int] | None = None
_EFFECTS_CACHE: dict[str, list[str]] | None = None
_EFFECTS_SIGNATURE: tuple[int, int] | None = None
_EFFECTS_ERROR_SIGNATURE: tuple[int, int] | None = None
_BANS_CACHE: dict[str, dict[str, Any]] | None = None
_BANS_SIGNATURE: tuple[int, int] | None = None
_BANS_ERROR_SIGNATURE: tuple[int, int] | None = None
_MUTES_CACHE: dict[str, dict[str, Any]] | None = None
_MUTES_SIGNATURE: tuple[int, int] | None = None
_MUTES_ERROR_SIGNATURE: tuple[int, int] | None = None

# When a player has multiple roles, the first matching role in this list wins.
# Keep this priority explicit instead of depending on JSON dictionary ordering.
ROLE_PRIORITY = (
    "owner",
    "co-owner",
    "moderator",
    "leadstaff",
    "cs",
    "tcs",
    "admin",
    "vip",
)


# V2 account-ID helpers are imported from the reusable top-level account_ids.py module.

def _signature(path: str) -> tuple[int, int] | None:
    try:
        stat = os.stat(path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return None


def _validate_roles(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate expected keys while retaining tag and command metadata."""
    if not isinstance(raw, dict):
        raise ValueError("roles.json root must be a JSON object")

    result: dict[str, dict[str, Any]] = {}
    for role_name, value in raw.items():
        if not isinstance(role_name, str) or not isinstance(value, dict):
            _LOG.warning("Ignoring invalid role entry: %r", role_name)
            continue

        tag = value.get("tag", "")
        commands = value.get("commands", [])
        ids = value.get("ids", [])

        if not isinstance(tag, str):
            _LOG.warning("Role %s has a non-string tag; using an empty tag.", role_name)
            tag = ""
        if not isinstance(commands, list):
            commands = []
        commands = [command for command in commands if isinstance(command, str)]

        if not isinstance(ids, list):
            ids = []
        valid_ids = []
        for account_id in ids:
            if _is_v2_id(account_id):
                valid_ids.append(account_id)
            elif isinstance(account_id, str) and account_id.startswith("pb-"):
                _LOG.warning(
                    "Ignoring legacy V1 ID in role %s; V2 IDs beginning with 'a-' are required.",
                    role_name,
                )
        result[role_name] = {
            "tag": tag,
            "commands": commands,
            "ids": valid_ids,
        }
    return result


def get_roles(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Load role configuration; reload it automatically if roles.json changes."""
    global _CACHE, _SIGNATURE
    with _LOCK:
        current_signature = _signature(_ROLES_FILE)
        if not force_reload and _CACHE and current_signature == _SIGNATURE:
            return _CACHE

        try:
            with open(_ROLES_FILE, "r", encoding="utf-8") as file:
                raw = json.load(file)
            _CACHE = _validate_roles(raw)
            _SIGNATURE = current_signature
            _LOG.info("Loaded %d role definitions from %s.", len(_CACHE), _ROLES_FILE)
        except Exception:
            _LOG.exception("Could not load roles.json; role tags will be disabled.")
            _CACHE = {}
            _SIGNATURE = current_signature
        return _CACHE


def get_player_roles(account_id: str | None) -> list[str]:
    """Return configured roles plus the server's protected owner role."""
    if not _is_v2_id(account_id):
        return []

    roles = get_roles()
    assigned = [
        role_name
        for role_name, role_info in roles.items()
        if account_id in role_info.get("ids", [])
    ]

    if account_id == _PROTECTED_OWNER_ACCOUNT_ID:
        owner_key = next(
            (
                name for name in roles
                if isinstance(name, str) and name.casefold() == "owner"
            ),
            None,
        )

        owner_key = owner_key or "owner"
        if owner_key not in assigned:
            assigned.insert(0, owner_key)

    return assigned


def get_player_role(account_id: str | None) -> tuple[str, dict[str, Any]] | None:
    """Return the highest-priority role, including the protected owner."""
    if not _is_v2_id(account_id):
        return None

    roles = get_roles()

    if account_id == _PROTECTED_OWNER_ACCOUNT_ID:
        owner_key = next(
            (
                name for name in roles
                if isinstance(name, str) and name.casefold() == "owner"
            ),
            None,
        )

        if owner_key is not None and isinstance(roles.get(owner_key), dict):
            return owner_key, roles[owner_key]

        return "owner", {"commands": ["ALL"]}

    assigned = set(get_player_roles(account_id))

    for role_name in ROLE_PRIORITY:
        if role_name in assigned and role_name in roles:
            return role_name, roles[role_name]

    for role_name, role_info in roles.items():
        if role_name in assigned:
            return role_name, role_info

    return None


# ---------------------------------------------------------------------------
# Persistent role-management helpers.
# ---------------------------------------------------------------------------

def _normalise_role_name(role_name: Any) -> str | None:
    """Validate a human-provided role name without changing its casing."""
    if not isinstance(role_name, str):
        return None
    name = role_name.strip()
    if not name or len(name) > 32:
        return None
    if any(ord(char) < 32 for char in name):
        return None
    return name


def _read_roles_raw_locked() -> dict[str, Any]:
    """Read roles.json while the caller holds _LOCK."""
    with open(_ROLES_FILE, "r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ValueError("roles.json root must be a JSON object")
    return raw


def _find_role_key(raw: dict[str, Any], role_name: str) -> str | None:
    """Resolve a role key case-insensitively while preserving its stored casing."""
    folded = role_name.casefold()
    for key, value in raw.items():
        if (
            isinstance(key, str)
            and key.casefold() == folded
            and isinstance(value, dict)
        ):
            return key
    return None


def _save_roles_raw_locked(raw: dict[str, Any]) -> None:
    """Atomically persist roles and refresh the in-process cache/signature."""
    global _CACHE, _SIGNATURE
    _atomic_write_json(_ROLES_FILE, raw)
    _CACHE = _validate_roles(raw)
    _SIGNATURE = _signature(_ROLES_FILE)
    _LOG.info("Saved %d role definitions to %s.", len(_CACHE), _ROLES_FILE)


def create_role(role_name: str) -> str | None:
    """Create a role with an empty command list and no assigned players.

    Role-name matching is case-insensitive. Return the stored role key on
    success, or None if the name is invalid or a role already exists.
    """
    name = _normalise_role_name(role_name)
    if name is None:
        return None

    with _LOCK:
        raw = _read_roles_raw_locked()
        if _find_role_key(raw, name) is not None:
            return None
        raw[name] = {
            "tag": name,
            "tagcolor": [1.0, 1.0, 1.0],
            "commands": [],
            "ids": [],
        }
        _save_roles_raw_locked(raw)
    _LOG.info("Created role %s.", name)
    return name


def add_player_role(role_name: str, account_id: str | None) -> str | None:
    """Assign a role to a valid V2 account ID and persist it atomically.

    Return the stored role key on success (including an already-assigned role),
    or None if the role/account ID is invalid or the role does not exist.
    """
    name = _normalise_role_name(role_name)
    if name is None or not _is_v2_id(account_id):
        return None

    with _LOCK:
        raw = _read_roles_raw_locked()
        actual_key = _find_role_key(raw, name)
        if actual_key is None:
            return None
        role_data = raw[actual_key]
        ids = role_data.get("ids", [])
        if not isinstance(ids, list):
            ids = []
        if account_id in ids:
            return actual_key
        ids.append(account_id)
        role_data["ids"] = ids
        _save_roles_raw_locked(raw)

    _LOG.info("Assigned role %s to V2 account %s.", actual_key, account_id)
    return actual_key


def remove_player_role(role_name: str, account_id: str | None) -> str | None:
    """Remove only the requested role from a valid V2 account ID.

    Other roles and independent custom tags are left untouched. Return the
    stored role key on success, or None if the role/assignment is not present.
    """
    name = _normalise_role_name(role_name)
    if name is None or not _is_v2_id(account_id):
        return None

    with _LOCK:
        raw = _read_roles_raw_locked()
        actual_key = _find_role_key(raw, name)
        if actual_key is None:
            return None
        role_data = raw[actual_key]
        ids = role_data.get("ids", [])
        if not isinstance(ids, list) or account_id not in ids:
            return None
        role_data["ids"] = [value for value in ids if value != account_id]
        _save_roles_raw_locked(raw)

    _LOG.info("Removed role %s from V2 account %s.", actual_key, account_id)
    return actual_key

def _normalise_command_name(command_name: Any) -> str | None:
    """Normalize one command or alias token for permission storage."""
    if not isinstance(command_name, str):
        return None
    value = command_name.strip()
    if value.startswith("/"):
        value = value[1:]
    if not value or len(value) > 48:
        return None
    value = value.casefold()
    if value == "all":
        return "ALL"
    if not all(
        char.isascii() and (char.isalnum() or char in "_-")
        for char in value
    ):
        return None
    return value


def add_command_to_role(
    role_name: str, command_name: str
) -> tuple[str, bool] | None:
    """Add a command/alias permission to a role and atomically save roles.json.

    Returns (stored_role_name, added_new_entry), or None if the role or token
    is invalid/not found. Existing permissions and unrelated JSON metadata are
    preserved.
    """
    name = _normalise_role_name(role_name)
    command = _normalise_command_name(command_name)
    if name is None or command is None:
        return None

    with _LOCK:
        raw = _read_roles_raw_locked()
        actual_key = _find_role_key(raw, name)
        if actual_key is None:
            return None

        role_data = raw[actual_key]
        commands = role_data.get("commands", [])
        if not isinstance(commands, list):
            commands = []
        # Preserve existing entries but compare permissions without case.
        if any(isinstance(item, str) and item.casefold() == command.casefold()
               for item in commands):
            return actual_key, False
        commands.append(command)
        role_data["commands"] = commands
        _save_roles_raw_locked(raw)

    _LOG.info("Granted command %s to role %s.", command, actual_key)
    return actual_key, True


def remove_command_from_role(
    role_name: str, command_name: str
) -> tuple[str, bool] | None:
    """Remove a command/alias permission and atomically save roles.json.

    Returns (stored_role_name, removed_entry), or None if the role/token is
    invalid/not found. Comparison is case-insensitive.
    """
    name = _normalise_role_name(role_name)
    command = _normalise_command_name(command_name)
    if name is None or command is None:
        return None

    with _LOCK:
        raw = _read_roles_raw_locked()
        actual_key = _find_role_key(raw, name)
        if actual_key is None:
            return None

        role_data = raw[actual_key]
        commands = role_data.get("commands", [])
        if not isinstance(commands, list):
            commands = []
        kept = [
            item for item in commands
            if not (isinstance(item, str) and item.casefold() == command.casefold())
        ]
        if len(kept) == len(commands):
            return actual_key, False
        role_data["commands"] = kept
        _save_roles_raw_locked(raw)

    _LOG.info("Revoked command %s from role %s.", command, actual_key)
    return actual_key, True

# ---------------------------------------------------------------------------
# Independent custom tags. These override role tags but never change roles.json.
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data: Any) -> None:
    """Write JSON atomically in the same directory as its destination."""
    temporary_path = path + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            _LOG.warning("Could not clean temporary JSON file %s.", temporary_path)


def _validate_custom_tags(raw: Any) -> dict[str, dict[str, str]]:
    """Validate customtag.json entries; no animation number is stored."""
    if not isinstance(raw, dict):
        raise ValueError("customtag.json root must be a JSON object")
    result: dict[str, dict[str, str]] = {}
    for account_id, value in raw.items():
        if not _is_v2_id(account_id):
            _LOG.warning("Ignoring custom tag with an invalid/non-V2 account ID: %r", account_id)
            continue
        if not isinstance(value, dict):
            _LOG.warning("Ignoring invalid custom-tag record for %s.", account_id)
            continue
        tag_text = value.get("tag", "")
        if not isinstance(tag_text, str):
            _LOG.warning("Ignoring non-string custom tag for %s.", account_id)
            continue
        tag_text = tag_text.strip()
        if tag_text:
            result[account_id] = {"tag": tag_text}
    return result


def _load_custom_tags_locked(force_reload: bool = False) -> dict[str, dict[str, str]]:
    """Load custom tags while the caller holds _LOCK."""
    global _CUSTOM_TAGS_CACHE, _CUSTOM_TAGS_SIGNATURE
    signature = _signature(_CUSTOM_TAGS_FILE)
    if (
        not force_reload
        and _CUSTOM_TAGS_CACHE is not None
        and signature == _CUSTOM_TAGS_SIGNATURE
    ):
        return _CUSTOM_TAGS_CACHE

    if signature is None:
        _atomic_write_json(_CUSTOM_TAGS_FILE, {})
        signature = _signature(_CUSTOM_TAGS_FILE)

    try:
        with open(_CUSTOM_TAGS_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)
        _CUSTOM_TAGS_CACHE = _validate_custom_tags(raw)
        _CUSTOM_TAGS_SIGNATURE = signature
        _LOG.info("Loaded %d custom tag(s) from %s.", len(_CUSTOM_TAGS_CACHE), _CUSTOM_TAGS_FILE)
    except Exception:
        _LOG.exception("Could not load customtag.json; custom tags are disabled until repaired.")
        _CUSTOM_TAGS_CACHE = {}
        _CUSTOM_TAGS_SIGNATURE = signature
    return _CUSTOM_TAGS_CACHE


def get_custom_tags(force_reload: bool = False) -> dict[str, dict[str, str]]:
    """Return a copy of custom tag records keyed by V2 account ID."""
    with _LOCK:
        records = _load_custom_tags_locked(force_reload)
        return {account_id: dict(record) for account_id, record in records.items()}


def get_custom_tag(account_id: str | None) -> str | None:
    """Return the player's custom tag, if one is stored for their V2 account ID."""
    if not _is_v2_id(account_id):
        return None
    with _LOCK:
        record = _load_custom_tags_locked().get(account_id)
        tag_text = record.get("tag", "") if record else ""
        return tag_text or None


def set_custom_tag(account_id: str | None, tag_text: str) -> bool:
    """Create or replace a custom tag; returns False for invalid input."""
    global _CUSTOM_TAGS_CACHE, _CUSTOM_TAGS_SIGNATURE
    if not _is_v2_id(account_id) or not isinstance(tag_text, str):
        return False
    tag_text = tag_text.strip()
    if not tag_text or len(tag_text) > 32 or "\n" in tag_text or "\r" in tag_text:
        return False

    with _LOCK:
        records = _load_custom_tags_locked()
        records[account_id] = {"tag": tag_text}
        _atomic_write_json(_CUSTOM_TAGS_FILE, records)
        _CUSTOM_TAGS_CACHE = records
        _CUSTOM_TAGS_SIGNATURE = _signature(_CUSTOM_TAGS_FILE)
    _LOG.info("Saved custom tag for V2 account %s.", account_id)
    return True


def remove_custom_tag(account_id: str | None) -> bool:
    """Remove only the player's custom tag; assigned roles remain untouched."""
    global _CUSTOM_TAGS_CACHE, _CUSTOM_TAGS_SIGNATURE
    if not _is_v2_id(account_id):
        return False
    with _LOCK:
        records = _load_custom_tags_locked()
        if account_id not in records:
            return False
        records.pop(account_id, None)
        _atomic_write_json(_CUSTOM_TAGS_FILE, records)
        _CUSTOM_TAGS_CACHE = records
        _CUSTOM_TAGS_SIGNATURE = _signature(_CUSTOM_TAGS_FILE)
    _LOG.info("Removed custom tag for V2 account %s.", account_id)
    return True


def change_role_tag(role_name: str, tag_text: str) -> str | None:
    """Persistently change a role's tag while preserving all other JSON fields.

    Returns the actual role key on success, or None when the role is not found
    or the supplied tag is invalid.
    """
    global _CACHE, _SIGNATURE
    if not isinstance(role_name, str) or not isinstance(tag_text, str):
        return None
    role_name = role_name.strip()
    tag_text = tag_text.strip()
    if not role_name or not tag_text or len(tag_text) > 32 or "\n" in tag_text or "\r" in tag_text:
        return None

    with _LOCK:
        try:
            with open(_ROLES_FILE, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except Exception:
            _LOG.exception("Could not read roles.json to change a role tag.")
            raise
        if not isinstance(raw, dict):
            raise ValueError("roles.json root must be a JSON object")

        matched_key = next(
            (key for key, value in raw.items()
             if isinstance(key, str) and key.casefold() == role_name.casefold()
             and isinstance(value, dict)),
            None,
        )
        if matched_key is None:
            return None

        raw[matched_key]["tag"] = tag_text
        _atomic_write_json(_ROLES_FILE, raw)
        _CACHE = _validate_roles(raw)
        _SIGNATURE = _signature(_ROLES_FILE)
        _LOG.info("Changed tag for role %s in roles.json.", matched_key)
        return matched_key


def get_player_tag(account_id: str | None) -> tuple[str, str, str] | None:
    """Return (kind, name, text), preferring custom tags over role tags."""
    custom_tag = get_custom_tag(account_id)
    if custom_tag:
        return ("custom", "custom", custom_tag)
    assignment = get_player_role(account_id)
    if assignment is None:
        return None
    role_name, role_info = assignment
    role_tag = role_info.get("tag", "")
    if not isinstance(role_tag, str) or not role_tag.strip():
        return None
    return ("role", role_name, role_tag.strip())



# ---------------------------------------------------------------------------
# Persistent custom player effects. Records are keyed exclusively by V2 IDs.
# ---------------------------------------------------------------------------

def _normalise_effect_name(value: Any) -> str | None:
    """Normalize an effect token; actual support is checked by spaz_effects.py."""
    if not isinstance(value, str):
        return None
    name = value.strip().casefold()
    if not name or len(name) > 40:
        return None
    if not all(char.isascii() and (char.isalnum() or char == "_") for char in name):
        return None
    return name


def _validate_effect_records(raw: Any) -> dict[str, list[str]]:
    """Validate effects.json entries and discard invalid/non-V2 identities."""
    if not isinstance(raw, dict):
        raise ValueError("effects.json root must be a JSON object")
    records: dict[str, list[str]] = {}
    for account_id, value in raw.items():
        if not _is_v2_id(account_id):
            _LOG.warning("Ignoring effect record with invalid/non-V2 account ID: %r", account_id)
            continue
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, list):
            candidates = value
        else:
            _LOG.warning("Ignoring invalid effect list for %s.", account_id)
            continue
        effects: list[str] = []
        for candidate in candidates:
            effect_name = _normalise_effect_name(candidate)
            if effect_name is None:
                _LOG.warning("Ignoring invalid effect name %r for %s.", candidate, account_id)
                continue
            if effect_name not in effects:
                effects.append(effect_name)
        if effects:
            records[account_id] = effects
    return records


def _load_effects_locked(force_reload: bool = False) -> dict[str, list[str]]:
    """Load effects.json while _LOCK is held; never overwrite malformed JSON."""
    global _EFFECTS_CACHE, _EFFECTS_SIGNATURE, _EFFECTS_ERROR_SIGNATURE
    signature = _signature(_EFFECTS_FILE)
    if (
        not force_reload
        and _EFFECTS_CACHE is not None
        and signature == _EFFECTS_SIGNATURE
    ):
        return _EFFECTS_CACHE

    if signature is None:
        _atomic_write_json(_EFFECTS_FILE, {})
        signature = _signature(_EFFECTS_FILE)

    try:
        with open(_EFFECTS_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)
        validated = _validate_effect_records(raw)
    except Exception:
        if _EFFECTS_ERROR_SIGNATURE != signature:
            _LOG.exception(
                "Could not read effects.json; custom-effect changes are disabled until it is repaired."
            )
        _EFFECTS_ERROR_SIGNATURE = signature
        raise

    _EFFECTS_ERROR_SIGNATURE = None
    _EFFECTS_CACHE = validated
    _EFFECTS_SIGNATURE = signature
    _LOG.info("Loaded %d custom-effect record(s) from %s.", len(validated), _EFFECTS_FILE)
    return _EFFECTS_CACHE


def get_all_custom_effects(force_reload: bool = False) -> dict[str, list[str]]:
    """Return a copy of all persisted effects keyed by V2 account ID."""
    with _LOCK:
        records = _load_effects_locked(force_reload)
        return {account_id: list(effects) for account_id, effects in records.items()}


def get_custom_effects(account_id: str | None) -> list[str]:
    """Return the stored custom effects for a valid V2 account ID."""
    if not _is_v2_id(account_id):
        return []
    with _LOCK:
        return list(_load_effects_locked().get(account_id, []))


def set_effect(effect_name: str, account_id: str | None) -> bool:
    """Add one custom effect to a valid V2 account; preserve other effects."""
    global _EFFECTS_CACHE, _EFFECTS_SIGNATURE, _EFFECTS_ERROR_SIGNATURE
    normalized = _normalise_effect_name(effect_name)
    if not _is_v2_id(account_id) or normalized is None:
        return False
    with _LOCK:
        records = _load_effects_locked()
        current = list(records.get(account_id, []))
        if normalized not in current:
            current.append(normalized)
            records[account_id] = current
            _atomic_write_json(_EFFECTS_FILE, records)
            _EFFECTS_CACHE = records
            _EFFECTS_SIGNATURE = _signature(_EFFECTS_FILE)
            _EFFECTS_ERROR_SIGNATURE = None
    return True


def add_custom_effect(account_id: str | None, effect_name: str) -> bool:
    """Account-first wrapper for adding one effect to a valid V2 account."""
    return set_effect(effect_name, account_id)


def remove_effect(account_id: str | None, effect_name: str) -> bool:
    """Remove one saved effect from a valid V2 account; leave other effects intact."""
    global _EFFECTS_CACHE, _EFFECTS_SIGNATURE, _EFFECTS_ERROR_SIGNATURE
    normalized = _normalise_effect_name(effect_name)
    if not _is_v2_id(account_id) or normalized is None:
        return False
    with _LOCK:
        records = _load_effects_locked()
        current = records.get(account_id)
        if not current or normalized not in current:
            return False
        remaining = [item for item in current if item != normalized]
        if remaining:
            records[account_id] = remaining
        else:
            records.pop(account_id, None)
        _atomic_write_json(_EFFECTS_FILE, records)
        _EFFECTS_CACHE = records
        _EFFECTS_SIGNATURE = _signature(_EFFECTS_FILE)
        _EFFECTS_ERROR_SIGNATURE = None
    return True


def remove_all_effects(account_id: str | None) -> bool:
    """Remove every saved custom effect from a valid V2 account."""
    global _EFFECTS_CACHE, _EFFECTS_SIGNATURE, _EFFECTS_ERROR_SIGNATURE
    if not _is_v2_id(account_id):
        return False
    with _LOCK:
        records = _load_effects_locked()
        if account_id not in records:
            return False
        records.pop(account_id, None)
        _atomic_write_json(_EFFECTS_FILE, records)
        _EFFECTS_CACHE = records
        _EFFECTS_SIGNATURE = _signature(_EFFECTS_FILE)
        _EFFECTS_ERROR_SIGNATURE = None
    return True


def remove_custom_effect(account_id: str | None, effect_name: str | None = None) -> bool:
    """Remove a specific effect, or all effects when effect_name is None."""
    if effect_name is None or str(effect_name).strip().casefold() == "all":
        return remove_all_effects(account_id)
    return remove_effect(account_id, effect_name)


# ---------------------------------------------------------------------------
# Persistent V2-only bans. The JSON record is keyed by account ID, never client ID.
# ---------------------------------------------------------------------------

def _utc_iso(value: datetime | None = None) -> str:
    """Return a timezone-aware UTC timestamp in a stable ISO-8601 format."""
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    """Parse a stored UTC/ISO timestamp; return None if it is malformed."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _load_bans_locked(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Load bans; caller must hold _LOCK. Invalid JSON is not overwritten."""
    global _BANS_CACHE, _BANS_SIGNATURE, _BANS_ERROR_SIGNATURE
    signature = _signature(_BANNED_FILE)
    if (
        not force_reload
        and _BANS_CACHE is not None
        and signature == _BANS_SIGNATURE
    ):
        return _BANS_CACHE

    if signature is None:
        _atomic_write_json(_BANNED_FILE, {})
        signature = _signature(_BANNED_FILE)

    try:
        with open(_BANNED_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)
        if not isinstance(raw, dict):
            raise ValueError("banned.json root must be a JSON object")
    except Exception:
        if _BANS_ERROR_SIGNATURE != signature:
            _LOG.exception(
                "Could not read banned.json; ban operations are disabled until it is repaired."
            )
        _BANS_ERROR_SIGNATURE = signature
        # Do not replace a corrupt file with an empty dictionary: that could
        # silently erase persistent moderation records.
        raise

    _BANS_ERROR_SIGNATURE = None
    validated: dict[str, dict[str, Any]] = {}
    for account_id, record in raw.items():
        if not _is_v2_id(account_id) or not isinstance(record, dict):
            _LOG.warning("Ignoring invalid ban record for account %r.", account_id)
            continue
        reason = record.get("reason", "No reason provided")
        banned_by = record.get("banned_by", "unknown")
        banned_at = record.get("banned_at", "")
        expires_at = record.get("expires_at")
        client_id_at_ban = record.get("client_id_at_ban")
        if not isinstance(reason, str):
            reason = "No reason provided"
        if not isinstance(banned_by, str):
            banned_by = "unknown"
        if not isinstance(banned_at, str):
            banned_at = ""
        if expires_at is not None and not isinstance(expires_at, str):
            _LOG.warning("Invalid expiry for %s; treating the record as a permanent ban.", account_id)
            expires_at = None
        if (
            isinstance(client_id_at_ban, bool)
            or not isinstance(client_id_at_ban, int)
            or client_id_at_ban < 0
        ):
            client_id_at_ban = None
        validated[account_id] = {
            "reason": reason[:256],
            "banned_by": banned_by,
            "banned_at": banned_at,
            "expires_at": expires_at,
            "client_id_at_ban": client_id_at_ban,
        }

    _BANS_CACHE = validated
    _BANS_SIGNATURE = signature
    _LOG.info("Loaded %d ban record(s) from %s.", len(validated), _BANNED_FILE)
    return _BANS_CACHE


def get_banned_data(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Return a copy of the persisted V2 ban records."""
    with _LOCK:
        records = _load_bans_locked(force_reload)
        return {account_id: dict(record) for account_id, record in records.items()}


def find_active_bans_by_client_id(client_id: int) -> list[tuple[str, dict[str, Any]]]:
    """Find active bans originally created for a client ID.

    Client IDs are session-scoped and can be reused. Callers must first check
    whether that client ID currently belongs to a connected player. This helper
    is intended for recovering the V2 account after /ban has disconnected the
    player in the same server session. It can return multiple matches if an ID
    was reused for multiple still-active bans; callers should refuse ambiguity.
    """
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 0:
        return []

    with _LOCK:
        records = _load_bans_locked()
        candidates = [
            account_id
            for account_id, record in records.items()
            if record.get("client_id_at_ban") == client_id
        ]

    active: list[tuple[str, dict[str, Any]]] = []
    for account_id in candidates:
        record = get_active_ban(account_id)
        if record is not None:
            active.append((account_id, record))
    return active


def is_owner_account_id(account_id: str | None) -> bool:
    """Recognize the protected owner and configured owner-role members."""
    if not _is_v2_id(account_id):
        return False

    if account_id == _PROTECTED_OWNER_ACCOUNT_ID:
        return True

    for role_name, role_data in get_roles().items():
        if not isinstance(role_name, str):
            continue

        if role_name.casefold() != "owner":
            continue

        ids = role_data.get("ids", [])
        if isinstance(ids, list) and account_id in ids:
            return True

    return False


def ban_player(
    account_id: str | None,
    reason: str = "No reason provided",
    banned_by: str | None = None,
    duration_hours: int | None = None,
    client_id_at_ban: int | None = None,
) -> bool:
    """Persist a permanent or hours-based ban. None duration means permanent."""
    global _BANS_CACHE, _BANS_SIGNATURE
    if not _is_v2_id(account_id):
        return False
    if duration_hours is not None and (
        isinstance(duration_hours, bool)
        or not isinstance(duration_hours, int)
        or duration_hours < 1
    ):
        return False
    if is_owner_account_id(account_id):
        _LOG.warning("Refused to ban protected owner account %s.", account_id)
        return False

    reason = str(reason).strip()[:256] or "No reason provided"
    actor = banned_by if _is_v2_id(banned_by) else "unknown"
    now = datetime.now(timezone.utc)
    expiry = _utc_iso(now + timedelta(hours=duration_hours)) if duration_hours is not None else None
    if (
        isinstance(client_id_at_ban, bool)
        or not isinstance(client_id_at_ban, int)
        or client_id_at_ban < 0
    ):
        client_id_at_ban = None

    record = {
        "reason": reason,
        "banned_by": actor,
        "banned_at": _utc_iso(now),
        "expires_at": expiry,
        # Kept only as a convenience for /unban <clientID>; V2 ID remains the key.
        "client_id_at_ban": client_id_at_ban,
    }

    with _LOCK:
        records = _load_bans_locked()
        records[account_id] = record
        _atomic_write_json(_BANNED_FILE, records)
        _BANS_CACHE = records
        _BANS_SIGNATURE = _signature(_BANNED_FILE)
    _LOG.info(
        "Saved %s ban for V2 account %s.",
        f"{duration_hours}-hour" if duration_hours is not None else "permanent",
        account_id,
    )
    return True


def unban_player(account_id: str | None) -> bool:
    """Remove a saved ban by V2 account ID; return False if no record exists."""
    global _BANS_CACHE, _BANS_SIGNATURE
    if not _is_v2_id(account_id):
        return False
    with _LOCK:
        records = _load_bans_locked()
        if account_id not in records:
            return False
        records.pop(account_id, None)
        _atomic_write_json(_BANNED_FILE, records)
        _BANS_CACHE = records
        _BANS_SIGNATURE = _signature(_BANNED_FILE)
    _LOG.info("Removed ban record for V2 account %s.", account_id)
    return True


def get_active_ban(account_id: str | None) -> dict[str, Any] | None:
    """Return an active ban; automatically delete an expired temporary ban."""
    global _BANS_CACHE, _BANS_SIGNATURE
    if not _is_v2_id(account_id):
        return None
    with _LOCK:
        records = _load_bans_locked()
        record = records.get(account_id)
        if record is None:
            return None

        expires_at = record.get("expires_at")
        if expires_at is not None:
            expiry = _parse_utc(expires_at)
            if expiry is None:
                # Fail closed for a malformed saved expiry: until an operator
                # repairs or removes it, this record behaves as a permanent ban.
                _LOG.warning(
                    "Malformed expiry for ban on %s; retaining it as active until manually unbanned.",
                    account_id,
                )
                return dict(record)
            if datetime.now(timezone.utc) >= expiry:
                records.pop(account_id, None)
                _atomic_write_json(_BANNED_FILE, records)
                _BANS_CACHE = records
                _BANS_SIGNATURE = _signature(_BANNED_FILE)
                _LOG.info("Expired temporary ban removed for V2 account %s.", account_id)
                return None

        return dict(record)

# ---------------------------------------------------------------------------
# Persistent V2-only staff mutes. These are distinct from short anti-spam mutes.
# ---------------------------------------------------------------------------

def _load_mutes_locked(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Load muted.json while holding _LOCK; preserve corrupt files for repair."""
    global _MUTES_CACHE, _MUTES_SIGNATURE, _MUTES_ERROR_SIGNATURE
    signature = _signature(_MUTED_FILE)
    if (
        not force_reload
        and _MUTES_CACHE is not None
        and signature == _MUTES_SIGNATURE
    ):
        return _MUTES_CACHE

    if signature is None:
        _atomic_write_json(_MUTED_FILE, {})
        signature = _signature(_MUTED_FILE)

    try:
        with open(_MUTED_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)
        if not isinstance(raw, dict):
            raise ValueError("muted.json root must be a JSON object")
    except Exception:
        if _MUTES_ERROR_SIGNATURE != signature:
            _LOG.exception(
                "Could not read muted.json; staff mute operations are disabled until it is repaired."
            )
        _MUTES_ERROR_SIGNATURE = signature
        raise

    _MUTES_ERROR_SIGNATURE = None
    validated: dict[str, dict[str, Any]] = {}
    for account_id, record in raw.items():
        if not _is_v2_id(account_id) or not isinstance(record, dict):
            _LOG.warning("Ignoring invalid mute record for account %r.", account_id)
            continue
        reason = record.get("reason", "No reason provided")
        muted_by = record.get("muted_by", "unknown")
        muted_at = record.get("muted_at", "")
        expires_at = record.get("expires_at")
        client_id_at_mute = record.get("client_id_at_mute")
        if not isinstance(reason, str):
            reason = "No reason provided"
        if not isinstance(muted_by, str):
            muted_by = "unknown"
        if not isinstance(muted_at, str):
            muted_at = ""
        if expires_at is not None and not isinstance(expires_at, str):
            _LOG.warning(
                "Invalid expiry for mute on %s; treating it as indefinite until manually unmuted.",
                account_id,
            )
            expires_at = None
        if (
            isinstance(client_id_at_mute, bool)
            or not isinstance(client_id_at_mute, int)
            or client_id_at_mute < 0
        ):
            client_id_at_mute = None
        validated[account_id] = {
            "reason": reason[:256],
            "muted_by": muted_by,
            "muted_at": muted_at,
            "expires_at": expires_at,
            "client_id_at_mute": client_id_at_mute,
        }

    _MUTES_CACHE = validated
    _MUTES_SIGNATURE = signature
    _LOG.info("Loaded %d staff mute record(s) from %s.", len(validated), _MUTED_FILE)
    return _MUTES_CACHE


def get_muted_data(force_reload: bool = False) -> dict[str, dict[str, Any]]:
    """Return a copy of staff mute records keyed by V2 account ID."""
    with _LOCK:
        records = _load_mutes_locked(force_reload)
        return {account_id: dict(record) for account_id, record in records.items()}


def mute_player(
    account_id: str | None,
    reason: str = "No reason provided",
    muted_by: str | None = None,
    duration_hours: int | None = None,
    client_id_at_mute: int | None = None,
) -> bool:
    """Persist an indefinite or hours-based staff mute for a valid V2 ID."""
    global _MUTES_CACHE, _MUTES_SIGNATURE, _MUTES_ERROR_SIGNATURE
    if not _is_v2_id(account_id):
        return False
    if duration_hours is not None and (
        isinstance(duration_hours, bool)
        or not isinstance(duration_hours, int)
        or duration_hours < 1
    ):
        return False
    if is_owner_account_id(account_id):
        _LOG.warning("Refused to staff-mute protected owner account %s.", account_id)
        return False

    reason = str(reason).strip()[:256] or "No reason provided"
    actor = muted_by if _is_v2_id(muted_by) else "unknown"
    now = datetime.now(timezone.utc)
    expiry = (
        _utc_iso(now + timedelta(hours=duration_hours))
        if duration_hours is not None
        else None
    )
    if (
        isinstance(client_id_at_mute, bool)
        or not isinstance(client_id_at_mute, int)
        or client_id_at_mute < 0
    ):
        client_id_at_mute = None

    record = {
        "reason": reason,
        "muted_by": actor,
        "muted_at": _utc_iso(now),
        "expires_at": expiry,
        "client_id_at_mute": client_id_at_mute,
    }
    with _LOCK:
        records = _load_mutes_locked()
        records[account_id] = record
        _atomic_write_json(_MUTED_FILE, records)
        _MUTES_CACHE = records
        _MUTES_SIGNATURE = _signature(_MUTED_FILE)
        _MUTES_ERROR_SIGNATURE = None
    _LOG.info(
        "Saved %s staff mute for V2 account %s.",
        f"{duration_hours}-hour" if duration_hours is not None else "indefinite",
        account_id,
    )
    return True


def unmute_player(account_id: str | None) -> bool:
    """Remove a saved staff mute by V2 account ID."""
    global _MUTES_CACHE, _MUTES_SIGNATURE, _MUTES_ERROR_SIGNATURE
    if not _is_v2_id(account_id):
        return False
    with _LOCK:
        records = _load_mutes_locked()
        if account_id not in records:
            return False
        records.pop(account_id, None)
        _atomic_write_json(_MUTED_FILE, records)
        _MUTES_CACHE = records
        _MUTES_SIGNATURE = _signature(_MUTED_FILE)
        _MUTES_ERROR_SIGNATURE = None
    _LOG.info("Removed staff mute for V2 account %s.", account_id)
    return True


def find_active_mutes_by_client_id(client_id: int) -> list[tuple[str, dict[str, Any]]]:
    """Find currently active mute records originally created for a session client ID."""
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 0:
        return []
    with _LOCK:
        records = _load_mutes_locked()
        candidates = [
            account_id
            for account_id, record in records.items()
            if record.get("client_id_at_mute") == client_id
        ]
    active: list[tuple[str, dict[str, Any]]] = []
    for account_id in candidates:
        record = get_active_mute(account_id)
        if record is not None:
            active.append((account_id, record))
    return active


def get_active_mute(account_id: str | None) -> dict[str, Any] | None:
    """Return a staff mute record, removing expired temporary mutes automatically."""
    global _MUTES_CACHE, _MUTES_SIGNATURE, _MUTES_ERROR_SIGNATURE
    if not _is_v2_id(account_id):
        return None
    with _LOCK:
        records = _load_mutes_locked()
        record = records.get(account_id)
        if record is None:
            return None
        expires_at = record.get("expires_at")
        if expires_at is not None:
            expiry = _parse_utc(expires_at)
            if expiry is None:
                _LOG.warning(
                    "Malformed expiry for mute on %s; retaining it as active until manually unmuted.",
                    account_id,
                )
                return dict(record)
            if datetime.now(timezone.utc) >= expiry:
                records.pop(account_id, None)
                _atomic_write_json(_MUTED_FILE, records)
                _MUTES_CACHE = records
                _MUTES_SIGNATURE = _signature(_MUTED_FILE)
                _MUTES_ERROR_SIGNATURE = None
                _LOG.info("Expired temporary mute removed for V2 account %s.", account_id)
                return None
        return dict(record)

