# ba_meta require api 9
"""Ender-style solo opponent for BombSquad 1.8.0 / API 9.

Uses the Ender movement/attack loop supplied by the user, but exposes the
schedule_reconcile() and cleanup_activity() hooks used by custom_hooks.py.
The bot is a normal Spaz with boxing gloves explicitly equipped.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any

import babase
import bascenev1 as bs
from bascenev1lib.actor.spaz import Spaz

_LOG = logging.getLogger("Lucifer.SoloBot")

_DEFAULT_CONFIG: dict[str, Any] = {
    "enable": True,
    "name": "KING KAI",
    "character": "Mel",
    "nameColor": [1.0, 1.0, 1.0],
    "color": [0.0, 0.0, 0.0],
    "highlight": [0.0, 0.0, 0.0],
    "spawnSeconds": 0.35,
    "respawnSeconds": 0.65,
    "showAnnouncements": True,
    "spawnAnnouncement": (
        "\ue045  \ue042  {name} \ue042  \ue045\n"
        "\ue047  HAS ENTERED THE FIGHT  \ue047\n"
        "\ue048  PREPARE FOR A FIGHT  \ue043"
    ),
    "returnAnnouncement": (
        "\ue048  \ue042 {name} RETURNS! \ue042  \ue048\n"
        "\ue047  THE FIGHT CONTINUES  \ue047"
    ),
    "spawnAnnouncementColor": [0.2, 1.0, 0.75],
    "returnAnnouncementColor": [0.35, 0.75, 1.0],
}


def _color(value: Any, fallback: tuple[float, float, float]) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return fallback
    try:
        components = tuple(float(v) for v in value)
        if not all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in components):
            return fallback
        return components  # type: ignore[return-value]
    except (TypeError, ValueError, OverflowError):
        return fallback


def _load_config() -> dict[str, Any]:
    config = dict(_DEFAULT_CONFIG)
    try:
        import setting

        supplied = setting.get_section("soloBot")
        if isinstance(supplied, dict):
            for key in config:
                if key in supplied:
                    config[key] = supplied[key]
    except Exception:
        _LOG.exception("Could not read soloBot settings; using defaults.")

    if not isinstance(config["enable"], bool):
        config["enable"] = True
    for key, default in (("name", "KING KAI"), ("character", "Mel")):
        if not isinstance(config[key], str) or not config[key].strip():
            config[key] = default
        config[key] = config[key][:48]

    config["nameColor"] = _color(config["nameColor"], (1.0, 1.0, 1.0))
    config["color"] = _color(config["color"], (0.0, 0.0, 0.0))
    config["highlight"] = _color(config["highlight"], (0.0, 0.0, 0.0))
    config["spawnAnnouncementColor"] = _color(
        config["spawnAnnouncementColor"], (0.2, 1.0, 0.75)
    )
    config["returnAnnouncementColor"] = _color(
        config["returnAnnouncementColor"], (0.35, 0.75, 1.0)
    )

    for key, default, minimum, maximum in (
        ("spawnSeconds", 0.35, 0.0, 5.0),
        ("respawnSeconds", 0.65, 0.1, 15.0),
    ):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = default
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            value = default
        if not math.isfinite(value):
            value = default
        config[key] = max(minimum, min(maximum, value))

    if not isinstance(config.get("showAnnouncements"), bool):
        config["showAnnouncements"] = True
    for key in ("spawnAnnouncement", "returnAnnouncement"):
        if not isinstance(config.get(key), str):
            config[key] = _DEFAULT_CONFIG[key]
        config[key] = config[key][:500]
    return config


_CONFIG = _load_config()


def refresh_config() -> dict[str, Any]:
    """Reload soloBot configuration from mods/setting.json."""
    global _CONFIG
    _CONFIG = _load_config()
    return dict(_CONFIG)


class Bot:
    """Small wrapper retaining the supplied snippet's direct-Spaz controls."""

    def __init__(self, activity: Any, position: tuple[float, float, float]) -> None:
        self.activity = activity
        self.bot = Spaz(
            color=_CONFIG["color"],
            highlight=_CONFIG["highlight"],
            character=_CONFIG["character"],
        )
        self.bot.handlemessage(bs.StandMessage(position, 0))
        self.node = self.bot.node
        self.node.name = _CONFIG["name"]
        self.node.name_color = _CONFIG["nameColor"]

        # Explicitly equip boxing gloves (the engine applies them to both hands).
        self.bot.equip_boxing_gloves()

        self._think_timer: Any = None
        self._protect_timer: Any = None
        self._last_skill_time = -999.0
        self._stopped = False

    def start(self) -> None:
        """Start the same 0.15-second think and close-range protection loops."""
        self._think_timer = bs.Timer(0.15, bs.WeakCallStrict(self._think), repeat=True)
        self._protect_timer = bs.Timer(0.01, bs.WeakCallStrict(self._protect), repeat=True)

    def on(self, index: int) -> None:
        """Use the supplied code's quick press-then-release input sequence."""
        if self._stopped or not self.node or not self.node.exists():
            return
        names = ["jump", "bomb", "pickup", "punch"]
        if not 0 <= index < len(names):
            return
        for action in ("press", "release"):
            try:
                getattr(self.bot, "on_" + names[index] + "_" + action)()
            except Exception:
                _LOG.debug("Ender input %s/%s failed.", names[index], action, exc_info=True)

    def move(self, x: float, z: float) -> None:
        if self._stopped or not self.node or not self.node.exists():
            return
        self.bot.on_move_left_right(x)
        self.bot.on_move_up_down(z)

    def on_run(self, value: float) -> None:
        if not self._stopped and self.node and self.node.exists():
            self.bot.on_run(value)

    def _target(self) -> Any | None:
        if self._stopped or not self.node or not self.node.exists():
            return None
        try:
            my_position = self.node.position
            targets = [
                node for node in bs.getnodes()
                if node.exists()
                and node.getnodetype() == "spaz"
                and node is not self.node
            ]
            if not targets:
                return None
            return min(targets, key=lambda node: math.dist(my_position, node.position))
        except Exception:
            _LOG.debug("Ender target scan failed.", exc_info=True)
            return None

    def skill(self) -> None:
        """Punch now, then attempt pickup shortly afterward, as in the source."""
        if self._stopped:
            return
        self._last_skill_time = float(bs.time())
        self.on(3)
        try:
            bs.timer(0.05, babase.CallStrict(self.on, 2))
        except Exception:
            _LOG.debug("Could not schedule Ender pickup input.", exc_info=True)

    def _protect(self) -> None:
        if self._stopped or not self.node or not self.node.exists():
            return
        target = self._target()
        if target is None:
            return
        try:
            if math.dist(self.node.position, target.position) < 1.6:
                self.skill()
        except Exception:
            _LOG.debug("Ender close-range check failed.", exc_info=True)

    def _think(self) -> None:
        if self._stopped or not self.node or not self.node.exists():
            return
        target = self._target()
        if target is None:
            self.on_run(0.0)
            self.move(0.0, 0.0)
            return
        try:
            dx = float(target.position[0]) - float(self.node.position[0])
            dz = float(target.position[2]) - float(self.node.position[2])
            length = math.hypot(dx, dz)
            if length <= 1e-6:
                return
            dx /= length
            dz /= length
            self.on_run(1.0)
            self.move(dx, -dz)
            if math.dist(self.node.position, target.position) < 1.6:
                self.skill()
        except Exception:
            _LOG.debug("Ender movement update failed.", exc_info=True)

    def is_alive(self) -> bool:
        if self._stopped:
            return False
        try:
            return bool(self.node and self.node.exists() and self.bot.is_alive())
        except Exception:
            return bool(self.node and self.node.exists())

    def remove(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        # Releasing timer references ends the repeating BaseTimer callbacks.
        self._think_timer = None
        self._protect_timer = None
        try:
            if self.bot is not None:
                self.bot.handlemessage(bs.DieMessage())
        except Exception:
            _LOG.debug("Ender bot cleanup failed.", exc_info=True)


def _is_game_activity(activity: Any) -> bool:
    try:
        if not isinstance(activity, bs.GameActivity):
            return False
        has_ended = getattr(activity, "has_ended", None)
        return not callable(has_ended) or not bool(has_ended())
    except Exception:
        return False


def _announce(activity: Any, returning: bool) -> None:
    if not _CONFIG["showAnnouncements"]:
        return
    key = "returnAnnouncement" if returning else "spawnAnnouncement"
    color_key = "returnAnnouncementColor" if returning else "spawnAnnouncementColor"
    try:
        message = _CONFIG[key].replace("{name}", _CONFIG["name"])
        bs.broadcastmessage(message, color=_CONFIG[color_key], transient=True)
    except Exception:
        _LOG.exception("Could not broadcast Ender spawn announcement.")


def _remove_from_activity(activity: Any) -> None:
    bot = getattr(activity, "_lucifer_solo_bot_instance", None)
    if bot is not None:
        try:
            with activity.context:
                bot.remove()
        except Exception:
            _LOG.debug("Could not remove Ender from activity.", exc_info=True)
    setattr(activity, "_lucifer_solo_bot_instance", None)
    setattr(activity, "_lucifer_solo_bot_respawn_after", 0.0)


def _schedule_check(activity: Any, token: int, delay: float) -> None:
    if not _is_game_activity(activity):
        return
    try:
        with activity.context:
            bs.timer(
                max(0.1, float(delay)),
                babase.CallPartial(_reconcile, activity, token),
            )
    except Exception:
        _LOG.exception("Could not schedule Ender player-count check.")


def _reconcile(activity: Any, token: int) -> None:
    if not _CONFIG["enable"] or not _is_game_activity(activity):
        return
    if getattr(activity, "_lucifer_solo_bot_token", None) != token:
        return
    try:
        if bs.get_foreground_host_activity() is not activity:
            return
    except Exception:
        return

    try:
        count = len(getattr(activity, "players", ()))
    except Exception:
        _LOG.exception("Could not read player count for Ender.")
        _schedule_check(activity, token, 5.0)
        return

    bot = getattr(activity, "_lucifer_solo_bot_instance", None)

    # Match the supplied code: keep the bot with at most one human player.
    if count > 1:
        if bot is not None:
            _remove_from_activity(activity)
        return

    now = float(bs.time())
    respawn_after = float(getattr(activity, "_lucifer_solo_bot_respawn_after", 0.0) or 0.0)
    if bot is not None and bot.is_alive():
        _schedule_check(activity, token, 5.0)
        return
    if bot is not None:
        _remove_from_activity(activity)
        setattr(activity, "_lucifer_solo_bot_respawn_after", now + _CONFIG["respawnSeconds"])
        _schedule_check(activity, token, max(0.1, _CONFIG["respawnSeconds"]))
        return
    if now < respawn_after:
        _schedule_check(activity, token, respawn_after - now)
        return

    # Use the nearest existing Spaz node's location, as in the supplied code.
    try:
        with activity.context:
            targets = [
                node for node in bs.getnodes()
                if node.exists() and node.getnodetype() == "spaz"
            ]
            if targets:
                chosen_node = random.choice(targets)
                raw_position = chosen_node.position
                position = (float(raw_position[0]), float(raw_position[1]), float(raw_position[2]))
            else:
                position = (0.0, 0.1, 0.0)
            instance = Bot(activity, position)
            instance.start()
            setattr(activity, "_lucifer_solo_bot_instance", instance)
            _announce(activity, bool(getattr(activity, "_lucifer_solo_bot_announced", False)))
            setattr(activity, "_lucifer_solo_bot_announced", True)
            _LOG.info("Spawned solo Ender-style bot %s with boxing gloves.", _CONFIG["name"])
    except Exception:
        _LOG.exception("Could not spawn Ender-style solo bot.")
    _schedule_check(activity, token, 5.0)


def schedule_reconcile(activity: Any, delay: float = 0.6) -> None:
    """Entry point expected by mods/custom_hooks.py after player/activity events."""
    if not _CONFIG["enable"] or not _is_game_activity(activity):
        return
    try:
        token = int(getattr(activity, "_lucifer_solo_bot_token", 0)) + 1
        setattr(activity, "_lucifer_solo_bot_token", token)
        with activity.context:
            bs.timer(
                max(0.1, float(delay)),
                babase.CallPartial(_reconcile, activity, token),
            )
    except Exception:
        _LOG.exception("Could not schedule Ender solo-bot reconciliation.")


def cleanup_activity(activity: Any) -> None:
    """Entry point expected by mods/custom_hooks.py before an activity ends."""
    try:
        setattr(
            activity,
            "_lucifer_solo_bot_token",
            int(getattr(activity, "_lucifer_solo_bot_token", 0)) + 1,
        )
    except Exception:
        pass
    try:
        _remove_from_activity(activity)
    except Exception:
        _LOG.exception("Ender solo-bot cleanup failed.")
