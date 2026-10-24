# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Owner-only commands: bomb powers, effects, and LUCIOUSBELLY (LB) economy tools."""

from __future__ import annotations

import logging
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers
from features import owner_powers as fx
from shop import coins, currency
from tools.account_ids import get_v2_account_id, is_v2_account_id

_LOG = logging.getLogger("Lucious.OwnerCommands")
_RED = (1.0, 0.18, 0.18)
_GOLD = (1.0, 0.78, 0.25)
_CYAN = (0.35, 0.9, 1.0)
_MAX_AMOUNT = 10**9

Commands = [
    "nuke", "bombshower", "airstrike", "smite", "thunder", "blackout",
    "announce", "arrive", "addlb", "removelb", "setlb", "checklb", "ownerhelp",
]
CommandAliases = ["shower", "strike", "dark", "ohelp"]
_ALIAS = {"shower": "bombshower", "strike": "airstrike", "dark": "blackout", "ohelp": "ownerhelp"}


def canonical_command(command: str) -> str:
    name = str(command).strip().casefold().lstrip("/")
    return _ALIAS.get(name, name)


def _err(message: str, clientid: int) -> None:
    handlers.send(f"\ue062  {message}  \ue062", clientid, color=_RED)


def _need_activity(clientid: int) -> Any | None:
    activity = fx.get_activity()
    if activity is None:
        _err("There is no active game.", clientid)
    return activity


def _pick_players(activity: Any, token: str, clientid: int) -> list[Any] | None:
    """Zero-based activity player ID (see /list) or 'all'."""
    try:
        players = list(activity.players)
    except Exception:
        _err("Could not read the player list.", clientid)
        return None
    token = token.strip().casefold()
    if token == "all":
        return players
    try:
        index = int(token)
    except ValueError:
        _err("Player ID must be a number or 'all'.", clientid)
        return None
    if index < 0 or index >= len(players):
        _err("Player ID not found; use /list.", clientid)
        return None
    return [players[index]]


def _seconds(arguments: list[str], default: float, low: float, high: float,
             clientid: int) -> float | None:
    if not arguments:
        return default
    try:
        value = float(arguments[0])
    except ValueError:
        _err(f"Seconds must be a number ({low:g}-{high:g}).", clientid)
        return None
    return max(low, min(high, value))


# --------------------------------- bombs -----------------------------------
def _nuke(args: list[str], clientid: int) -> bool:
    activity = _need_activity(clientid)
    if activity is None:
        return False
    centers: list[tuple[float, float] | None] = [None]
    if args:
        players = _pick_players(activity, args[0], clientid)
        if players is None:
            return False
        centers = []
        for player in players[:8]:
            pos = fx.player_position(player)
            if pos is not None:
                centers.append((pos[0], pos[2]))
        if not centers:
            _err("No living target found.", clientid)
            return False
    fx.nuke(activity, centers)
    return True


def _bombshower(args: list[str], clientid: int) -> bool:
    if args and args[0].casefold() == "stop":
        fx.stop_shower()
        handlers.send("\ue043  BOMB SHOWER STOPPED  \ue043", clientid, color=_CYAN)
        return True
    activity = _need_activity(clientid)
    if activity is None:
        return False
    seconds = _seconds(args, fx.SHOWER_SECONDS, 10.0, 300.0, clientid)
    if seconds is None:
        return False
    fx.start_shower(activity, seconds)
    return True


def _airstrike(args: list[str], clientid: int) -> bool:
    if len(args) != 1:
        _err("Usage: /airstrike <playerID|all>", clientid)
        return False
    activity = _need_activity(clientid)
    players = _pick_players(activity, args[0], clientid) if activity else None
    if not players:
        return False
    if fx.airstrike(activity, players) == 0:
        _err("No living target found.", clientid)
        return False
    return True


def _smite(args: list[str], clientid: int) -> bool:
    if len(args) != 1:
        _err("Usage: /smite <playerID|all>", clientid)
        return False
    activity = _need_activity(clientid)
    players = _pick_players(activity, args[0], clientid) if activity else None
    if not players:
        return False
    if fx.smite(activity, players) == 0:
        _err("No living target found.", clientid)
        return False
    return True


def _thunder(args: list[str], clientid: int) -> bool:
    activity = _need_activity(clientid)
    seconds = _seconds(args, 10.0, 4.0, 60.0, clientid)
    if activity is None or seconds is None:
        return False
    fx.darken(activity, seconds, lightning=True)
    fx.thunder(activity, seconds)
    return True


def _blackout(args: list[str], clientid: int) -> bool:
    activity = _need_activity(clientid)
    seconds = _seconds(args, 20.0, 3.0, 120.0, clientid)
    if activity is None or seconds is None:
        return False
    fx.darken(activity, seconds, lightning=False)
    return True


def _announce(args: list[str], clientid: int) -> bool:
    text = " ".join(args).strip()
    if not text or len(text) > 120:
        _err("Usage: /announce <text up to 120 chars>", clientid)
        return False
    bs.broadcastmessage(text, color=_GOLD, top=True)
    activity = fx.get_activity()
    if activity is not None:
        fx.banner(activity, text.upper(), None, 5.0)
    return True


def _arrive(args: list[str], clientid: int) -> bool:
    if not fx.arrival("OWNER"):
        _err("There is no active game.", clientid)
        return False
    return True


