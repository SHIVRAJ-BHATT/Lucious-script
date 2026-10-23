# ba_meta require api 9
"""Snowfall particle effect for BombSquad 1.8.0+ / API 9.

The effect is configured by ``mods/setting.json``::

    "snowfall": {
        "enable": true,
        "size": 0.75,
        "fallSpeed": 50.0,
        "particleCount": 10
    }

Runtime code can call ``set_runtime_enabled(True/False)`` to override the
setting for the current server process only. That override is not written to
setting.json and resets on server restart.

Owner/editor: Lucifer.
"""

from __future__ import annotations

import logging
import math
import random
import time
import weakref
from typing import Any

import bascenev1 as bs

_LOG = logging.getLogger("Lucifer.Snowfall")
_INSTALLED = False
_RUNTIME_OVERRIDE: bool | None = None
_ACTIVE_ACTIVITIES: weakref.WeakSet[Any] = weakref.WeakSet()

# Reduce timer callbacks from 50/sec to 25/sec. This materially reduces
# particle-emission work during gameplay; each burst still uses the configured
# particle count and the original position/velocity math.
_INTERVAL = 0.04
_OPTIONS_CACHE_SECONDS = 1.0
_DEFAULT_OPTIONS: tuple[float, float, int] = (0.75, 50.0, 10)
_OPTIONS_CACHE: tuple[float, float, int] | None = None
_OPTIONS_CACHE_AT = 0.0
_OPTIONS_READ_WARNED = False
_SETTING_READ_WARNED = False
_LAST_EMIT_ERROR_LOG_AT = -1.0e9
_EMIT_ERROR_LOG_INTERVAL = 30.0
_SUPPORTED_OPTION_SIZE = (0.1, 5.0)
_SUPPORTED_FALL_SPEED = (1.0, 150.0)
_SUPPORTED_PARTICLE_COUNT = (1, 100)


def _setting_enabled() -> bool:
    """Read the persistent enable flag, failing closed on errors."""
    global _SETTING_READ_WARNED
    try:
        from setting import get_section

        section = get_section("snowfall")
        value = section.get("enable", True)
        _SETTING_READ_WARNED = False
        return value if isinstance(value, bool) else False
    except Exception:
        if not _SETTING_READ_WARNED:
            _SETTING_READ_WARNED = True
            _LOG.warning(
                "Could not read snowfall enable setting; defaulting to disabled.",
                exc_info=True,
            )
        return False


def is_enabled() -> bool:
    """Return the effective setting, including any temporary runtime override."""
    if _RUNTIME_OVERRIDE is not None:
        return _RUNTIME_OVERRIDE
    return _setting_enabled()


def _load_snowfall_options() -> tuple[float, float, int]:
    """Read and validate the configurable size, fall speed, and count."""
    from setting import get_section

    section = get_section("snowfall")
    size = section.get("size", _DEFAULT_OPTIONS[0])
    fall_speed = section.get("fallSpeed", _DEFAULT_OPTIONS[1])
    particle_count = section.get("particleCount", _DEFAULT_OPTIONS[2])

    if (
        isinstance(size, bool)
        or not isinstance(size, (int, float))
        or not math.isfinite(float(size))
        or not _SUPPORTED_OPTION_SIZE[0] <= size <= _SUPPORTED_OPTION_SIZE[1]
    ):
        size = _DEFAULT_OPTIONS[0]

    if (
        isinstance(fall_speed, bool)
        or not isinstance(fall_speed, (int, float))
        or not math.isfinite(float(fall_speed))
        or not _SUPPORTED_FALL_SPEED[0] <= fall_speed <= _SUPPORTED_FALL_SPEED[1]
    ):
        fall_speed = _DEFAULT_OPTIONS[1]

    if (
        isinstance(particle_count, bool)
        or not isinstance(particle_count, int)
        or not _SUPPORTED_PARTICLE_COUNT[0]
        <= particle_count
        <= _SUPPORTED_PARTICLE_COUNT[1]
    ):
        particle_count = _DEFAULT_OPTIONS[2]

    return float(size), float(fall_speed), particle_count


def _snowfall_options() -> tuple[float, float, int]:
    """Return validated options, caching configuration for one second.

    The snowfall timer fires every 40 ms. Caching prevents configuration access
    on every particle burst and allows setting changes to take effect within
    approximately one second.
    """
    global _OPTIONS_CACHE, _OPTIONS_CACHE_AT, _OPTIONS_READ_WARNED

    now = time.monotonic()
    cached = _OPTIONS_CACHE
    if cached is not None and now - _OPTIONS_CACHE_AT < _OPTIONS_CACHE_SECONDS:
        return cached

    try:
        options = _load_snowfall_options()
        _OPTIONS_READ_WARNED = False
    except Exception:
        if not _OPTIONS_READ_WARNED:
            _LOG.warning(
                "Could not read snowfall particle settings; using safe defaults.",
                exc_info=True,
            )
        _OPTIONS_READ_WARNED = True
        options = _DEFAULT_OPTIONS

    _OPTIONS_CACHE = options
    _OPTIONS_CACHE_AT = now
    return options


