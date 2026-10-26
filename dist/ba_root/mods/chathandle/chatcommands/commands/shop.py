# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Original Owner/Editor: Lucious
# Modified by: <your name here>
"""Wallet, cosmetic store, and paid-command store chat commands."""

from __future__ import annotations

import logging
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers
from shop import coins
from tools.account_ids import is_v2_account_id

# ======================================================================
#  THEME - edit these values to restyle every message in this file
# ======================================================================
STORE_NAME = "LUCIOUS STORE"
STORE_FOOTER = "coins  |  style  |  perks"

_COLOR_MAIN = (0.70, 0.45, 1.00)    # violet - main panels
_COLOR_INFO = (0.40, 1.00, 0.75)    # mint   - lists / info
_COLOR_ERROR = (1.00, 0.35, 0.50)   # rose   - errors

_OPEN = "«"
_CLOSE = "»"
_BULLET = "+"
_DIVIDER = "-" * 20
# ======================================================================

_LOG = logging.getLogger("NovaStore.Commands")

Commands = ["me", "daily", "topcoins", "shop", "buytag", "buyeffect", "cmdshop"]
CommandAliases: list[str] = []
_PAID_COMMAND_KEYS = ("end", "nv", "gm")

_REGISTERED_MODULES: tuple[Any, ...] | None = None
_REGISTERED_NAME_CACHE: dict[int, tuple[Any, frozenset[str]]] = {}

# Fixed prices for allowlisted paid commands.
_PAID_COMMAND_PRICES = {
    "nv": 2000,
    "end": 5000,
    "gm": 5000,
}

# Fixed cosmetic prices (used for display and purchases).
_COSMETIC_PRICES = {
    "tagPrice": 2000,
    "effectPrice": 5000,
}


# ----------------------------------------------------------------------
#  Settings
# ----------------------------------------------------------------------
def _config() -> dict[str, Any]:
    try:
        import setting
        value = setting.get_section("economy")
        return value if isinstance(value, dict) else {}
    except Exception:
        _LOG.exception("Could not read economy settings.")
        return {}


def _economy_enabled() -> bool:
    return bool(_config().get("enable", True))


def _paid_config() -> dict[str, Any]:
    value = _config().get("paidCommands", {})
    return value if isinstance(value, dict) else {}


def _price(key: str, default: int) -> int:
    if key in _COSMETIC_PRICES:
        return _COSMETIC_PRICES[key]
    raw = _config().get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return default
    return raw


def _paid_price_from_config(command: str, economy: dict[str, Any]) -> int | None:
    paid_config = economy.get("paidCommands", {})
    if (
        not bool(economy.get("enable", True))
        or not isinstance(paid_config, dict)
        or not bool(paid_config.get("enable", True))
    ):
        return None
    normalized = str(command).strip().casefold().lstrip("/")
    if normalized not in _PAID_COMMAND_KEYS:
        return None
    return _PAID_COMMAND_PRICES.get(normalized)


def _paid_price(command: str) -> int | None:
    return _paid_price_from_config(command, _config())


def is_paid_command(command: str) -> bool:
    return _paid_price(str(command).strip().casefold().lstrip("/")) is not None


# ----------------------------------------------------------------------
#  Message styling
# ----------------------------------------------------------------------
def _send(message: str, clientid: int, color: tuple[float, float, float] = _COLOR_INFO) -> None:
    try:
        handlers.send(message, clientid, color=color)
    except TypeError:
        handlers.send(message, clientid)


def _error(message: str, clientid: int) -> None:
    _send(f"[!] {message}", clientid, _COLOR_ERROR)


def _panel(title: str, lines: list[str], clientid: int,
           color: tuple[float, float, float] = _COLOR_MAIN) -> None:
    body = "\n".join(f"  {_BULLET} {line}" for line in lines)
    _send(
        f"{_OPEN} {STORE_NAME} {_CLOSE}\n"
        f"{_DIVIDER}\n"
        f"> {title}\n"
        f"{body}\n"
        f"{_DIVIDER}\n"
        f"{STORE_FOOTER}",
        clientid,
        color,
    )


