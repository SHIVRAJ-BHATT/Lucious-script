"""Persistent player statistics for BombSquad 1.8.0 / API 9.

This module preserves the public names used by the legacy stats mod while
using V2 account IDs only. Statistics are persisted under the server's
python_directory_user/stats/ directory.

The game-end integration must call update(score_set) once for each completed
round. This file does not install hooks or add chat commands by itself.
"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import shutil
import threading
from typing import Any

import _babase

try:
    import setting
except ImportError:  # Stats should still load if optional settings are absent.
    setting = None  # type: ignore[assignment]

from tools.account_ids import get_v2_account_id, is_v2_account_id

_LOG = logging.getLogger("Lucifer.Stats")

damage_data: dict[str, float] = {}
ranks: list[str] = []
top3Name: list[str] = []
cached_stats: dict[str, dict[str, Any]] = {}
seasonStartDate: datetime.datetime | None = None
_stats_lock = threading.RLock()
_STATS_LOAD_FAILED = False

try:
    our_settings = setting.get_settings_data() if setting is not None else {}
    if not isinstance(our_settings, dict):
        our_settings = {}
except Exception:
    _LOG.exception("Could not read settings; seasonal statistics reset is disabled.")
    our_settings = {}

try:
    _env = _babase.env()
    _python_user_dir = _env.get("python_directory_user")
except Exception:
    _python_user_dir = None

if not isinstance(_python_user_dir, str) or not _python_user_dir:
    _python_user_dir = os.path.join(os.getcwd(), "user")

base_path = os.path.join(_python_user_dir, "stats")
statsfile = os.path.join(base_path, "stats.json")
_backup_file = statsfile + ".backup"


def _ensure_stats_directory() -> None:
    os.makedirs(base_path, exist_ok=True)


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _normalise_player_record(account_id: str, record: Any) -> dict[str, Any] | None:
    """Validate one saved record and discard unrecognized identity formats."""
    if not is_v2_account_id(account_id):
        if isinstance(account_id, str) and account_id.startswith("pb-"):
            _LOG.warning(
                "Ignoring legacy V1 stats entry %r; only V2 account IDs are supported.",
                account_id,
            )
        else:
            _LOG.warning("Ignoring stats entry with invalid account ID %r.", account_id)
        return None

    if not isinstance(record, dict):
        _LOG.warning("Ignoring malformed stats entry for %s.", account_id)
        return None

    name = record.get("name", "Player")
    if not isinstance(name, str) or not name.strip():
        name = "Player"

    return {
        "rank": max(0, _safe_int(record.get("rank", 0))),
        "name": name.strip()[:128],
        "scores": max(0, _safe_int(record.get("scores", 0))),
        "total_damage": max(0.0, _safe_float(record.get("total_damage", 0.0))),
        "kills": max(0, _safe_int(record.get("kills", 0))),
        "deaths": max(0, _safe_int(record.get("deaths", 0))),
        "games": max(0, _safe_int(record.get("games", 0))),
        "kd": max(0.0, _safe_float(record.get("kd", 0.0))),
        "avg_score": max(0.0, _safe_float(record.get("avg_score", 0.0))),
        "aid": account_id,
        "last_seen": str(record.get("last_seen", ""))[:64],
    }


def _normalise_stats(raw_stats: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_stats, dict):
        raise ValueError("stats must be a JSON object")

    result: dict[str, dict[str, Any]] = {}
    for account_id, record in raw_stats.items():
        if not isinstance(account_id, str):
            _LOG.warning("Ignoring stats entry with non-string account ID.")
            continue
        normalised = _normalise_player_record(account_id, record)
        if normalised is not None:
            result[account_id] = normalised
    return result


def _configured_reset_days() -> int:
    """Return a positive reset interval, or 0 when resets are not configured."""
    value = our_settings.get("statsResetAfterDays")
    days = _safe_int(value, 0)
    return days if days > 0 else 0


def _set_season_days_remaining(days_remaining: int | None) -> None:
    try:
        if days_remaining is not None:
            _babase.season_ends_in_days = max(0, int(days_remaining))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as file:
        return json.load(file)


def _parse_start_date(value: Any) -> datetime.datetime:
    if isinstance(value, str) and value.strip():
        try:
            return datetime.datetime.strptime(value.strip(), "%d-%m-%Y")
        except ValueError:
            _LOG.warning("Invalid stats startDate %r; resetting the season date to today.", value)
    return datetime.datetime.now()


def get_stats_by_id(account_id: str):
    """Return one cached record for a valid V2 account ID, if present."""
    if not is_v2_account_id(account_id):
        return None
    stats = get_cached_stats()
    record = stats.get(account_id)
    return copy.deepcopy(record) if record is not None else None


def get_all_stats() -> dict[str, dict[str, Any]]:
    """Load statistics from stats.json, using the backup if main JSON is invalid.

    If both an existing main file and its backup are invalid, raise an error
    rather than silently returning empty records that could later overwrite data.
    """
    global seasonStartDate, _STATS_LOAD_FAILED

    with _stats_lock:
        _ensure_stats_directory()
        if not os.path.exists(statsfile):
            seasonStartDate = datetime.datetime.now()
            _STATS_LOAD_FAILED = False
            return {}

        main_error: Exception | None = None
        try:
            document = _read_json(statsfile)
        except Exception as exc:
            main_error = exc
            document = None
            _LOG.error("Could not read %s: %s", statsfile, exc)

        loaded_from_backup = False
        if document is None:
            if os.path.exists(_backup_file):
                try:
                    document = _read_json(_backup_file)
                    loaded_from_backup = True
                    _LOG.warning("Loaded player stats from backup %s.", _backup_file)
                except Exception:
                    _LOG.exception("Both stats.json and its backup are unreadable.")
                    _STATS_LOAD_FAILED = True
                    raise RuntimeError("Statistics file and backup are invalid.") from main_error
            else:
                _STATS_LOAD_FAILED = True
                raise RuntimeError(
                    "stats.json is invalid and no backup exists; refusing to erase records."
                ) from main_error

        if isinstance(document, dict) and "stats" in document:
            raw_stats = document.get("stats")
            seasonStartDate = _parse_start_date(document.get("startDate"))
        elif isinstance(document, dict):
            # Legacy/raw dictionary compatibility.
            _LOG.warning("Loading legacy raw stats JSON format.")
            raw_stats = document
            seasonStartDate = datetime.datetime.now()
        else:
            _STATS_LOAD_FAILED = True
            raise ValueError("Statistics JSON root must be an object.")

        stats = _normalise_stats(raw_stats)
        _STATS_LOAD_FAILED = False

        # Restore valid backup to the main path before a subsequent save can
        # make a fresh backup; do not copy the corrupted main file over it.
        if loaded_from_backup:
            _atomic_write_json(statsfile, {
                "startDate": seasonStartDate.strftime("%d-%m-%Y"),
                "stats": stats,
            }, make_backup=False)

        reset_days = _configured_reset_days()
        if reset_days:
            elapsed_days = max(0, (datetime.datetime.now() - seasonStartDate).days)
            _set_season_days_remaining(reset_days - elapsed_days)
            if elapsed_days >= reset_days:
                backupStatsFile()
                seasonStartDate = datetime.datetime.now()
                _LOG.info("Statistics season expired; starting a new season.")
                return {}
        return stats


def backupStatsFile() -> str | None:
    """Create a timestamped backup; return its path if successful."""
    with _stats_lock:
        if not os.path.exists(statsfile):
            return None
        _ensure_stats_directory()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = os.path.join(base_path, "stats_" + timestamp + ".json")
        try:
            shutil.copy2(statsfile, backup_path)
            _LOG.info("Created stats backup: %s", backup_path)
            return backup_path
        except OSError:
            _LOG.exception("Could not create timestamped stats backup.")
            return None


def _atomic_write_json(path: str, data: dict[str, Any], *, make_backup: bool = True) -> None:
    """Atomically write a JSON document in its target directory."""
    _ensure_stats_directory()
    temp_path = path + ".tmp"
    if make_backup and os.path.exists(path):
        try:
            shutil.copy2(path, path + ".backup")
        except OSError:
            _LOG.exception("Could not create rolling stats backup.")
            raise
    try:
        with open(temp_path, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False, sort_keys=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            _LOG.warning("Could not remove temporary stats file %s.", temp_path)


def dump_stats(stats: dict[str, Any]) -> None:
    """Persist stats and refresh the validated cache."""
    global seasonStartDate, cached_stats
    with _stats_lock:
        if _STATS_LOAD_FAILED:
            raise RuntimeError("Refusing to write stats while the database is unreadable.")
        if seasonStartDate is None:
            seasonStartDate = datetime.datetime.now()
        normalised = _normalise_stats(stats)
        document = {
            "startDate": seasonStartDate.strftime("%d-%m-%Y"),
            "stats": normalised,
        }
        _atomic_write_json(statsfile, document)
        cached_stats = copy.deepcopy(normalised)


def get_sorted_stats(stats: dict[str, Any]) -> list[tuple[int, int, int, int, str, str]]:
    """Return score leaderboard rows in descending score order."""
    entries = []
    if not isinstance(stats, dict):
        return entries

    for account_id, record in stats.items():
        if not isinstance(record, dict) or not is_v2_account_id(account_id):
            continue
        entries.append((
            _safe_int(record.get("scores", 0)),
            _safe_int(record.get("kills", 0)),
            _safe_int(record.get("deaths", 0)),
            _safe_int(record.get("games", 0)),
            str(record.get("name", "Player") or "Player"),
            account_id,
        ))
    entries.sort(key=lambda row: (row[0], row[1], -row[2], row[3], row[5]), reverse=True)
    return entries


def _recalculate_stats(stats: dict[str, dict[str, Any]]) -> list[str]:
    """Recalculate ranks and derived values without saving."""
    global ranks, top3Name
    entries = get_sorted_stats(stats)
    current_ranks: list[str] = []
    top_names: list[str] = []

    for rank, entry in enumerate(entries, start=1):
        scores, kills, deaths, games, name, account_id = entry
        record = stats[account_id]
        record["rank"] = rank
        record["scores"] = scores
        record["kills"] = kills
        record["deaths"] = deaths
        record["games"] = games
        record["kd"] = round(kills / deaths, 2) if deaths > 0 else float(kills)
        record["avg_score"] = round(scores / games, 2) if games > 0 else 0.0
        record["name"] = name or "Player"
        record["aid"] = account_id
        record["total_damage"] = max(0.0, _safe_float(record.get("total_damage", 0.0)))
        current_ranks.append(account_id)
        if rank <= 3:
            top_names.append(record["name"])

    ranks = current_ranks
    top3Name = top_names
    return current_ranks


def refreshStats() -> None:
    """Refresh ranks/KD/average score and persist the computed values."""
    global cached_stats
    with _stats_lock:
        stats = get_all_stats()
        _recalculate_stats(stats)
        dump_stats(stats)


def _v2_id_for_player(player: Any) -> str | None:
    """Extract an API 9 account ID and reject any non-V2 identity."""
    if player is None:
        return None

    # API 9 Player objects may expose get_account_id directly.
    getter = getattr(player, "get_account_id", None)
    if callable(getter):
        try:
            account_id = getter()
            if is_v2_account_id(account_id):
                return account_id
            if account_id is not None:
                _LOG.debug("Skipping non-V2 stats identity: %r", account_id)
                return None
        except Exception:
            pass

    # Otherwise use the project's validated SessionPlayer helper.
    for attr_name in ("sessionplayer", "_sessionplayer"):
        try:
            session_player = getattr(player, attr_name, None)
        except Exception:
            session_player = None
        if session_player is not None:
            try:
                account_id = get_v2_account_id(session_player)
            except Exception:
                account_id = None
            if is_v2_account_id(account_id):
                return account_id
    return None


def _get_player_name(player: Any) -> str:
    """Capture the display name from the live player; no legacy HTTP lookup."""
    if player is None:
        return "Player"
    try:
        getname = getattr(player, "getname", None)
        if callable(getname):
            try:
                name = getname(icon=False)
            except TypeError:
                name = getname()
            if isinstance(name, str) and name.strip():
                return name.strip()[:128]
    except Exception:
        pass

    try:
        session_player = getattr(player, "sessionplayer", None)
        getname = getattr(session_player, "getname", None)
        if callable(getname):
            name = getname(full=True)
            if isinstance(name, str) and name.strip():
                return name.strip()[:128]
    except Exception:
        pass
    return "Player"


def update(score_set: Any) -> None:
    """Collect a completed round's score records.

    The game integration must call this once per completed round.
    """
    if score_set is None:
        return
    kills: dict[str, int] = {}
    deaths: dict[str, int] = {}
    scores: dict[str, int] = {}
    names: dict[str, str] = {}

    try:
        records = score_set.get_records().values()
    except Exception:
        _LOG.exception("Could not read the completed round's score records.")
        return

    for record in records:
        player = getattr(record, "player", None)
        account_id = _v2_id_for_player(player)
        if not is_v2_account_id(account_id):
            continue
        kills[account_id] = kills.get(account_id, 0) + max(
            0, _safe_int(getattr(record, "accum_kill_count", 0))
        )
        deaths[account_id] = deaths.get(account_id, 0) + max(
            0, _safe_int(getattr(record, "accum_killed_count", 0))
        )
        scores[account_id] = scores.get(account_id, 0) + max(
            0, _safe_int(getattr(record, "accumscore", 0))
        )
        names[account_id] = _get_player_name(player)

    if scores:
        UpdateThread(kills, deaths, scores, names).start()
    else:
        _LOG.debug("No score records with valid V2 account IDs; stats were not updated.")


class UpdateThread(threading.Thread):
    """Persist one completed round's captured statistics off the game thread."""

    def __init__(
        self,
        kills: dict[str, int],
        deaths: dict[str, int],
        scores: dict[str, int],
        names: dict[str, str] | None = None,
    ):
        super().__init__(name="BombSquadStatsUpdate", daemon=True)
        self.kills = dict(kills)
        self.deaths = dict(deaths)
        self.scores = dict(scores)
        self.names = dict(names or {})

    def run(self) -> None:
        global cached_stats
        try:
            with _stats_lock:
                stats = get_all_stats()
                now = datetime.datetime.now().isoformat(timespec="seconds")

                for account_id, score in self.scores.items():
                    if not is_v2_account_id(account_id):
                        continue
                    record = stats.get(account_id)
                    if record is None:
                        name = self.names.get(account_id) or "Player"
                        record = {
                            "rank": 0,
                            "name": name,
                            "scores": 0,
                            "total_damage": 0.0,
                            "kills": 0,
                            "deaths": 0,
                            "games": 0,
                            "kd": 0.0,
                            "avg_score": 0.0,
                            "aid": account_id,
                            "last_seen": now,
                        }
                        stats[account_id] = record
                    else:
                        name = self.names.get(account_id)
                        if name and name != "Player":
                            record["name"] = name

                    record["scores"] = max(0, _safe_int(record.get("scores", 0))) + score
                    record["kills"] = max(0, _safe_int(record.get("kills", 0))) + self.kills.get(account_id, 0)
                    record["deaths"] = max(0, _safe_int(record.get("deaths", 0))) + self.deaths.get(account_id, 0)
                    record["games"] = max(0, _safe_int(record.get("games", 0))) + 1
                    # Consume pending damage once so later rounds do not
                    # add the same accumulated amount repeatedly.
                    pending_damage = _safe_float(damage_data.pop(account_id, 0.0))
                    record["total_damage"] = max(
                        0.0,
                        _safe_float(record.get("total_damage", 0.0))
                        + pending_damage,
                    )
                    record["last_seen"] = now
                    record["aid"] = account_id

                _recalculate_stats(stats)
                dump_stats(stats)
        except Exception:
            _LOG.exception("Could not persist completed-round statistics.")