def refresh_snowfall_settings() -> tuple[float, float, int]:
    """Invalidate the option cache and read the settings immediately.

    Call after changing snowfall settings at runtime. Otherwise changes are
    picked up automatically within approximately one second.
    """
    global _OPTIONS_CACHE, _OPTIONS_CACHE_AT, _OPTIONS_READ_WARNED
    _OPTIONS_CACHE = None
    _OPTIONS_CACHE_AT = 0.0
    _OPTIONS_READ_WARNED = False
    return _snowfall_options()


def _emit_snowfall() -> None:
    """Emit one configurable snowfall burst in the current activity context."""
    global _LAST_EMIT_ERROR_LOG_AT
    try:
        size, fall_speed, particle_count = _snowfall_options()
        # Preserve the original particle position and horizontal velocity math.
        px = -10.0 + (random.random() * 30.0)
        py = 15.0
        pz = -10.0 + (random.random() * 30.0)
        position = (px, py, pz)

        direction = -1.0 if px > 0.0 else 1.0
        vx = (-5.0 + (random.random() * 30.0)) * direction
        vz = (-5.0 + (random.random() * 30.0)) * direction
        velocity = (vx, -fall_speed, vz)
        particle_scale = (1.0 + random.random()) * size

        bs.emitfx(
            position=position,
            velocity=velocity,
            count=particle_count,
            scale=particle_scale,
            spread=0.0,
            chunk_type="spark",
        )
    except Exception:
        # This callback can run 50 times/sec; rate-limit repeated identical-path
        # failures so a broken effect does not overwhelm terminal output.
        now = time.monotonic()
        if now - _LAST_EMIT_ERROR_LOG_AT >= _EMIT_ERROR_LOG_INTERVAL:
            _LAST_EMIT_ERROR_LOG_AT = now
            _LOG.warning("A snowfall particle burst could not be emitted.", exc_info=True)


def _activity_is_running(activity: Any) -> bool:
    """Return whether an activity is eligible to host snowfall."""
    try:
        if bool(getattr(activity, "expired", False)):
            return False
        has_ended = getattr(activity, "has_ended", None)
        if callable(has_ended) and has_ended():
            return False
        is_transitioning_out = getattr(activity, "is_transitioning_out", None)
        if callable(is_transitioning_out) and is_transitioning_out():
            return False
        return True
    except Exception:
        return False


def _start_activity(activity: Any) -> bool:
    """Start snowfall in a transitioned-in game activity, if not already active."""
    if not is_enabled() or not isinstance(activity, bs.GameActivity):
        return False
    if not _activity_is_running(activity):
        return False

    try:
        existing = getattr(activity, "_lucifer_snowfall_timer", None)
        if existing is not None:
            _ACTIVE_ACTIVITIES.add(activity)
            return True

        # Timer and emitted chunks must belong to this activity's scene context.
        with activity.context:
            timer = bs.BaseTimer(_INTERVAL, _emit_snowfall, repeat=True)
        setattr(activity, "_lucifer_snowfall_timer", timer)
        _ACTIVE_ACTIVITIES.add(activity)
        _LOG.info("Snowfall started for %s.", type(activity).__name__)
        return True
    except Exception:
        _LOG.exception("Could not start snowfall for %s.", type(activity).__name__)
        return False


def _stop_activity(activity: Any, *, forget: bool = False) -> None:
    """Stop the activity timer by releasing it; optionally remove its registry entry."""
    try:
        if getattr(activity, "_lucifer_snowfall_timer", None) is not None:
            # BaseTimer is context-owned; releasing the last reference cancels it.
            setattr(activity, "_lucifer_snowfall_timer", None)
            _LOG.info("Snowfall stopped for %s.", type(activity).__name__)
    except Exception:
        _LOG.warning("Could not stop snowfall timer cleanly.", exc_info=True)
    if forget:
        try:
            _ACTIVE_ACTIVITIES.discard(activity)
        except Exception:
            _LOG.debug("Could not remove expired activity from snowfall registry.", exc_info=True)


def snowfall_generator(
    activity: Any,
    count: int = 15,
    fall_speed: float = 0.2,
    scale: float = 0.5,
) -> bool:
    """Compatibility entry point matching the supplied API 7 method signature.

    The legacy implementation used fixed particle values, so the parameters are
    retained for compatibility and intentionally do not alter the effect.
    """
    del count, fall_speed, scale
    return _start_activity(activity)