# ----------------------------------------------------------------------
#  Player helpers
# ----------------------------------------------------------------------
def _player_name(clientid: int) -> str:
    """Display name only; never used as identity."""
    try:
        activity = bs.get_foreground_host_activity()
        if activity is not None:
            for player in getattr(activity, "players", ()):
                try:
                    if int(player.sessionplayer.inputdevice.client_id) != int(clientid):
                        continue
                    getter = getattr(player, "getname", None)
                    if callable(getter):
                        try:
                            value = getter(full=False, icon=False)
                        except TypeError:
                            value = getter()
                        if isinstance(value, str) and value.strip():
                            return " ".join(value.split())[:64]
                except Exception:
                    continue
    except Exception:
        pass
    try:
        for entry in bs.get_game_roster():
            if isinstance(entry, dict) and int(entry.get("client_id", -1)) == int(clientid):
                for key in ("display_string", "name", "name_full"):
                    value = entry.get(key)
                    if isinstance(value, str) and value.strip():
                        return " ".join(value.split())[:64]
    except Exception:
        pass
    return "Player"


def _require_wallet(account_id: str | None, clientid: int) -> tuple[str, str] | None:
    if not _economy_enabled():
        _error("The store is closed right now.", clientid)
        return None
    if not is_v2_account_id(account_id):
        _error("You need a signed-in V2 account to use a wallet.", clientid)
        return None
    return account_id, _player_name(clientid)


# ----------------------------------------------------------------------
#  Command registry
# ----------------------------------------------------------------------
def _registered_modules() -> tuple[Any, ...]:
    global _REGISTERED_MODULES
    if _REGISTERED_MODULES is not None:
        return _REGISTERED_MODULES
    try:
        from chathandle.chatcommands.commands import normal_commands, management, cheats, fun
        modules = (normal_commands, management, cheats, fun)
    except Exception:
        _LOG.exception("Could not inspect installed command modules.")
        return ()
    _REGISTERED_MODULES = modules
    return modules


def _registered_names(module: Any) -> frozenset[str]:
    cache_key = id(module)
    cached = _REGISTERED_NAME_CACHE.get(cache_key)
    if cached is not None and cached[0] is module:
        return cached[1]
    names: set[str] = set()
    for attr in ("Commands", "CommandAliases"):
        raw = getattr(module, attr, ()) or ()
        if isinstance(raw, (tuple, list, set, frozenset)):
            names.update(str(item).strip().casefold().lstrip("/") for item in raw)
    result = frozenset(names)
    _REGISTERED_NAME_CACHE[cache_key] = (module, result)
    return result


def refresh_shop_command_registry() -> None:
    global _REGISTERED_MODULES
    _REGISTERED_MODULES = None
    _REGISTERED_NAME_CACHE.clear()


def has_command_permission(account_id: str | None, command: str, module: Any) -> bool:
    normalized = str(command).strip().casefold().lstrip("/")
    try:
        if handlers.check_permissions(account_id, normalized):
            return True
        canonicalizer = getattr(module, "canonical_command", None)
        if callable(canonicalizer):
            canonical = str(canonicalizer(normalized)).strip().casefold().lstrip("/")
            if canonical and canonical != normalized:
                return bool(handlers.check_permissions(account_id, canonical))
        return False
    except Exception:
        _LOG.exception("Could not determine role permission for /%s.", normalized)
        raise


def find_paid_command_module(command: str, modules: tuple[Any, ...] | list[Any] | None = None) -> Any | None:
    normalized = str(command).strip().casefold().lstrip("/")
    if normalized not in _PAID_COMMAND_KEYS or _paid_price(normalized) is None:
        return None
    candidates = tuple(modules) if modules is not None else _registered_modules()
    for module in candidates:
        if getattr(module, "__name__", "").endswith("commands.shop"):
            continue
        if normalized in _registered_names(module) and callable(getattr(module, "ExcelCommand", None)):
            return module
    return None