# ------------------------------- LB economy ---------------------------------
def _lb_targets(token: str, clientid: int) -> list[tuple[str, str, int]] | None:
    """Resolve 'all' or a client ID into (v2_account_id, name, client_id)."""
    session = bs.get_foreground_host_session()
    if session is None:
        _err("No active session.", clientid)
        return None
    token = token.strip().casefold()
    wanted: int | None = None
    if token != "all":
        try:
            wanted = int(token)
        except ValueError:
            _err("Target must be a client ID or 'all'.", clientid)
            return None
    found: dict[str, tuple[str, str, int]] = {}
    for sp in getattr(session, "sessionplayers", ()):
        try:
            cid = int(sp.inputdevice.client_id)
        except Exception:
            continue
        if wanted is not None and cid != wanted:
            continue
        account = get_v2_account_id(sp)
        if not is_v2_account_id(account) or account in found:
            continue
        try:
            name = " ".join(str(sp.getname(full=False, icon=False)).split())[:64] or "Player"
        except Exception:
            name = "Player"
        found[account] = (account, name, cid)
    if not found:
        _err("No connected player with a verified V2 account matched.", clientid)
        return None
    return list(found.values())


def _amount(value: str, clientid: int, allow_zero: bool = False) -> int | None:
    try:
        number = int(value)
    except ValueError:
        _err("Amount must be a whole number.", clientid)
        return None
    if number < (0 if allow_zero else 1) or number > _MAX_AMOUNT:
        _err(f"Amount must be between {0 if allow_zero else 1} and {_MAX_AMOUNT:,}.", clientid)
        return None
    return number


def _lb_change(command: str, args: list[str], clientid: int) -> bool:
    if len(args) != 2:
        _err(f"Usage: /{command} <clientID|all> <amount>", clientid)
        return False
    amount = _amount(args[1], clientid, allow_zero=(command == "setlb"))
    targets = _lb_targets(args[0], clientid)
    if amount is None or targets is None:
        return False

    done = 0
    for account, name, cid in targets:
        try:
            balance = coins.get_balance(account, name)
            if command == "addlb":
                new = coins.add_coins(account, amount, name)
                note = f"+{currency.fmt(amount)}"
            elif command == "removelb":
                take = min(amount, balance)
                if take < 1:
                    continue
                new = coins.spend_coins(account, take, name)
                note = f"-{currency.fmt(take)}"
            else:  # setlb
                if amount > balance:
                    new = coins.add_coins(account, amount - balance, name)
                elif amount < balance:
                    new = coins.spend_coins(account, balance - amount, name)
                else:
                    new = balance
                note = f"BALANCE SET: {currency.fmt(amount)}"
            if new is None:
                continue
            handlers.send(
                f"\ue04c  LUCIOUS ECONOMY  \ue04d\n\ue047  {note}\n"
                f"\ue048  BALANCE: {currency.fmt(new)}", cid, color=_GOLD)
            done += 1
        except Exception:
            _LOG.exception("/%s failed for %s.", command, account)
    if done == 0:
        _err("No wallet was changed.", clientid)
        return False
    handlers.send(f"\ue043  /{command} applied to {done} player(s)  \ue043", clientid, color=_CYAN)
    return True


def _checklb(args: list[str], clientid: int) -> bool:
    if len(args) != 1:
        _err("Usage: /checklb <clientID>", clientid)
        return False
    targets = _lb_targets(args[0], clientid)
    if targets is None:
        return False
    lines = []
    for account, name, cid in targets[:10]:
        try:
            lines.append(f"\ue047  {name} [{cid}]: {currency.fmt(coins.get_balance(account))}")
        except Exception:
            lines.append(f"\ue062  {name} [{cid}]: wallet error")
    handlers.send("\n".join(lines), clientid, color=_GOLD)
    return True


def _ownerhelp(args: list[str], clientid: int) -> bool:
    handlers.send(
        "\ue04c  OWNER COMMANDS  \ue04d\n"
        "/nuke [playerID|all]   /bombshower [seconds|stop]\n"
        "/airstrike <playerID|all>   /smite <playerID|all>\n"
        "/thunder [sec]   /blackout [sec]   /arrive   /announce <text>\n"
        "/addlb /removelb /setlb <clientID|all> <amt>   /checklb <clientID>",
        clientid, color=_CYAN)
    return True


_HANDLERS = {
    "nuke": _nuke, "bombshower": _bombshower, "airstrike": _airstrike,
    "smite": _smite, "thunder": _thunder, "blackout": _blackout,
    "announce": _announce, "arrive": _arrive, "checklb": _checklb,
    "ownerhelp": _ownerhelp,
}


def ExcelCommand(command: str, arguments: list[str], clientid: int,
                 accountid: str | None) -> bool:
    name = canonical_command(command)
    try:
        from playersdata import pdata
        is_owner = pdata.is_owner_account_id(accountid)
    except Exception:
        _LOG.exception("Owner check failed.")
        is_owner = False
    if not is_owner:
        handlers.send_denied(clientid)
        return False
    if name in ("addlb", "removelb", "setlb"):
        return _lb_change(name, arguments, clientid)
    handler = _HANDLERS.get(name)
    if handler is None:
        return False
    try:
        return bool(handler(arguments, clientid))
    except Exception:
        _LOG.exception("Owner command /%s failed.", name)
        _err("Command failed; check the server terminal.", clientid)
        return False