def get_cached_stats() -> dict[str, dict[str, Any]]:
    """Return a copy of the current validated stats cache."""
    global cached_stats
    with _stats_lock:
        if not cached_stats:
            stats = get_all_stats()
            _recalculate_stats(stats)
            cached_stats = copy.deepcopy(stats)
        return copy.deepcopy(cached_stats)


def getRank(acc_id: str) -> int:
    """Return a player's score rank, or 0 for an invalid/unranked identity."""
    if not is_v2_account_id(acc_id):
        return 0
    stats = get_cached_stats()
    record = stats.get(acc_id)
    return _safe_int(record.get("rank", 0)) if record else 0


get_rank = getRank


def get_score(acc_id: str) -> int:
    if not is_v2_account_id(acc_id):
        return 0
    return _safe_int(get_cached_stats().get(acc_id, {}).get("scores", 0))


def get_kills(acc_id: str) -> int:
    if not is_v2_account_id(acc_id):
        return 0
    return _safe_int(get_cached_stats().get(acc_id, {}).get("kills", 0))


def get_deaths(acc_id: str) -> int:
    if not is_v2_account_id(acc_id):
        return 0
    return _safe_int(get_cached_stats().get(acc_id, {}).get("deaths", 0))


def get_kd(acc_id: str) -> float:
    if not is_v2_account_id(acc_id):
        return 0.0
    record = get_cached_stats().get(acc_id, {})
    return round(
        _safe_int(record.get("kills", 0)) / _safe_int(record.get("deaths", 0)),
        2,
    ) if _safe_int(record.get("deaths", 0)) > 0 else float(_safe_int(record.get("kills", 0)))