def available_paid_commands(modules: tuple[Any, ...] | list[Any] | None = None) -> list[tuple[str, int]]:
    economy = _config()
    candidates = tuple(modules) if modules is not None else _registered_modules()
    registered: set[str] = set()
    for module in candidates:
        if getattr(module, "__name__", "").endswith("commands.shop"):
            continue
        if callable(getattr(module, "ExcelCommand", None)):
            registered.update(_registered_names(module))
    result: list[tuple[str, int]] = []
    for command in _PAID_COMMAND_KEYS:
        price = _paid_price_from_config(command, economy)
        if price is not None and command in registered:
            result.append((command, price))
    return result


def _refresh_player_effect(clientid: int, account_id: str) -> None:
    try:
        activity = bs.get_foreground_host_activity()
        if activity is None:
            return
        from spazmod import spaz_effects
        for player in getattr(activity, "players", ()):
            try:
                if int(player.sessionplayer.inputdevice.client_id) != int(clientid):
                    continue
                actor = getattr(player, "actor", None)
                if actor is not None:
                    spaz_effects.refresh_player_effects(actor, account_id)
                return
            except Exception:
                continue
    except Exception:
        _LOG.exception("Could not refresh a purchased effect immediately.")


# ----------------------------------------------------------------------
#  Public commands
# ----------------------------------------------------------------------
def _me(arguments: list[str], clientid: int, account_id: str | None) -> bool:
    if arguments:
        _error("Usage: /me", clientid)
        return False
    wallet = _require_wallet(account_id, clientid)
    if wallet is None:
        return False
    account, name = wallet
    try:
        balance = coins.get_balance(account, name)
    except Exception:
        _LOG.exception("Could not read the wallet for %s.", account)
        _error("Couldn't load your wallet. Please tell an admin.", clientid)
        return False
    _panel("My Wallet", [f"Player: {name}", f"Balance: {balance:,} coins"], clientid)
    return True


def _daily(arguments: list[str], clientid: int, account_id: str | None) -> bool:
    if arguments:
        _error("Usage: /daily", clientid)
        return False
    wallet = _require_wallet(account_id, clientid)
    if wallet is None:
        return False
    account, name = wallet
    config = _config()
    reward = config.get("dailyReward", 100)
    cooldown = config.get("dailyCooldownHours", 24)
    if isinstance(reward, bool) or not isinstance(reward, int) or reward < 1:
        reward = 100
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or cooldown < 1:
        cooldown = 24
    try:
        claimed, balance, remaining = coins.claim_daily(account, reward, cooldown, name)
    except Exception:
        _LOG.exception("Daily claim failed for V2 account %s.", account)
        _error("Couldn't save your daily bonus. Please tell an admin.", clientid)
        return False
    if not claimed:
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60
        _error(f"Already collected! Come back in {hours}h {minutes}m.", clientid)
        return False
    _panel("Daily Bonus", [f"Received: +{reward:,} coins", f"Balance: {balance:,} coins"], clientid)
    return True


def _topcoins(arguments: list[str], clientid: int) -> bool:
    if arguments:
        _error("Usage: /topcoins", clientid)
        return False
    if not _economy_enabled():
        _error("The store is closed right now.", clientid)
        return False
    try:
        leaders = coins.top_balances(10)
    except Exception:
        _LOG.exception("Could not load the coin leaderboard.")
        _error("Leaderboard unavailable right now.", clientid)
        return False
    if not leaders:
        _panel("Richest Players", ["Nobody has coins yet - try /daily!"], clientid, _COLOR_INFO)
        return True
    lines = []
    for index, player in enumerate(leaders, 1):
        pname = str(player.get("name", "Player"))[:22]
        lines.append(f"#{index}  {pname}  -  {int(player['balance']):,}")
    _panel("Richest Players", lines, clientid, _COLOR_INFO)
    return True


def _shop(arguments: list[str], clientid: int) -> bool:
    if arguments:
        _error("Usage: /shop", clientid)
        return False
    tag_price = _price("tagPrice", 100)
    effect_price = _price("effectPrice", 200)
    try:
        from spazmod import spaz_effects
        effects = sorted(spaz_effects.EFFECT_NAMES)
    except Exception:
        _LOG.exception("Could not load custom effects for the store listing.")
        effects = []
    lines = [
        f"/buytag <text>   ->  {tag_price:,} coins",
        f"/buyeffect <name>  ->  {effect_price:,} coins",
        "You only pay when the change is applied.",
    ]
    if effects:
        lines.append("Effects: " + ", ".join(effects))
    _panel("Cosmetics", lines, clientid)
    return True


