# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Owner/Editor: Lucifer
"""Persistent, V2-only coin wallets for BombSquad 1.8.0 / API 9.

Wallet identity is always a verified V2 account ID. Display names are metadata
only and are never used to resolve or merge accounts.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import os
import tempfile
import threading
import time
from typing import Any

from tools.account_ids import is_v2_account_id

_LOG = logging.getLogger("Lucifer.Coins")
_LOCK = threading.RLock()
_MAX_BALANCE = 10**12
_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "playersdata",
    "coins.json",
)

# Cache wallet data in memory to avoid reparsing coins.json on every balance
# lookup. File signatures invalidate the cache when an admin edits/replaces the
# JSON file externally (for example through SFTP). Mutations use a shallow copy
# of the account map and replace the changed wallet record before editing it.
_FileSignature = tuple[int, int, int, int, int]
_CACHE_DATA: dict[str, dict[str, Any]] | None = None
_CACHE_SIGNATURE: _FileSignature | None = None


class WalletStorageError(RuntimeError):
    """Raised when wallet data cannot safely be read or persisted."""


def _check_account_id(account_id: str | None) -> str:
    if not is_v2_account_id(account_id):
        raise ValueError("A valid V2 account ID beginning with 'a-' is required.")
    return account_id


def _normalise_name(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    cleaned = " ".join(name.split()).strip()[:64]
    return cleaned or None


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _file_signature() -> _FileSignature:
    """Return a signature that changes when coins.json is replaced or edited."""
    stat = os.stat(_STORE_PATH)
    return (
        int(stat.st_dev),
        int(stat.st_ino),
        int(stat.st_size),
        int(getattr(stat, "st_mtime_ns", stat.st_mtime * 1_000_000_000)),
        int(getattr(stat, "st_ctime_ns", stat.st_ctime * 1_000_000_000)),
    )


def _invalidate_cache() -> None:
    global _CACHE_DATA, _CACHE_SIGNATURE
    _CACHE_DATA = None
    _CACHE_SIGNATURE = None


def refresh_wallet_cache() -> None:
    """Force the next wallet operation to reload coins.json from disk.

    File edits are normally detected automatically via the file signature; this
    helper is available for administrative tools that want an explicit refresh.
    """
    with _LOCK:
        _invalidate_cache()


def _ensure_parent() -> None:
    try:
        os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
        if not os.path.exists(_STORE_PATH):
            _write_locked({})
    except WalletStorageError:
        raise
    except OSError as exc:
        raise WalletStorageError("Could not initialize playersdata/coins.json.") from exc


def _normalise_wallet_data(raw: Any) -> dict[str, dict[str, Any]]:
    """Validate persisted values without silently replacing corrupt root data."""
    if not isinstance(raw, dict):
        raise WalletStorageError("coins.json must contain a JSON object at its root.")

    result: dict[str, dict[str, Any]] = {}
    for account_id, entry in raw.items():
        if not is_v2_account_id(account_id):
            # Strict V2-only policy: no V1 or display-name fallback.
            _LOG.warning("Ignoring coin wallet with invalid/non-V2 account ID %r.", account_id)
            continue
        if not isinstance(entry, dict):
            _LOG.warning("Ignoring malformed coin wallet for %s.", account_id)
            continue

        balance = _safe_int(entry.get("balance", 0), -1)
        if balance < 0 or balance > _MAX_BALANCE:
            _LOG.warning("Ignoring invalid balance in wallet for %s.", account_id)
            continue

        last_daily = entry.get("lastDailyClaim", 0)
        if isinstance(last_daily, bool) or not isinstance(last_daily, (int, float)):
            last_claim_time = 0.0
        else:
            try:
                last_claim_time = float(last_daily)
            except (TypeError, ValueError, OverflowError):
                last_claim_time = 0.0
            if not math.isfinite(last_claim_time) or last_claim_time < 0.0:
                last_claim_time = 0.0

        name = _normalise_name(entry.get("name"))
        result[account_id] = {
            "balance": balance,
            "lastDailyClaim": last_claim_time,
            "name": name or ("Player " + account_id[-6:]),
        }
    return result


def _read_locked(*, copy_data: bool = True) -> dict[str, dict[str, Any]]:
    """Read validated wallets, reusing a signature-validated memory cache.

    Mutating callers get a shallow copy of the account map. Wallet records are
    treated as immutable until _entry_locked() explicitly copies the one being
    changed. Read-only callers may request the cached map directly.
    """
    global _CACHE_DATA, _CACHE_SIGNATURE
    _ensure_parent()

    # A file can be edited or atomically replaced by an administrator using
    # SFTP. If it changes while being read, retry once instead of caching a
    # possibly mismatched snapshot.
    for attempt in range(2):
        try:
            signature_before = _file_signature()
        except OSError as exc:
            _invalidate_cache()
            _LOG.exception("Could not inspect coin wallet data.")
            raise WalletStorageError("coins.json could not be accessed.") from exc

        if _CACHE_DATA is not None and signature_before == _CACHE_SIGNATURE:
            return dict(_CACHE_DATA) if copy_data else _CACHE_DATA

        try:
            with open(_STORE_PATH, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            _invalidate_cache()
            _LOG.exception("Could not safely read coin wallet data.")
            raise WalletStorageError(
                "coins.json could not be read. Fix or restore the file before changing balances."
            ) from exc

        try:
            signature_after = _file_signature()
        except OSError as exc:
            _invalidate_cache()
            _LOG.exception("Could not verify coin wallet data after reading it.")
            raise WalletStorageError("coins.json could not be verified after reading.") from exc

        if signature_before != signature_after:
            if attempt == 0:
                continue
            _invalidate_cache()
            raise WalletStorageError(
                "coins.json changed repeatedly while being read; retry the operation."
            )

        try:
            result = _normalise_wallet_data(raw)
        except WalletStorageError:
            _invalidate_cache()
            raise
        _CACHE_DATA = result
        _CACHE_SIGNATURE = signature_after
        return dict(result) if copy_data else result

    raise WalletStorageError("Could not obtain a stable snapshot of coins.json.")


def _write_locked(data: dict[str, dict[str, Any]]) -> None:
    """Atomically persist wallets and refresh the cache only after success."""
    global _CACHE_DATA, _CACHE_SIGNATURE
    directory = os.path.dirname(_STORE_PATH)
    temporary_path: str | None = None
    try:
        os.makedirs(directory, exist_ok=True)

        # Do not overwrite an external edit made after this process read the
        # file. Ask the caller to retry so an admin's SFTP edit is not lost.
        if _CACHE_DATA is not None and _CACHE_SIGNATURE is not None:
            try:
                current_signature = _file_signature()
            except FileNotFoundError:
                current_signature = None
            if current_signature != _CACHE_SIGNATURE:
                _invalidate_cache()
                raise WalletStorageError(
                    "coins.json changed outside the wallet module; retry the operation."
                )

        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".coins-",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = file.name
            json.dump(data, file, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, _STORE_PATH)
        temporary_path = None
    except WalletStorageError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        _invalidate_cache()
        raise WalletStorageError("Could not save coin wallet data.") from exc
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except OSError:
                _LOG.debug("Could not remove temporary coin file %s.", temporary_path, exc_info=True)

    # The atomic replacement succeeded. Refresh cache metadata; a stat failure
    # must not turn a successful write into a reported transaction failure.
    _CACHE_DATA = data
    try:
        _CACHE_SIGNATURE = _file_signature()
    except OSError:
        _CACHE_DATA = None
        _CACHE_SIGNATURE = None
        _LOG.debug("Could not refresh the coin-wallet cache signature.", exc_info=True)


def _entry_locked(
    data: dict[str, dict[str, Any]], account_id: str, name: str | None = None
) -> dict[str, Any]:
    """Return a wallet record safe to mutate without touching the cache."""
    record = data.get(account_id)
    if record is None:
        record = {
            "balance": 0,
            "lastDailyClaim": 0.0,
            "name": name or ("Player " + account_id[-6:]),
        }
    else:
        # Copy before mutation, but let each transaction decide when metadata
        # changes should be persisted (failed/no-op transactions included).
        record = dict(record)
    data[account_id] = record
    return record


def get_balance(account_id: str | None, name: str | None = None) -> int:
    """Return a wallet balance. Missing valid accounts start at zero."""
    account_id = _check_account_id(account_id)
    clean_name = _normalise_name(name)
    with _LOCK:
        data = _read_locked(copy_data=False)
        record = data.get(account_id)
        if record is None:
            if not clean_name:
                return 0
            working = dict(data)
            _entry_locked(working, account_id, clean_name)
            _write_locked(working)
            return 0
        balance = int(record["balance"])
        if clean_name and record.get("name") != clean_name:
            working = dict(data)
            updated_record = dict(record)
            updated_record["name"] = clean_name
            working[account_id] = updated_record
            _write_locked(working)
        return balance


def add_coins(
    account_id: str | None,
    amount: int,
    name: str | None = None,
) -> int:
    """Atomically add a positive number of coins and return the new balance."""
    account_id = _check_account_id(account_id)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise ValueError("Coin amount must be a positive integer.")
    clean_name = _normalise_name(name)
    with _LOCK:
        data = _read_locked()
        record = _entry_locked(data, account_id, clean_name)
        if clean_name:
            record["name"] = clean_name
        new_balance = int(record["balance"]) + amount
        if new_balance > _MAX_BALANCE:
            raise ValueError("Wallet balance limit would be exceeded.")
        record["balance"] = new_balance
        _write_locked(data)
        return new_balance


def spend_coins(
    account_id: str | None,
    amount: int,
    name: str | None = None,
) -> int | None:
    """Atomically spend coins; return new balance or None if funds are insufficient."""
    account_id = _check_account_id(account_id)
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
        raise ValueError("Coin amount must be a positive integer.")
    clean_name = _normalise_name(name)
    with _LOCK:
        data = _read_locked()
        record = _entry_locked(data, account_id, clean_name)
        balance = int(record["balance"])
        if balance < amount:
            if clean_name and record.get("name") != clean_name:
                record["name"] = clean_name
                _write_locked(data)
            return None
        if clean_name:
            record["name"] = clean_name
        new_balance = balance - amount
        record["balance"] = new_balance
        _write_locked(data)
        return new_balance


def claim_daily(
    account_id: str | None,
    reward: int,
    cooldown_hours: float = 24.0,
    name: str | None = None,
    now: float | None = None,
) -> tuple[bool, int, int]:
    """Claim a daily reward. Returns (claimed, current_balance, seconds_remaining)."""
    account_id = _check_account_id(account_id)
    if isinstance(reward, bool) or not isinstance(reward, int) or reward < 1:
        raise ValueError("Daily reward must be a positive integer.")
    if isinstance(cooldown_hours, bool) or not isinstance(cooldown_hours, (int, float)):
        raise ValueError("Daily cooldown must be a finite number of at least one hour.")
    try:
        cooldown_hours_value = float(cooldown_hours)
        current_time = float(time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Daily cooldown and current time must be finite numbers.") from exc
    if not math.isfinite(cooldown_hours_value) or cooldown_hours_value < 1.0:
        raise ValueError("Daily cooldown must be a finite number of at least one hour.")
    if not math.isfinite(current_time) or current_time < 0.0:
        raise ValueError("Current time must be a finite, non-negative timestamp.")
    cooldown = cooldown_hours_value * 3600.0
    if not math.isfinite(cooldown):
        raise ValueError("Daily cooldown is too large to represent safely.")
    clean_name = _normalise_name(name)
    with _LOCK:
        data = _read_locked()
        record = _entry_locked(data, account_id, clean_name)
        last_claim = float(record.get("lastDailyClaim", 0.0))
        elapsed = max(0.0, current_time - last_claim)
        if last_claim > 0.0 and elapsed < cooldown:
            if clean_name and record.get("name") != clean_name:
                record["name"] = clean_name
                _write_locked(data)
            return False, int(record["balance"]), max(1, int(cooldown - elapsed))
        if clean_name:
            record["name"] = clean_name
        balance = int(record["balance"]) + reward
        if balance > _MAX_BALANCE:
            raise ValueError("Wallet balance limit would be exceeded.")
        record["balance"] = balance
        record["lastDailyClaim"] = current_time
        _write_locked(data)
        return True, balance, 0


def top_balances(limit: int = 10) -> list[dict[str, Any]]:
    """Return the highest wallet balances, using bounded top-k selection."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        limit = 10
    limit = min(limit, 100)
    with _LOCK:
        data = _read_locked(copy_data=False)
        # nsmallest needs O(N log K) work and O(K) result storage, rather than
        # sorting and retaining every positive-balance wallet for a small K.
        players = heapq.nsmallest(
            limit,
            (
                {
                    "account_id": account_id,
                    "balance": int(record["balance"]),
                    "name": str(record.get("name") or ("Player " + account_id[-6:])),
                }
                for account_id, record in data.items()
                if int(record.get("balance", 0)) > 0
            ),
            key=lambda player: (
                -player["balance"],
                player["name"].casefold(),
                player["account_id"],
            ),
        )
    return players
