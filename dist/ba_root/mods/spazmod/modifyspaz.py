"""Character-spawn integration for role tags, player defaults, effects, and death flight.

Owner/Editor: Lucifer
"""

# ba_meta require api 9
from __future__ import annotations

import logging
from typing import Any

import babase
import bascenev1 as bs
from bascenev1lib.actor.spaz import Spaz

from spazmod import tag
from spazmod import spaz_effects

_LOG = logging.getLogger("ModifySpaz")
_SUPPORTED_BOMB_TYPES = frozenset({"normal", "sticky", "impact", "ice"})

# Death-flight tuning. The body is lifted for at most two seconds.
_DEAD_BODY_LIFETIME = 2.0
_ANTIGRAV_INTERVAL = 0.05

try:
    import setting as _setting
except Exception:
    _setting = None
    _LOG.warning(
        "Could not import setting.py; player defaults will use safe defaults.",
        exc_info=True,
    )


def _get_playermod_config() -> dict[str, Any]:
    """Return the playermod settings mapping, falling back safely on errors."""
    if _setting is None:
        return {}

    try:
        getter = getattr(_setting, "get_section", None)
        if callable(getter):
            section = getter("playermod")
            if isinstance(section, dict):
                return section
            _LOG.warning(
                "setting.get_section('playermod') returned %s; using safe defaults.",
                type(section).__name__,
            )
            return {}

        data_getter = getattr(_setting, "get_settings_data", None)
        data = data_getter() if callable(data_getter) else {}
        if not isinstance(data, dict):
            _LOG.warning(
                "setting.get_settings_data() returned %s; using safe defaults.",
                type(data).__name__,
            )
            return {}

        section = data.get("playermod", {})
        if isinstance(section, dict):
            return section
        _LOG.warning(
            "The 'playermod' settings section is %s instead of a dictionary; "
            "using safe defaults.",
            type(section).__name__,
        )
    except Exception:
        _LOG.exception("Could not read playermod settings; using safe defaults.")

    return {}


def _death_flight_enabled() -> bool:
    """Read the JSON toggle. Only the JSON boolean true enables the effect."""
    return _get_playermod_config().get("enable_death_flight", True) is True


def _apply_player_defaults(spaz: Any) -> None:
    """Apply configured gloves, bomb type, and bomb count to a new Spaz."""
    config = _get_playermod_config()

    gloves = config.get("default_boxing_gloves", True)
    if gloves is True:
        equip_gloves = getattr(spaz, "equip_boxing_gloves", None)
        if callable(equip_gloves):
            try:
                equip_gloves()
            except Exception:
                _LOG.exception("Could not equip default boxing gloves.")

    bomb_count = config.get("default_bomb_count", 2)
    if (
        isinstance(bomb_count, bool)
        or not isinstance(bomb_count, int)
        or not 1 <= bomb_count <= 10
    ):
        bomb_count = 2

    bomb_type = config.get("default_bomb", "normal")
    if isinstance(bomb_type, str):
        bomb_type = bomb_type.strip().lower()
    if bomb_type not in _SUPPORTED_BOMB_TYPES:
        bomb_type = "normal"

    # Set defaults too, so temporary power-ups return to configured values.
    try:
        spaz.default_bomb_count = bomb_count
        set_bomb_count = getattr(spaz, "set_bomb_count", None)
        if callable(set_bomb_count):
            set_bomb_count(bomb_count)
        else:
            spaz.bomb_count = bomb_count
    except Exception:
        _LOG.exception("Could not apply configured default bomb count.")

    try:
        spaz.default_bomb_type = bomb_type
        spaz.bomb_type_default = bomb_type
        spaz.bomb_type = bomb_type
    except Exception:
        _LOG.exception("Could not apply configured default bomb type %r.", bomb_type)


def _dead_body_flight_tick(node: Any, end_time: float) -> None:
    """Apply an upward impulse repeatedly without unsupported node attributes."""
    try:
        if node is None or not node.exists():
            return

        if bs.time() >= end_time:
            return

        position = node.position
        node.handlemessage(
            "impulse",
            position[0], position[1] + 0.5, position[2],
            0.0, 5.0, 0.0,
            3.0, 10.0, 0.0, 0,
            0.0, 5.0, 0.0,
        )

        bs.timer(
            _ANTIGRAV_INTERVAL,
            babase.CallPartial(
                _dead_body_flight_tick,
                node,
                end_time,
            ),
        )
    except Exception:
        # The actor can be removed during round/activity teardown.
        _LOG.debug("Death-flight tick stopped because the body is unavailable.", exc_info=True)


def _start_dead_body_antigrav(node: Any) -> None:
    """Start the short upward-flight effect on a valid dead-body node."""
    try:
        if node is None or not node.exists():
            return
        _dead_body_flight_tick(node, bs.time() + _DEAD_BODY_LIFETIME)
    except Exception:
        _LOG.exception("Could not start death-flight effect.")


def _install_death_flight_patch() -> None:
    """Patch Spaz death handling once to launch dead player bodies upward."""
    if getattr(Spaz, "_modifyspaz_death_flight_patched", False):
        return

    original_handlemessage = Spaz.handlemessage

    def _handlemessage_with_death_flight(self: Any, msg: Any) -> Any:
        should_fly = (
            isinstance(msg, bs.DieMessage)
            and _death_flight_enabled()
            and not getattr(msg, "immediate", False)
            and not getattr(self, "_dead", False)
            and getattr(self, "_player", None) is not None
        )

        result = original_handlemessage(self, msg)

        if should_fly:
            try:
                node = getattr(self, "node", None)
                if node is not None and node.exists():
                    bs.pushcall(babase.CallPartial(_start_dead_body_antigrav, node))
            except Exception:
                _LOG.exception("Could not schedule death-flight effect.")

        return result

    Spaz.handlemessage = _handlemessage_with_death_flight
    Spaz._modifyspaz_death_flight_patched = True


_install_death_flight_patch()


def main(spaz: Any, player: Any) -> None:
    """Attach configured overhead displays and apply saved/configured features."""
    try:
        node = getattr(spaz, "node", None)
        if node is None or not node.exists():
            return
    except Exception:
        # Independent saved effects still get a chance to handle the actor.
        _LOG.exception("Could not validate the spawned character node.")
    else:
        # Keep these systems independent: one failing must not block the others.
        try:
            _apply_player_defaults(spaz)
        except Exception:
            _LOG.exception("Could not apply configured player defaults.")

        try:
            old_tag_effect = getattr(spaz, "_roles_tags_effect", None)
            if old_tag_effect is None or getattr(old_tag_effect, "stopped", True):
                tag_effect = tag.addtag(node, player, actor=spaz)
                if tag_effect is not None:
                    spaz._roles_tags_effect = tag_effect
        except Exception:
            _LOG.exception("Role tag setup failed for a spawned character.")

    try:
        spaz_effects.apply_player_effects(spaz, player)
    except Exception:
        _LOG.exception("Custom effect setup failed for a spawned character.")