def _refund_cosmetic_purchase(account: str, price: int, name: str,
                              clientid: int, item_label: str) -> bool:
    try:
        coins.add_coins(account, price, name)
        return True
    except Exception:
        _LOG.exception("CRITICAL: refund failed for %s purchase, account %s.", item_label, account)
        _error(f"{item_label} failed AND the refund failed. Contact an admin before retrying.", clientid)
        return False


def _buytag(arguments: list[str], clientid: int, account_id: str | None) -> bool:
    if not arguments:
        _error("Usage: /buytag <tag text>", clientid)
        return False
    wallet = _require_wallet(account_id, clientid)
    if wallet is None:
        return False
    account, name = wallet
    text = " ".join(arguments).strip()
    if not text or len(text) > 32 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        _error("Tags must be 1-32 normal characters.", clientid)
        return False
    try:
        from playersdata import pdata
        if pdata.get_custom_tag(account) == text:
            _error("You already have that tag. Nothing was charged.", clientid)
            return False
        price = _price("tagPrice", 100)
        new_balance = coins.spend_coins(account, price, name)
        if new_balance is None:
            _error(f"Not enough coins - a tag costs {price:,}. Check with /me.", clientid)
            return False
        try:
            saved = pdata.set_custom_tag(account, text)
        except Exception:
            _LOG.exception("Could not save custom tag for %s.", account)
            saved = False
        if not saved:
            if _refund_cosmetic_purchase(account, price, name, clientid, "Tag"):
                _error("Tag couldn't be saved - coins refunded.", clientid)
            return False
        try:
            from spazmod import tag as tag_system
            tag_system.refresh_client_tag(clientid)
        except Exception:
            _LOG.exception("Tag saved but live refresh failed; applies next spawn.")
        _panel("Tag Updated", [f"New tag: {text}", f"Cost: {price:,} coins",
                               f"Balance: {new_balance:,} coins"], clientid)
        return True
    except Exception:
        _LOG.exception("/buytag failed for account %s.", account)
        _error("Tag purchase failed. Please tell an admin.", clientid)
        return False


def _buyeffect(arguments: list[str], clientid: int, account_id: str | None) -> bool:
    if len(arguments) != 1:
        _error("Usage: /buyeffect <effect name>", clientid)
        return False
    wallet = _require_wallet(account_id, clientid)
    if wallet is None:
        return False
    account, name = wallet
    try:
        from playersdata import pdata
        from spazmod import spaz_effects
        effect_name = spaz_effects.normalize_effect_name(arguments[0])
        if effect_name is None:
            supported = ", ".join(sorted(spaz_effects.EFFECT_NAMES))
            _error(f"Unknown effect. Try: {supported}", clientid)
            return False
        if effect_name in pdata.get_custom_effects(account):
            _error("You already own that effect. Nothing was charged.", clientid)
            return False
        price = _price("effectPrice", 200)
        new_balance = coins.spend_coins(account, price, name)
        if new_balance is None:
            _error(f"Not enough coins - an effect costs {price:,}. Check with /me.", clientid)
            return False
        try:
            saved = pdata.set_effect(effect_name, account)
        except Exception:
            _LOG.exception("Could not save custom effect for %s.", account)
            saved = False
        if not saved:
            if _refund_cosmetic_purchase(account, price, name, clientid, "Effect"):
                _error("Effect couldn't be saved - coins refunded.", clientid)
            return False
        _refresh_player_effect(clientid, account)
        _panel("Effect Added", [f"Effect: {effect_name}", f"Cost: {price:,} coins",
                                f"Balance: {new_balance:,} coins"], clientid)
        return True
    except Exception:
        _LOG.exception("/buyeffect failed for account %s.", account)
        _error("Effect purchase failed. Please tell an admin.", clientid)
        return False


