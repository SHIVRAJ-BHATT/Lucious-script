# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Owner/Editor: Lucifer
"""Public wallet, cosmetic-purchase, and paid-command shop commands."""

from __future__ import annotations

import logging
from typing import Any

import bascenev1 as bs

from chathandle.chatcommands import handlers
from shop import coins
from tools.account_ids import is_v2_account_id

_LOG = logging.getLogger("Lucifer.CoinShopCommands")
_RED = (1.0, 0.22, 0.24)
_GOLD = (1.0, 0.78, 0.25)
_CYAN = (0.35, 0.9, 1.0)
_LEFT = "\ue04c"
_RIGHT = "\ue04d"
_ERROR_GLYPH = "\ue062"

Commands = ["me", "daily", "topcoins", "shop", "buytag", "buyeffect", "cmdshop"]
CommandAliases: list[str] = []
_PAID_COMMAND_KEYS = ("end", "nv", "gm")

# Command modules and their exported command names are stable during normal
# server operation. Cache them to avoid repeated import/attribute scanning in
# /cmdshop and paid-command dispatch. Call refresh_shop_command_registry() if
# command registrations are changed dynamically at runtime.
_REGISTERED_MODULES: tuple[Any, ...] | None = None
_REGISTERED_NAME_CACHE: dict[int, tuple[Any, frozenset[str]]] = {}


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
    """Return the fixed cosmetic price used for display and transactions."""
    if key in _COSMETIC_PRICES:
        return _COSMETIC_PRICES[key]
    raw = _config().get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return default
    return raw


# Fixed prices for the allowlisted paid commands.
# /nv is the lower-impact option; /end and /gm cost more.
_PAID_COMMAND_PRICES = {
    "nv": 2000,
    "end": 5000,
    "gm": 5000,
}

# Fixed cosmetic prices; these are used for both shop display and purchases.
_COSMETIC_PRICES = {
    "tagPrice": 2000,
    "effectPrice": 5000,
}


def _paid_price_from_config(command: str, economy: dict[str, Any]) -> int | None:
    """Resolve an allowlisted command's price from one settings snapshot."""
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
    """Return the fixed price for an enabled, allowlisted paid command."""
    return _paid_price_from_config(command, _config())


def is_paid_command(command: str) -> bool:
    """True only for the explicit paid-command allowlist and active settings."""
    return _paid_price(str(command).strip().casefold().lstrip("/")) is not None


def _send(message: str, clientid: int, color: tuple[float, float, float] = _CYAN) -> None:
    try:
        handlers.send(message, clientid, color=color)
    except TypeError:
        handlers.send(message, clientid)


def _error(message: str, clientid: int) -> None:
    _send(f"{_ERROR_GLYPH}  {_LEFT} {message} {_RIGHT}  {_ERROR_GLYPH}", clientid, _RED)


def _panel(title: str, body: str, clientid: int) -> None:
    _send(
        f"{_LEFT}  LUCIFER ECONOMY  {_RIGHT}\n"
        f"\ue049  {title}\n{body}\n"
        f"{_LEFT}  COINS • COSMETICS • REWARDS  {_RIGHT}",
        clientid,
        _GOLD,
    )


def _player_name(clientid: int) -> str:
    """Get a display name for presentation only; never use it as identity."""
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
        _error("The coin economy is currently disabled.", clientid)
        return None
    if not is_v2_account_id(account_id):
        _error("A verified V2 account is required for a persistent wallet.", clientid)
        return None
    return account_id, _player_name(clientid)


def _registered_modules() -> tuple[Any, ...]:
    """Load and cache existing command modules without creating import cycles."""
    global _REGISTERED_MODULES
    if _REGISTERED_MODULES is not None:
        return _REGISTERED_MODULES
    try:
        from chathandle.chatcommands.commands import normal_commands, management, cheats, fun
        modules = (normal_commands, management, cheats, fun)
    except Exception:
        # Do not cache a failed import; a later call can retry after startup.
        _LOG.exception("Could not inspect installed command modules.")
        return ()
    _REGISTERED_MODULES = modules
    return modules


def _registered_names(module: Any) -> frozenset[str]:
    """Return cached normalized command names for a module."""
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
    """Clear command metadata caches after runtime command registration changes."""
    global _REGISTERED_MODULES
    _REGISTERED_MODULES = None
    _REGISTERED_NAME_CACHE.clear()