def updateTop3Names(ids: list[str]) -> None:
    """Refresh top-three names from local records without external HTTP requests."""
    global top3Name
    stats = get_cached_stats()
    top3Name = [
        str(stats.get(account_id, {}).get("name", "???") or "???")
        for account_id in ids[:3]
        if is_v2_account_id(account_id)
    ]


def record_damage(account_id: str, amount: float) -> bool:
    """Add damage to the pending damage counter for a validated V2 identity."""
    if not is_v2_account_id(account_id):
        return False
    value = _safe_float(amount, -1.0)
    if value < 0:
        return False
    with _stats_lock:
        damage_data[account_id] = damage_data.get(account_id, 0.0) + value
    return True


def initialise() -> None:
    """Load/create the database and populate rank/name caches at startup."""
    global cached_stats, seasonStartDate
    with _stats_lock:
        _ensure_stats_directory()
        if not os.path.exists(statsfile):
            seasonStartDate = datetime.datetime.now()
            dump_stats({})
            ranks.clear()
            top3Name.clear()
            cached_stats = {}
            _LOG.info("Created empty statistics database at %s.", statsfile)
            return
        stats = get_all_stats()
        _recalculate_stats(stats)
        cached_stats = copy.deepcopy(stats)


# Keep the data directory and empty database ready when the module is imported.
# A failure is logged without preventing the rest of the mod loader from running.
try:
    initialise()
except Exception:
    _LOG.exception("Statistics initialization failed; check stats.json and its backup.")
