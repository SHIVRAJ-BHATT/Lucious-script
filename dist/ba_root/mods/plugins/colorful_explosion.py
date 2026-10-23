# ba_meta require api 9
"""Holi-coloured scorch marks after BombSquad API 9 bomb explosions.

Adds a genuine BombSquad ``scorch`` node after each bomb explosion. The scorch
is tinted with a Holi colour (or blue for ice bombs) and fades over six seconds.
Normal blast damage, radius, particles, sounds, and physics are not modified.

Enable/disable via mods/setting.json::
    "colorful_explosions": {"enable": true}
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any, Sequence

import babase
import bascenev1 as bs
from bascenev1lib.actor import bomb as bomb_module

_LOG = logging.getLogger("Lucifer.ColorfulExplosion")
_INSTALLED = False
_ACTIVE_SCORCHES: list[Any] = []

# Colours used for ordinary bombs.
_HOLI_COLORS: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.6),  # Pink
    (0.0, 1.0, 1.0),  # Cyan
    (0.5, 1.0, 0.0),  # Lime
    (1.0, 0.8, 0.0),  # Yellow
    (0.6, 0.0, 1.0),  # Purple
    (1.0, 0.4, 0.0),  # Orange
)
_ICE_COLOR = (0.2, 0.2, 1.0)
_SCORCH_SIZE_FACTOR = 0.85
_SCORCH_LIFETIME = 6.0
# A lower cap limits visual clutter and bounds tracked scene-node references.
_MAX_ACTIVE_SCORCHES = 60


def _node_exists(node: Any) -> bool:
    """Return True if a node wrapper still refers to a live scene node."""
    try:
        return node is not None and bool(node.exists())
    except Exception:
        return False


def _setting_enabled() -> bool:
    """Read the central toggle; fail closed if it is absent or invalid."""
    try:
        from setting import get_section

        section = get_section("colorful_explosions")
        if not isinstance(section, dict):
            return False
        value = section.get("enable")
        return value if isinstance(value, bool) else False
    except Exception:
        _LOG.debug("Could not read colorful_explosions setting.", exc_info=True)
        return False


def _get_activity(*actors: Any) -> Any | None:
    """Resolve an owning activity, preferring the bomb/blast actors."""
    for actor in actors:
        getter = getattr(actor, "_activity", None)
        if callable(getter):
            try:
                activity = getter()
                if activity is not None:
                    return activity
            except Exception:
                # Try the next actor, then the foreground activity.
                pass
    try:
        return bs.getactivity()
    except Exception:
        return None


def _forget_scorch(scorch: Any) -> None:
    """Remove a scorch reference by identity, not wrapper equality."""
    _ACTIVE_SCORCHES[:] = [node for node in _ACTIVE_SCORCHES if node is not scorch]


def _delete_scorch(scorch: Any) -> bool:
    """Delete a scorch and forget it only after it is no longer live.

    Returns True when the node is gone. A failed deletion remains tracked so a
    later cap-enforcement attempt can retry instead of silently losing it.
    """
    if not _node_exists(scorch):
        _forget_scorch(scorch)
        return True

    try:
        scorch.delete()
    except Exception:
        _LOG.warning("Could not delete a Holi scorch node; keeping it tracked.", exc_info=True)

    if not _node_exists(scorch):
        _forget_scorch(scorch)
        return True
    return False


def _prune_scorches() -> None:
    """Drop references to scorch nodes which the engine has already removed."""
    _ACTIVE_SCORCHES[:] = [node for node in _ACTIVE_SCORCHES if _node_exists(node)]


def _make_room_for_scorch() -> bool:
    """Enforce the active-node cap without looping forever on deletion errors."""
    _prune_scorches()
    attempts_remaining = len(_ACTIVE_SCORCHES)

    while len(_ACTIVE_SCORCHES) >= _MAX_ACTIVE_SCORCHES and attempts_remaining > 0:
        oldest = _ACTIVE_SCORCHES[0]
        if _delete_scorch(oldest):
            continue

        # A deletion failure should not trap us in an infinite loop. Try each
        # existing node at most once; if none can be removed, skip this scorch.
        if _ACTIVE_SCORCHES and _ACTIVE_SCORCHES[0] is oldest:
            _ACTIVE_SCORCHES.append(_ACTIVE_SCORCHES.pop(0))
        attempts_remaining -= 1

    return len(_ACTIVE_SCORCHES) < _MAX_ACTIVE_SCORCHES


def _create_holi_scorch(
    position: Sequence[float], bomb_type: str, blast_radius: float
) -> None:
    """Create a coloured, fading scorch mark in the current activity context."""
    try:
        if len(position) < 3:
            return
        pos = (float(position[0]), float(position[1]), float(position[2]))
        radius = float(blast_radius)
    except (TypeError, ValueError, OverflowError, IndexError):
        return

    if not all(math.isfinite(value) for value in (*pos, radius)):
        return

    # Keep malformed/extreme input from producing an enormous scorch patch.
    radius = min(10.0, max(0.25, radius))
    kind = str(bomb_type).strip().lower()
    scorch_size = radius * _SCORCH_SIZE_FACTOR
    is_tnt = kind == "tnt"

    if not _make_room_for_scorch():
        _LOG.debug(
            "Skipping Holi scorch: %d existing scorch nodes could not be evicted.",
            len(_ACTIVE_SCORCHES),
        )
        return

    scorch = bs.newnode(
        "scorch",
        attrs={
            "position": pos,
            "size": scorch_size,
            "big": is_tnt,
        },
    )
    _ACTIVE_SCORCHES.append(scorch)

    try:
        scorch.color = _ICE_COLOR if kind == "ice" else random.choice(_HOLI_COLORS)

        # Remain visible for four seconds, then fade over the following two.
        bs.animate(scorch, "presence", {0.0: 1.0, 4.0: 1.0, 6.0: 0.0})
        # This one-shot timer intentionally uses bs.timer(): it does not need
        # cancellation; _delete_scorch() handles tracking and node cleanup.
        bs.timer(
            _SCORCH_LIFETIME + 0.1,
            babase.CallStrict(_delete_scorch, scorch),
        )
    except Exception:
        # Do not leave a partly initialized node untracked if setup fails.
        _delete_scorch(scorch)
        raise


def _on_bomb_exploded(bomb: Any, blast: Any) -> None:
    """Add a coloured scorch mark after the stock bomb explosion occurs."""
    if not _setting_enabled():
        return

    # Prefer the blast's position (the actual explosion point); fall back to the
    # bomb node for builds where the blast node is unavailable.
    position = None
    blast_node = getattr(blast, "node", None)
    if _node_exists(blast_node):
        try:
            position = tuple(blast_node.position)
        except Exception:
            position = None

    if position is None:
        bomb_node = getattr(bomb, "node", None)
        if not _node_exists(bomb_node):
            return
        try:
            position = tuple(bomb_node.position)
        except Exception:
            return

    activity = _get_activity(bomb, blast)
    if activity is None:
        return

    bomb_type = getattr(
        blast,
        "blast_type",
        getattr(bomb, "bomb_type", "normal"),
    )
    blast_radius = getattr(
        blast,
        "blast_radius",
        getattr(blast, "radius", getattr(bomb, "blast_radius", 2.0)),
    )

    try:
        with activity.context:
            _create_holi_scorch(position, bomb_type, blast_radius)
    except Exception:
        _LOG.exception("Could not create a Holi scorch mark after a bomb explosion.")


def install() -> bool:
    """Attach the scorch effect to Bomb explosion callbacks exactly once."""
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        original_init = bomb_module.Bomb.__init__
        if getattr(original_init, "_lucifer_holi_wrapped", False):
            _INSTALLED = True
            return True

        def wrapped_bomb_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            try:
                self.add_explode_callback(_on_bomb_exploded)
            except Exception:
                _LOG.debug(
                    "Could not attach Holi scorch callback to this bomb.",
                    exc_info=True,
                )

        wrapped_bomb_init._lucifer_holi_wrapped = True  # type: ignore[attr-defined]
        bomb_module.Bomb.__init__ = wrapped_bomb_init  # type: ignore[method-assign]
        _INSTALLED = True
        _LOG.info("Installed Holi-coloured scorch marks for bomb explosions.")
        return True
    except Exception:
        _LOG.exception("Could not install the colorful explosion hook.")
        return False