def _current_game_activity() -> Any | None:
    """Return the current live game activity, even if hooks missed its entry.

    When snowfall is disabled in setting.json, custom_hooks can intentionally
    skip installing our transition hooks at startup. If `/snow` is then used
    midway through a match, `install()` is called after that match's transition
    event has already happened and `_ACTIVE_ACTIVITIES` may be empty. Discover
    the foreground activity directly so runtime toggles apply to the current
    match rather than waiting for the next one.
    """
    getters = (
        getattr(bs, "get_foreground_host_activity", None),
        getattr(bs, "getactivity", None),
    )
    for getter in getters:
        if not callable(getter):
            continue
        try:
            activity = getter()
        except Exception:
            continue
        try:
            if isinstance(activity, bs.GameActivity) and _activity_is_running(activity):
                return activity
        except Exception:
            continue
    return None


def _activities_to_update() -> list[Any]:
    """Return registered activities plus the current game, if any."""
    activities = list(_ACTIVE_ACTIVITIES)
    current = _current_game_activity()
    if current is not None:
        try:
            _ACTIVE_ACTIVITIES.add(current)
        except Exception:
            _LOG.debug("Could not register the current game activity for snowfall.", exc_info=True)
        if not any(activity is current for activity in activities):
            activities.append(current)
    return activities


def set_runtime_enabled(enabled: bool) -> bool:
    """Toggle snowfall immediately for the current match and future matches.

    This changes only the in-process override, not setting.json. The foreground
    game activity is discovered explicitly because the command may be used after
    the current activity's transition-in hook has already fired.
    """
    global _RUNTIME_OVERRIDE
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    _RUNTIME_OVERRIDE = enabled

    activities = _activities_to_update()
    for activity in activities:
        if enabled:
            _start_activity(activity)
        else:
            _stop_activity(activity, forget=False)

    _LOG.info(
        "Snowfall runtime override set to %s; updated %d active game activity/activities.",
        enabled,
        len(activities),
    )
    return enabled


def clear_runtime_override() -> bool:
    """Clear the temporary override and apply setting.json immediately."""
    global _RUNTIME_OVERRIDE
    _RUNTIME_OVERRIDE = None
    enabled = is_enabled()

    activities = _activities_to_update()
    for activity in activities:
        if enabled:
            _start_activity(activity)
        else:
            _stop_activity(activity, forget=False)

    _LOG.info(
        "Snowfall runtime override cleared; setting.json enabled=%s; updated %d active game activity/activities.",
        enabled,
        len(activities),
    )
    return enabled


def install() -> bool:
    """Install API 9 game-activity lifecycle hooks exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        activity_class = bs.GameActivity
        original_in = activity_class.on_transition_in
        if not getattr(original_in, "_lucifer_snowfall_wrapped", False):
            def wrapped_transition_in(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = original_in(self, *args, **kwargs)
                try:
                    _ACTIVE_ACTIVITIES.add(self)
                    if is_enabled():
                        _start_activity(self)
                except Exception:
                    _LOG.exception("Could not initialize snowfall after game transition-in.")
                return result

            wrapped_transition_in._lucifer_snowfall_wrapped = True  # type: ignore[attr-defined]
            activity_class.on_transition_in = wrapped_transition_in  # type: ignore[method-assign]

        original_out = activity_class.on_transition_out
        if not getattr(original_out, "_lucifer_snowfall_wrapped", False):
            def wrapped_transition_out(self: Any, *args: Any, **kwargs: Any) -> Any:
                _stop_activity(self, forget=True)
                return original_out(self, *args, **kwargs)

            wrapped_transition_out._lucifer_snowfall_wrapped = True  # type: ignore[attr-defined]
            activity_class.on_transition_out = wrapped_transition_out  # type: ignore[method-assign]

        # Keep the method available to scripts that used the old API 7 entry point.
        if not hasattr(bs.Activity, "snowfall_generator"):
            bs.Activity.snowfall_generator = snowfall_generator  # type: ignore[attr-defined]

        # The module may be installed for the first time from `/snow` during
        # an already-running match. Its transition-in hook has then already fired,
        # so register the current activity explicitly as well.
        current = _current_game_activity()
        if current is not None:
            try:
                _ACTIVE_ACTIVITIES.add(current)
                if is_enabled():
                    _start_activity(current)
            except Exception:
                _LOG.exception("Could not register snowfall for the current live game.")

        _INSTALLED = True
        _LOG.info("Installed API 9 snowfall lifecycle hooks (enabled=%s).", is_enabled())
        return True
    except Exception:
        _LOG.exception("Could not install snowfall lifecycle hooks.")
        return False