def has_command_permission(account_id: str | None, command: str, module: Any) -> bool:
    """Return whether the player already has role permission for a command.

    Permission lookup failures raise instead of silently treating the player as
    unprivileged; in that case the dispatcher will fail closed without charging.
    """
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
    """Find a registered implementation for one configured paid command."""
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
    """List enabled paid commands that currently have a registered handler."""
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
    """Refresh a saved effect on a live character, if one exists."""
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
        _LOG.exception("Could not refresh a purchased custom effect immediately.")


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
        _error("Wallet data is unavailable; check the server log.", clientid)
        return False
    _panel("YOUR WALLET", f"\ue048  BALANCE: {balance:,} COINS", clientid)
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
        _error("Daily reward could not be saved. Check the server log.", clientid)
        return False
    if not claimed:
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60
        _error(f"Daily reward already claimed. Try again in {hours}h {minutes}m.", clientid)
        return False
    _panel("DAILY REWARD CLAIMED", f"\ue047  +{reward:,} COINS\n\ue048  BALANCE: {balance:,}", clientid)
    return True


def _topcoins(arguments: list[str], clientid: int) -> bool:
    if arguments:
        _error("Usage: /topcoins", clientid)
        return False
    if not _economy_enabled():
        _error("The coin economy is currently disabled.", clientid)
        return False
    try:
        leaders = coins.top_balances(10)
    except Exception:
        _LOG.exception("Could not load the coin leaderboard.")
        _error("Coin leaderboard is unavailable; check the server log.", clientid)
        return False
    if not leaders:
        _panel("TOP COIN HOLDERS", "No coin balances yet. Use /daily to start earning.", clientid)
        return True
    lines = [f"{_LEFT}  TOP 10 COIN HOLDERS  {_RIGHT}"]
    for index, player in enumerate(leaders, 1):
        name = str(player.get("name", "Player"))[:22]
        lines.append(f"\ue047  {index}. {name}  •  {int(player['balance']):,}")
    lines.append(f"{_LEFT}  LUCIFER ECONOMY  {_RIGHT}")
    _send("\n".join(lines), clientid, _GOLD)
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
        _LOG.exception("Could not load supported custom effects for the shop listing.")
        effects = []
    body = (
        f"\ue047  /buytag <your text>  •  {tag_price:,} COINS\n"
        f"\ue047  /buyeffect <name>  •  {effect_price:,} COINS\n"
        "\ue043  One fixed price per successful change; no rarity tiers."
    )
    if effects:
        body += "\n\ue048  EFFECTS: " + ", ".join(effects)
    _panel("COSMETIC SHOP", body, clientid)
    return True


def _refund_cosmetic_purchase(
    account: str, price: int, name: str, clientid: int, item_label: str
) -> bool:
    """Refund a failed cosmetic purchase and make refund failures explicit."""
    try:
        coins.add_coins(account, price, name)
        return True
    except Exception:
        _LOG.exception(
            "CRITICAL: could not refund failed %s purchase to V2 account %s.",
            item_label,
            account,
        )
        _error(
            f"{item_label} could not be saved and the automatic refund failed. "
            "Contact an admin before retrying.",
            clientid,
        )
        return False


def _buytag(arguments: list[str], clientid: int, account_id: str | None) -> bool:
    if not arguments:
        _error("Usage: /buytag <your custom tag text>", clientid)
        return False
    wallet = _require_wallet(account_id, clientid)
    if wallet is None:
        return False
    account, name = wallet
    text = " ".join(arguments).strip()
    if not text or len(text) > 32 or any(ord(char) < 32 or ord(char) == 127 for char in text):
        _error("Tag text must be 1-32 printable characters.", clientid)
        return False

    try:
        from playersdata import pdata
        current = pdata.get_custom_tag(account)
        if current == text:
            _error("That tag is already set. No coins were charged.", clientid)
            return False
        price = _price("tagPrice", 100)
        new_balance = coins.spend_coins(account, price, name)
        if new_balance is None:
            _error(f"Not enough coins. /buytag costs {price:,}; use /me to check your balance.", clientid)
            return False
        try:
            saved = pdata.set_custom_tag(account, text)
        except Exception:
            _LOG.exception("Could not save purchased custom tag for %s.", account)
            saved = False
        if not saved:
            if _refund_cosmetic_purchase(account, price, name, clientid, "Tag"):
                _error("Tag could not be saved; your coins were refunded.", clientid)
            return False
        try:
            from spazmod import tag as tag_system
            tag_system.refresh_client_tag(clientid)
        except Exception:
            _LOG.exception("Tag was saved but could not refresh live; it applies on next spawn.")
        _panel("CUSTOM TAG ACTIVATED", f"\ue047  TAG: {text}\n\ue048  -{price:,} COINS\n\ue043  BALANCE: {new_balance:,}", clientid)
        return True
    except Exception:
        _LOG.exception("/buytag failed for account %s.", account)
        _error("Tag purchase failed. Check server logs for wallet or tag storage errors.", clientid)
        return False


