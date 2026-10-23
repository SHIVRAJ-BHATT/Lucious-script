# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Short announcements for powerful player-versus-player punches (API 9)."""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any

import bascenev1 as bs
from bascenev1lib.actor.popuptext import PopupText

_LOG = logging.getLogger("Lucifer.HitMessages")

# These are HitMessage.magnitude values, not damage points. They are intended
# as reachable starting thresholds and can be tuned after live-server testing.
POWER_HIT_MESSAGE_MAGNITUDE = 160.0
SMASH_HIT_MESSAGE_MAGNITUDE = 280.0
EXTREME_HIT_MESSAGE_MAGNITUDE = 420.0
MESSAGE_PAIR_COOLDOWN = 0.8

# Short labels keep popups readable during fast combat. Glyphs frame each tier.
_POWER_WORDS = (
    "POW", "BONK", "WHAM", "OOF", "THWACK", "BAM", "KAPOW", "ZAP", "CLASH"
)
_SMASH_WORDS = (
    "SMASH", "CRUSHED", "BRUTAL", "SLAM", "WRECKED", "SHATTERED", "K.O."
)
_EXTREME_WORDS = (
    "RIP", "DEAD", "FATAL", "ANNIHILATED", "OBLITERATED", "FINISHED", "DESTROYED"
)

_LAST_HIT_MESSAGE: dict[tuple[tuple[str, int], tuple[str, int]], float] = {}
_SETTING_READ_WARNED = False
_SETTINGS_CACHE_TTL = 1.0
_SETTINGS_CACHE_VALUE: bool | None = None
_SETTINGS_CACHE_CHECKED_AT = 0.0