def _cmdshop(arguments: list[str], clientid: int) -> bool:
    if arguments:
        _error("Usage: /cmdshop", clientid)
        return False
    if not _economy_enabled():
        _error("The store is closed right now.", clientid)
        return False
    commands = available_paid_commands()
    if not commands:
        _panel("Command Store", ["No commands for sale right now."], clientid, _COLOR_INFO)
        return True
    lines = [f"/{cmd}  ->  {price:,} coins" for cmd, price in commands]
    lines.append("You only pay if the command works.")
    _panel("Command Store", lines, clientid, _COLOR_INFO)
    return True


def ExcelCommand(command: str, arguments: list[str], clientid: int, accountid: str | None) -> bool:
    normalized = str(command).strip().casefold().lstrip("/")
    if normalized == "me":
        return _me(arguments, clientid, accountid)
    if normalized == "daily":
        return _daily(arguments, clientid, accountid)
    if normalized == "topcoins":
        return _topcoins(arguments, clientid)
    if normalized == "shop":
        return _shop(arguments, clientid)
    if normalized == "buytag":
        return _buytag(arguments, clientid, accountid)
    if normalized == "buyeffect":
        return _buyeffect(arguments, clientid, accountid)
    if normalized == "cmdshop":
        return _cmdshop(arguments, clientid)
    return False


# ----------------------------------------------------------------------
#  Paid commands
# ----------------------------------------------------------------------
def execute_paid_command(command: str, arguments: list[str], clientid: int,
                         account_id: str | None, target_module: Any) -> bool:
    command = str(command).strip().casefold().lstrip("/")
    price = _paid_price(command)
    if price is None or command not in _PAID_COMMAND_KEYS:
        return False
    wallet = _require_wallet(account_id, clientid)
    if wallet is None:
        return False
    account, name = wallet
    handler = getattr(target_module, "ExcelCommand", None)
    if not callable(handler) or command not in _registered_names(target_module):
        _error(f"/{command} isn't available here.", clientid)
        return False
    if command in ("end", "nv") and arguments:
        _error(f"Usage: /{command}", clientid)
        return False
    if command in ("end", "nv"):
        try:
            activity = bs.get_foreground_host_activity()
        except Exception:
            activity = None
        if activity is None:
            _error("No game is running right now.", clientid)
            return False
        if command == "end" and not callable(getattr(activity, "end_game", None)):
            _error("This activity can't be ended.", clientid)
            return False
        if command == "nv" and not hasattr(activity, "globalsnode"):
            _error("Night vision doesn't work in this activity.", clientid)
            return False

    try:
        new_balance = coins.spend_coins(account, price, name)
    except Exception:
        _LOG.exception("Could not charge V2 account %s for /%s.", account, command)
        _error("Payment failed. The command was not used.", clientid)
        return False
    if new_balance is None:
        _error(f"Not enough coins - /{command} costs {price:,}. Check with /me.", clientid)
        return False

    try:
        if command == "end":
            activity = bs.get_foreground_host_activity()
            with activity.context:
                activity.end_game()
            completed = True
        elif command == "nv":
            activity = bs.get_foreground_host_activity()
            with activity.context:
                activity.globalsnode.tint = (0.5, 0.5, 1.0)
                activity.globalsnode.ambient_color = (1.5, 1.5, 1.5)
            completed = True
        else:
            completed = bool(handler(command, arguments, clientid, account))
    except Exception:
        _LOG.exception("Paid command /%s raised an exception.", command)
        completed = False

    if not completed:
        try:
            coins.add_coins(account, price, name)
        except Exception:
            _LOG.exception("CRITICAL: refund failed for paid /%s, account %s.", command, account)
            _error("Command failed AND the refund failed. Contact an admin.", clientid)
            return False
        _error(f"/{command} didn't work - {price:,} coins refunded.", clientid)
        return False

    lines = [f"Used: /{command}", f"Cost: {price:,} coins", f"Balance: {new_balance:,} coins"]
    if command == "end":
        lines.append("Ending the current game...")
    elif command == "nv":
        lines.append("Night vision is on.")
    _panel("Command Used", lines, clientid)
    return True