def _buyeffect(arguments: list[str], clientid: int, account_id: str | None) -> bool:
    if len(arguments) != 1:
        _error("Usage: /buyeffect <supported_effect_name>", clientid)
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
            _error(f"Unsupported effect. Available: {supported}", clientid)
            return False
        if effect_name in pdata.get_custom_effects(account):
            _error("That effect is already assigned. No coins were charged.", clientid)
            return False
        price = _price("effectPrice", 200)
        new_balance = coins.spend_coins(account, price, name)
        if new_balance is None:
            _error(f"Not enough coins. /buyeffect costs {price:,}; use /me to check your balance.", clientid)
            return False
        try:
            saved = pdata.set_effect(effect_name, account)
        except Exception:
            _LOG.exception("Could not save purchased custom effect for %s.", account)
            saved = False
        if not saved:
            if _refund_cosmetic_purchase(account, price, name, clientid, "Effect"):
                _error("Effect could not be saved; your coins were refunded.", clientid)
            return False
        _refresh_player_effect(clientid, account)
        _panel("CUSTOM EFFECT UNLOCKED", f"\ue047  EFFECT: {effect_name}\n\ue048  -{price:,} COINS\n\ue043  BALANCE: {new_balance:,}", clientid)
        return True
    except Exception:
        _LOG.exception("/buyeffect failed for account %s.", account)
        _error("Effect purchase failed. Check server logs for wallet or effect storage errors.", clientid)
        return False


def _cmdshop(arguments: list[str], clientid: int) -> bool:
    if arguments:
        _error("Usage: /cmdshop", clientid)
        return False
    if not _economy_enabled():
        _error("The coin economy is currently disabled.", clientid)
        return False
    commands = available_paid_commands()
    if not commands:
        _panel("PAID COMMAND SHOP", "No configured paid commands are registered right now.", clientid)
        return True
    lines = [f"{_LEFT}  PAID COMMANDS  {_RIGHT}"]
    for command, price in commands:
        lines.append(f"\ue047  /{command}  •  {price:,} COINS")
    lines.append("\ue043  Coins are charged only when a command succeeds.")
    _send("\n".join(lines), clientid, _GOLD)
    return True


def ExcelCommand(command: str, arguments: list[str], clientid: int, accountid: str | None) -> bool:
    """Dispatch public economy commands; paid protected commands use a separate path."""
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


def execute_paid_command(command: str, arguments: list[str], clientid: int,
                         account_id: str | None, target_module: Any) -> bool:
    """Charge for one allowlisted command; refund when the action fails."""
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
        _error(f"/{command} is not available on this server.", clientid)
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
            _error("There is no active game for that command.", clientid)
            return False
        if command == "end" and not callable(getattr(activity, "end_game", None)):
            _error("The current activity cannot be ended as a game.", clientid)
            return False
        if command == "nv" and not hasattr(activity, "globalsnode"):
            _error("Night vision is unavailable in the current activity.", clientid)
            return False

    try:
        new_balance = coins.spend_coins(account, price, name)
    except Exception:
        _LOG.exception("Could not charge V2 account %s for /%s.", account, command)
        _error("Payment could not be processed. No command was run.", clientid)
        return False
    if new_balance is None:
        _error(f"Not enough coins. /{command} costs {price:,}; use /me to check your balance.", clientid)
        return False

    try:
        # Styled implementations for /end and /nv prevent these paid uses from
        # adding plain generic status text from the existing management module.
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
            _LOG.exception("CRITICAL: could not refund failed paid command /%s to %s.", command, account)
            _error("Command failed and automatic refund also failed. Contact an admin with the server log.", clientid)
            return False
        _error(f"/{command} failed. {price:,} coins were refunded.", clientid)
        return False

    body = f"\ue047  /{command} EXECUTED\n\ue048  -{price:,} COINS\n\ue043  BALANCE: {new_balance:,}"
    if command == "end":
        body += "\n\ue043  Ending the current game."
    elif command == "nv":
        body += "\n\ue043  Night vision enabled."
    _panel("PAID COMMAND SUCCESS", body, clientid)
    return True
