# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Strong-punch metallic hit particles for the Lucifer API 9 mods.

The effect is cosmetic only: the original hit message is always processed
first, exactly once. Particle generation is throttled and uses conservative
counts to reduce avoidable visual workload during busy fights.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import bascenev1 as bs

_LOG = logging.getLogger("Lucifer.StrongPunchHitEffect")

# A punch-hit message with magnitude at or above this value produces sparks.
MIN_PUNCH_MAGNITUDE = 95.0

# Limit repeated visual bursts for the same victim. This does not affect damage,
# knockback, or the original HitMessage processing.
PER_VICTIM_COOLDOWN = 0.10

# Conservative particle counts reduce scene/render workload during busy fights.
# Strength still scales the effect while preserving its metallic-spark style.
_BASE_SPARK_COUNT = 5
_MAX_SPARK_COUNT = 10

_SETTING_READ_FAILURE_REPORTED = False


def _hit_effects_enabled() -> bool:
    """Read the configured toggle; default to enabled if unavailable."""
    global _SETTING_READ_FAILURE_REPORTED
    try:
        import setting

        getter = getattr(setting, "get_bool", None)
        if callable(getter):
            value = getter("hiteffects", True)
            return value if isinstance(value, bool) else True
    except Exception:
        # Avoid writing the same traceback on every qualifying impact if the
        # optional settings module is broken or unavailable.
        if not _SETTING_READ_FAILURE_REPORTED:
            _SETTING_READ_FAILURE_REPORTED = True
            _LOG.warning(
                "Could not read hiteffects setting; defaulting to enabled.",
                exc_info=True,
            )
    return True


def _finite_number(value: Any, default: float = 0.0) -> float:
    """Return a finite float, or a safe default for unexpected values."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _vector3(value: Any) -> tuple[float, float, float] | None:
    """Convert a three-component vector to finite floats or reject it."""
    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        components = tuple(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(components) != 3:
        return None
    return tuple(_finite_number(component) for component in components)  # type: ignore[return-value]


def _emit_for_hit(spaz: Any, message: Any) -> None:
    """Emit metal particles only for qualifying punch hits."""
    # Filter inexpensive conditions before reading settings or touching time.
    if not isinstance(message, bs.HitMessage):
        return
    if getattr(message, "hit_type", None) != "punch":
        return

    magnitude = _finite_number(getattr(message, "magnitude", 0.0))
    if magnitude < MIN_PUNCH_MAGNITUDE:
        return

    position = _vector3(getattr(message, "pos", None))
    if position is None:
        return

    direction = _vector3(getattr(message, "force_direction", None))
    if direction is None:
        direction = _vector3(getattr(message, "velocity", (0.0, 0.0, 0.0)))
    if direction is None:
        direction = (0.0, 0.0, 0.0)

    # Only qualifying visual events need a setting lookup. This keeps ordinary
    # hits and low-magnitude punches out of the settings path.
    if not _hit_effects_enabled():
        return

    # A per-victim cooldown caps effect rate without altering gameplay physics.
    try:
        now = float(bs.time())
        if not math.isfinite(now):
            raise ValueError("BombSquad returned a non-finite game time")
        last = _finite_number(
            getattr(spaz, "_lucifer_last_metal_hit_time", -1.0e9), -1.0e9
        )
        if now - last < PER_VICTIM_COOLDOWN:
            return
        spaz._lucifer_last_metal_hit_time = now
    except Exception:
        # Visuals remain best-effort if the optional cooldown bookkeeping fails.
        _LOG.debug("Could not apply hit-effect cooldown.", exc_info=True)

    # Direct sparks slightly away from the collision; no continuous particle
    # loop or timer is created for each hit.
    velocity = tuple(component * 0.65 for component in direction)
    excess = max(0.0, magnitude - MIN_PUNCH_MAGNITUDE)
    strength = min(1.0, excess / 180.0)
    count = min(
        _MAX_SPARK_COUNT,
        _BASE_SPARK_COUNT + int(strength * (_MAX_SPARK_COUNT - _BASE_SPARK_COUNT)),
    )
    scale = 0.38 + 0.18 * strength
    spread = 0.08 + 0.10 * strength

    bs.emitfx(
        position=position,
        velocity=velocity,
        count=count,
        scale=scale,
        spread=spread,
        chunk_type="metal",
    )


def install() -> bool:
    """Install an idempotent PlayerSpaz hit-message wrapper."""
    try:
        from bascenev1lib.actor.playerspaz import PlayerSpaz

        original = PlayerSpaz.handlemessage
        if getattr(original, "_lucifer_strong_punch_metal_wrapped", False):
            _LOG.info("Strong-punch metal hit effect is already installed.")
            return True

        def wrapped_handlemessage(
            self: Any, message: Any, *args: Any, **kwargs: Any
        ) -> Any:
            # Preserve the game's original hit handling first and exactly once.
            # A visual-effect failure must not replace damage/knockback logic.
            result = original(self, message, *args, **kwargs)
            if isinstance(message, bs.HitMessage):
                try:
                    _emit_for_hit(self, message)
                except Exception:
                    _LOG.exception("Could not emit strong-punch metal hit particles.")
            return result

        wrapped_handlemessage._lucifer_strong_punch_metal_wrapped = True  # type: ignore[attr-defined]
        PlayerSpaz.handlemessage = wrapped_handlemessage  # type: ignore[method-assign]
        _LOG.info(
            "Installed strong-punch metal hit effect "
            "(enabled=%s, minimum magnitude %.1f, cooldown %.2fs, particles %s-%s).",
            _hit_effects_enabled(),
            MIN_PUNCH_MAGNITUDE,
            PER_VICTIM_COOLDOWN,
            _BASE_SPARK_COUNT,
            _MAX_SPARK_COUNT,
        )
        return True
    except Exception:
        _LOG.exception("Could not install the strong-punch metal hit effect.")
        return False
