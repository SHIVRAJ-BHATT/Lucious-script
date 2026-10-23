# ba_meta require api 9
"""Bounded persistent recent-player history keyed exclusively by V2 account ID."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.account_ids import is_v2_account_id

_LOG = logging.getLogger("RoleTags.RecentPlayers")
_RECENTS_FILE = Path(__file__).with_name("recents.json")
_LOCK = threading.RLock()
_MAX_RECORDS = 50
_CONNECTED: dict[int, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_locked() -> list[dict[str, Any]]:
    if not _RECENTS_FILE.exists():
        return []
    try:
        with _RECENTS_FILE.open("r", encoding="utf-8") as file:
            raw = json.load(file)
        if not isinstance(raw, list):
            raise ValueError("recents.json root must be a JSON list")
        result: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict) or not is_v2_account_id(item.get("account_id")):
                continue
            name = item.get("name", "Unknown player")
            if not isinstance(name, str):
                name = "Unknown player"
            client_id = item.get("client_id")
            if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 0:
                client_id = None
            last_seen = item.get("last_seen", "unknown time")
            if not isinstance(last_seen, str):
                last_seen = "unknown time"
            result.append({
                "account_id": item["account_id"],
                "name": " ".join(name.replace("\r", " ").replace("\n", " ").split())[:80],
                "client_id": client_id,
                "last_seen": last_seen,
            })
        return result[-_MAX_RECORDS:]
    except Exception:
        _LOG.exception("Could not read playersdata/recents.json; preserving the existing file for repair.")
        return []


def _write_locked(records: list[dict[str, Any]]) -> None:
    temp = str(_RECENTS_FILE) + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as file:
            json.dump(records[-_MAX_RECORDS:], file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, _RECENTS_FILE)
    finally:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except OSError:
            _LOG.debug("Could not remove temporary recents file %s.", temp, exc_info=True)


def record_roster(roster: Any) -> None:
    """Persist only newly observed (client ID, V2 account ID) roster pairs."""
    global _CONNECTED
    if not isinstance(roster, (list, tuple)):
        return
    current: dict[int, str] = {}
    details: dict[tuple[int, str], str] = {}
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        try:
            client_id = int(entry.get("client_id"))
        except (TypeError, ValueError, OverflowError):
            continue
        account_id = entry.get("account_id")
        if client_id < 0 or not is_v2_account_id(account_id):
            continue
        display = entry.get("display_string", entry.get("name", "Unknown player"))
        if isinstance(display, (list, tuple)):
            display = " ".join(str(part) for part in display if part is not None)
        if not isinstance(display, str) or not display.strip():
            display = "Unknown player"
        display = " ".join(display.replace("\r", " ").replace("\n", " ").split())[:80]
        current[client_id] = account_id
        details[(client_id, account_id)] = display

    with _LOCK:
        prior_pairs = set(_CONNECTED.items())
        new_pairs = [(client_id, account_id) for client_id, account_id in current.items()
                     if (client_id, account_id) not in prior_pairs]
        _CONNECTED = current
        if not new_pairs:
            return
        records = _read_locked()
        changed = False
        for client_id, account_id in new_pairs:
            records = [item for item in records if item.get("account_id") != account_id]
            records.append({
                "account_id": account_id,
                "name": details[(client_id, account_id)],
                "client_id": client_id,
                "last_seen": _now_iso(),
            })
            changed = True
        if changed:
            _write_locked(records[-_MAX_RECORDS:])
            _LOG.debug("Recorded %d newly observed player(s) in recent history.", len(new_pairs))


def get_recent_players(limit: int = 15) -> list[dict[str, Any]]:
    """Return most recently observed V2 players, newest first."""
    try:
        count = max(1, min(int(limit), _MAX_RECORDS))
    except (TypeError, ValueError, OverflowError):
        count = 15
    with _LOCK:
        records = _read_locked()
        return [dict(record) for record in reversed(records[-count:])]
