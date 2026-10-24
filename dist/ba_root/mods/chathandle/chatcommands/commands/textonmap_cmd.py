# ba_meta require api 9
"""/txt - switch the on-map HUD live. Owner always; others via /addcmd <role> txt."""

from __future__ import annotations

from chathandle.chatcommands import handlers
from textonmap import manager

Commands = ["txt"]
CommandAliases: list[str] = []

_OK = (0.20, 1.0, 0.35)
_ERR = (1.0, 0.18, 0.18)
_FEATURES = {"bar": "team_bar", "bots": "falling_bots", "bg": "random_bg"}


def _show(clientid: int) -> None:
    st = manager.get_state()
    handlers.send_chat(
        "TEXT ON MAP\n"
        f"Active: {st['active']}\n"
        f"Available: {', '.join(manager.list_variants()) or 'none'}\n"
        f"Team bar: {'on' if st['team_bar'] else 'off'} | "
        f"Bots: {'on' if st['falling_bots'] else 'off'} | "
        f"BG: {'on' if st['random_bg'] else 'off'}\n"
        "Use: /txt <name> | /txt off | /txt bar|bots|bg on|off",
        clientid,
    )


def ExcelCommand(command: str, arguments: list[str], clientid: int, account_id) -> bool:
    if not arguments or arguments[0].casefold() in ("list", "status"):
        _show(clientid)
        return False

    first = arguments[0].casefold()

    if first in _FEATURES:
        if len(arguments) != 2 or arguments[1].casefold() not in ("on", "off"):
            handlers.send(f"Usage: /txt {first} on|off", clientid, color=_ERR)
            return False
        on = arguments[1].casefold() == "on"
        manager.set_feature(_FEATURES[first], on)
        handlers.send(f"{first} {'ON' if on else 'OFF'} (from next map)",
                      clientid, color=_OK)
        return True

    try:
        name = manager.switch(arguments[0])
    except ValueError:
        handlers.send(
            f"Unknown file. Available: {', '.join(manager.list_variants())}",
            clientid, color=_ERR)
        return False
    except RuntimeError as exc:
        handlers.send(str(exc), clientid, color=_ERR)
        return False

    handlers.send(f"Text on map -> {name}", clientid, color=_OK)
    return True
