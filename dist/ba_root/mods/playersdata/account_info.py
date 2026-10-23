# ba_meta require api 9
"""Persistent public V2 account tag and creation-time records."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from tools.account_ids import is_v2_account_id

_LOG = logging.getLogger("Lucifer.AccountInfo")
_DATA_PATH = os.path.join(os.path.dirname(__file__), "account_info.json")
_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}
_SIGNATURE: tuple[int, int] | None = None


def _signature() -> tuple[int, int] | None:
    try:
        stat = os.stat(_DATA_PATH)
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def _normalize_created_time(value: Any) -> str | None:
    """Convert API datetime/string values to a UTC ISO timestamp."""
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, str) and value.strip():
            raw = value.strip()
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
        else:
            return None

        # The API provides an account creation datetime. If a build supplies a
        # naive value, store it as UTC rather than inventing a local timezone.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return None


def _load_locked() -> dict[str, dict[str, Any]]:
    global _CACHE, _SIGNATURE
    signature = _signature()
    if signature == _SIGNATURE:
        return _CACHE

    if signature is None:
        _CACHE = {}
        _SIGNATURE = None
        return _CACHE

    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as stream:
            raw = json.load(stream)
        if not isinstance(raw, dict):
            raise ValueError("account_info.json root must be an object")
        clean: dict[str, dict[str, Any]] = {}
        for account_id, item in raw.items():
            if not is_v2_account_id(account_id) or not isinstance(item, dict):
                continue
            tag = item.get("tag")
            created_at = _normalize_created_time(item.get("created_at"))
            if not isinstance(tag, str):
                tag = ""
            clean[account_id] = {
                "tag": " ".join(tag.replace("\r", " ").replace("\n", " ").split())[:64],
                "created_at": created_at,
            }
        _CACHE = clean
        _SIGNATURE = signature
    except (OSError, ValueError, TypeError):
        _LOG.exception("Could not read account info from %s.", _DATA_PATH)
        _CACHE = {}
        _SIGNATURE = signature
    return _CACHE


def _save_locked() -> bool:
    global _SIGNATURE
    temporary_path = _DATA_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_DATA_PATH), exist_ok=True)
        with open(temporary_path, "w", encoding="utf-8") as stream:
            json.dump(_CACHE, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, _DATA_PATH)
        _SIGNATURE = _signature()
        return True
    except OSError:
        _LOG.exception("Could not save account info to %s.", _DATA_PATH)
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass
        return False


def record_account_info(account_id: Any, account_tag: Any, create_time: Any) -> bool:
    """Store account tag/creation timestamp from an accepted V2 auth request."""
    if not is_v2_account_id(account_id):
        return False
    tag = " ".join(str(account_tag or "").replace("\r", " ").replace("\n", " ").split())[:64]
    created_at = _normalize_created_time(create_time)
    if not tag or created_at is None:
        _LOG.warning("Skipping incomplete V2 account info for %s.", account_id)
        return False

    with _LOCK:
        _load_locked()
        previous = _CACHE.get(account_id, {})
        # Preserve existing valid values if a later request is incomplete.
        _CACHE[account_id] = {
            "tag": tag,
            "created_at": created_at or previous.get("created_at"),
        }
        return _save_locked()


def get_account_info(account_id: Any) -> dict[str, Any] | None:
    """Return cached public account details for a valid V2 account ID."""
    if not is_v2_account_id(account_id):
        return None
    with _LOCK:
        item = _load_locked().get(account_id)
        return dict(item) if item else None


def format_created_time(value: Any) -> str:
    """Format a stored creation timestamp as an unambiguous UTC date/time."""
    normalized = _normalize_created_time(value)
    if normalized is None:
        return "unavailable"
    try:
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError):
        return "unavailable"