def _finite_number(value: Any, default: float = 0.0) -> float:
    """Return a finite float or a safe fallback for unexpected values."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _messages_enabled() -> bool:
    """Read the hitmessages setting with a short cache for the hot hit path.

    The one-second TTL avoids consulting the settings module for every impact,
    while still allowing runtime setting changes to take effect promptly.
    Call refresh_hit_message_settings() to apply a change immediately.
    """
    global _SETTING_READ_WARNED
    global _SETTINGS_CACHE_VALUE, _SETTINGS_CACHE_CHECKED_AT

    now = time.monotonic()
    cached = _SETTINGS_CACHE_VALUE
    if cached is not None and now - _SETTINGS_CACHE_CHECKED_AT < _SETTINGS_CACHE_TTL:
        return cached

    enabled = True
    try:
        import setting

        getter = getattr(setting, "get_bool", None)
        if callable(getter):
            value = getter("hitmessages", True)
            enabled = value if isinstance(value, bool) else True
        _SETTING_READ_WARNED = False
    except Exception:
        if not _SETTING_READ_WARNED:
            _SETTING_READ_WARNED = True
            _LOG.exception("Could not read hitmessages setting; defaulting to enabled.")

    _SETTINGS_CACHE_VALUE = enabled
    _SETTINGS_CACHE_CHECKED_AT = now
    return enabled


def refresh_hit_message_settings() -> bool:
    """Force a fresh read of the hitmessages toggle and return its value."""
    global _SETTINGS_CACHE_VALUE, _SETTINGS_CACHE_CHECKED_AT
    _SETTINGS_CACHE_VALUE = None
    _SETTINGS_CACHE_CHECKED_AT = 0.0
    return _messages_enabled()


def _get_source_player(message: Any, victim_player: Any) -> Any | None:
    """Resolve the attacker using the API 9 player type expected by HitMessage."""
    getter = getattr(message, "get_source_player", None)
    if not callable(getter) or victim_player is None:
        return None
    try:
        return getter(type(victim_player))
    except Exception:
        _LOG.debug("Could not resolve HitMessage source player.", exc_info=True)
        return None


def _player_name(player: Any) -> str:
    """Return a short, single-line player name suitable for a broadcast."""
    try:
        session_player = getattr(player, "sessionplayer", None)
    except Exception:
        session_player = None

    for candidate in (player, session_player):
        if candidate is None:
            continue
        getter = getattr(candidate, "getname", None)
        if not callable(getter):
            continue
        try:
            name = getter(full=False, icon=False)
        except TypeError:
            try:
                name = getter()
            except Exception:
                continue
        except Exception:
            continue
        if isinstance(name, str) and name.strip():
            # Prevent newlines/control characters from making multi-line messages.
            return " ".join(name.replace("\n", " ").replace("\r", " ").split())[:32]
    return "Player"


def _player_identity(player: Any) -> tuple[str, int]:
    """Build a cooldown identity from client ID, falling back to object identity."""
    try:
        session_player = getattr(player, "sessionplayer", None)
    except Exception:
        session_player = None

    for candidate in (player, session_player):
        if candidate is None:
            continue
        try:
            device = getattr(candidate, "inputdevice", None)
            client_id = getattr(device, "client_id", None)
            if (
                isinstance(client_id, int)
                and not isinstance(client_id, bool)
                and client_id >= 0
            ):
                return ("client", client_id)
        except Exception:
            pass
    return ("object", id(player))


def _allow_announcement(attacker: Any, victim: Any) -> bool:
    """Apply a per-attacker/victim cooldown and bound the cache size."""
    now = time.monotonic()
    pair = (_player_identity(attacker), _player_identity(victim))
    last = _LAST_HIT_MESSAGE.get(pair, -1.0e9)
    if now - last < MESSAGE_PAIR_COOLDOWN:
        return False
    _LAST_HIT_MESSAGE[pair] = now

    if len(_LAST_HIT_MESSAGE) > 512:
        cutoff = now - 10.0
        stale = [key for key, stamp in _LAST_HIT_MESSAGE.items() if stamp < cutoff]
        for key in stale:
            _LAST_HIT_MESSAGE.pop(key, None)
        if len(_LAST_HIT_MESSAGE) > 512:
            oldest = sorted(_LAST_HIT_MESSAGE.items(), key=lambda item: item[1])
            for key, _stamp in oldest[: len(_LAST_HIT_MESSAGE) - 384]:
                _LAST_HIT_MESSAGE.pop(key, None)
    return True


def _announce_for_hit(victim_spaz: Any, message: Any) -> None:
    """Announce qualifying punch impacts between two distinct players.

    Cheap message filters run before settings, player-name, and popup work since
    this function sits on the frequent PlayerSpaz.handlemessage path.
    """
    if not isinstance(message, bs.HitMessage):
        return
    if getattr(message, "hit_type", None) != "punch":
        return

    magnitude = _finite_number(getattr(message, "magnitude", 0.0))
    if magnitude < POWER_HIT_MESSAGE_MAGNITUDE:
        return
    if not _messages_enabled():
        return

    victim_player = getattr(victim_spaz, "_player", None)
    if victim_player is None:
        return

    attacker_player = _get_source_player(message, victim_player)
    if attacker_player is None or attacker_player is victim_player:
        return

    # Do not announce hits when either player reference has expired.
    for player in (attacker_player, victim_player):
        exists = getattr(player, "exists", None)
        if callable(exists):
            try:
                if not exists():
                    return
            except Exception:
                return

    if magnitude >= EXTREME_HIT_MESSAGE_MAGNITUDE:
        tier = "extreme"
        word_pool = _EXTREME_WORDS
        color = (1.0, 0.20, 0.24)
        popup_scale = 0.84
    elif magnitude >= SMASH_HIT_MESSAGE_MAGNITUDE:
        tier = "smash"
        word_pool = _SMASH_WORDS
        color = (1.0, 0.43, 0.20)
        popup_scale = 0.76
    else:
        tier = "power"
        word_pool = _POWER_WORDS
        color = (0.30, 0.92, 1.0)
        popup_scale = 0.68

    # Validate the live victim node and position before consuming cooldown; hits
    # during spawn/despawn transitions should not suppress a later valid popup.
    try:
        node = getattr(victim_spaz, "node", None)
        if node is None or not node.exists():
            return

        body_position = getattr(node, "torso_position", None)
        if body_position is None:
            body_position = getattr(node, "position", None)
        if body_position is None or len(body_position) < 3:
            return

        position = tuple(
            _finite_number(body_position[index], float("nan")) for index in range(3)
        )
        if not all(math.isfinite(component) for component in position):
            return
    except Exception:
        _LOG.debug("Could not resolve victim position for hit popup.", exc_info=True)
        return

    # Avoid name lookups, string formatting, and PopupText construction if this
    # attacker/victim pair is still inside its cooldown window.
    if not _allow_announcement(attacker_player, victim_player):
        return

    # Random selection happens only after filtering and cooldown checks, keeping
    # the frequent hit path inexpensive. Tier-specific glyph framing provides a
    # distinct visual style without adding multiline or long text.
    word = random.choice(word_pool)
    if tier == "extreme":
        text = f"\ue062  \ue048 ✦ {word} ✦ \ue048  \ue062"
    elif tier == "smash":
        text = f"\ue048  \ue049 ◆ {word} ◆ \ue049  \ue048"
    else:
        text = f"\ue048  \ue043 ✦ {word} ✦ \ue043  \ue048"

    # PopupText animates upward and fades out on its own; no repeating timer is
    # attached to the character. Cooldown bounds popup creation during combat.
    try:
        popup_text = PopupText(
            text=text,
            position=(position[0], position[1] + 0.35, position[2]),
            color=color,
            random_offset=0.12,
            scale=popup_scale,
        )
        popup_text.autoretain()
    except Exception:
        _LOG.exception("Could not show a powerful-hit popup on the victim.")


def install() -> bool:
    """Install an idempotent PlayerSpaz wrapper for hit announcements."""
    try:
        from bascenev1lib.actor.playerspaz import PlayerSpaz

        original = PlayerSpaz.handlemessage
        if getattr(original, "_lucifer_hit_messages_wrapped", False):
            _LOG.info("Powerful-punch hit messages are already installed.")
            return True

        def wrapped_handlemessage(
            self: Any, message: Any, *args: Any, **kwargs: Any
        ) -> Any:
            # Let the engine apply damage/knockback normally and exactly once.
            result = original(self, message, *args, **kwargs)
            if isinstance(message, bs.HitMessage):
                try:
                    _announce_for_hit(self, message)
                except Exception:
                    _LOG.exception("Could not process a powerful-punch hit message.")
            return result

        wrapped_handlemessage._lucifer_hit_messages_wrapped = True  # type: ignore[attr-defined]
        # Preserve both the current and legacy metal-wrapper markers. The metal
        # effect uses _lucifer_strong_punch_metal_wrapped; preserving that exact
        # marker prevents a later install() call from stacking a duplicate hook.
        wrapped_handlemessage._lucifer_strong_punch_metal_wrapped = bool(  # type: ignore[attr-defined]
            getattr(original, "_lucifer_strong_punch_metal_wrapped", False)
        )
        wrapped_handlemessage._lucifer_metal_hit_effect_wrapped = bool(  # type: ignore[attr-defined]
            getattr(original, "_lucifer_metal_hit_effect_wrapped", False)
            or getattr(original, "_lucifer_strong_punch_metal_wrapped", False)
        )
        PlayerSpaz.handlemessage = wrapped_handlemessage  # type: ignore[method-assign]
        _LOG.info(
            "Installed powerful-punch hit messages (enabled=%s, thresholds=%.1f/%.1f/%.1f).",
            _messages_enabled(),
            POWER_HIT_MESSAGE_MAGNITUDE,
            SMASH_HIT_MESSAGE_MAGNITUDE,
            EXTREME_HIT_MESSAGE_MAGNITUDE,
        )
        return True
    except Exception:
        _LOG.exception("Could not install powerful-punch hit messages.")
        return False
