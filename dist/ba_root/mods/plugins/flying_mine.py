# ba_meta require api 9
# Owner/Editor: Lucifer
"""Performance-focused Flying Mine plugin for BombSquad / Ballistica API 9.

Spawns one floating mine per active GameActivity. Movement is animation-driven;
there is no custom collision-material setup or per-frame update loop.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any

import bascenev1 as bs
from bascenev1._map import Map
from bascenev1lib.gameutils import SharedObjects

_LOG = logging.getLogger("Lucifer.FlyingMine")
_ENABLED = False

# Lightweight fixed patrol region; avoids expensive/repeated map-bound probing.
_X_RANGE = (-8.0, 8.0)
_Y_RANGE = (3.0, 8.0)
_Z_RANGE = (-6.0, 6.0)


class Mine(bs.Actor):
    """A large floating prop that moves between randomized destinations."""

    _NORMAL_SPEED = 3.0
    _FAST_SPEED = 5.0
    _FAST_CHANCE = 0.20
    _MIN_TARGET_DISTANCE = 10.0
    _MIN_MOVE_DURATION = 2.5
    _MAX_MOVE_DURATION = 8.0
    _TARGET_PAUSE_DURATION = 0.65
    _RESTART_DELAY = 0.10

    def __init__(self) -> None:
        super().__init__()
        shared = SharedObjects.get()

        self._held = False
        self._movement_generation = 0
        self._motion: bs.Node | None = None

        self.node = bs.newnode(
            "prop",
            delegate=self,
            attrs={
                "position": self._random_position(),
                "velocity": (0.0, 0.0, 0.0),
                "sticky": False,
                "body": "sphere",
                "mesh": bs.getmesh("landMine"),
                "mesh_scale": 3.3,
                # Use only the built-in Powerup Curse texture; no random selection.
                "color_texture": bs.gettexture("powerupShield"),
                "body_scale": 3.3,
                "reflection": "powerup",
                "reflection_scale": [0.6],
                "density": 9999999999,
                "damping": 9999999999,
                "gravity_scale": 0.0,
                "shadow_size": 0.0,
                # Retain pickup interaction only; no custom collision materials.
                "materials": (shared.pickup_material,),
            },
        )
        self._set_random_target()

    @staticmethod
    def _random_position() -> tuple[float, float, float]:
        return (
            random.uniform(*_X_RANGE),
            random.uniform(*_Y_RANGE),
            random.uniform(*_Z_RANGE),
        )

    @staticmethod
    def _distance_squared(
        first: tuple[float, float, float],
        second: tuple[float, float, float],
    ) -> float:
        dx = first[0] - second[0]
        dy = first[1] - second[1]
        dz = first[2] - second[2]
        return dx * dx + dy * dy + dz * dz

    def _node_is_valid(self) -> bool:
        node = self.node
        return node is not None and node.exists()

    def _random_target(
        self, origin: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        """Pick a reasonably distant target with bounded random work."""
        min_distance_sq = self._MIN_TARGET_DISTANCE * self._MIN_TARGET_DISTANCE
        best = self._random_position()
        best_distance_sq = self._distance_squared(best, origin)

        # Bounded attempts prevent wasted work on small/odd patrol regions.
        for _ in range(8):
            candidate = self._random_position()
            distance_sq = self._distance_squared(candidate, origin)
            if distance_sq > best_distance_sq:
                best = candidate
                best_distance_sq = distance_sq
            if distance_sq >= min_distance_sq:
                return candidate
        return best

    def _set_random_target(self) -> None:
        if self._held or not self._node_is_valid():
            return

        current = tuple(float(value) for value in self.node.position)
        target = self._random_target(current)
        speed = (
            self._FAST_SPEED
            if random.random() < self._FAST_CHANCE
            else self._NORMAL_SPEED
        )
        duration = min(
            self._MAX_MOVE_DURATION,
            max(
                self._MIN_MOVE_DURATION,
                math.sqrt(self._distance_squared(current, target)) / speed,
            ),
        )

        self._movement_generation += 1
        generation = self._movement_generation
        self._animate_to(current, target, duration)
        bs.timer(
            duration + self._TARGET_PAUSE_DURATION,
            bs.WeakCallPartial(self._advance_to_next_target, generation),
        )

    def _animate_to(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        duration: float,
    ) -> None:
        """Animate position while reusing the combine node between moves."""
        if not self._node_is_valid():
            return

        motion = self._motion
        if motion is None or not motion.exists():
            motion = bs.newnode("combine", owner=self.node, attrs={"size": 3})
            motion.connectattr("output", self.node, "position")
            self._motion = motion

        # Re-animate existing inputs instead of allocating a new combine node
        # for every route segment. The three animation curves expire normally.
        for index in range(3):
            bs.animate(
                motion,
                f"input{index}",
                {0.0: start[index], duration: end[index]},
            )

    def _stop_motion(self) -> None:
        motion = self._motion
        self._motion = None
        if motion is not None and motion.exists():
            motion.delete()

    def _advance_to_next_target(self, generation: int) -> None:
        if generation != self._movement_generation or self._held:
            return
        self._set_random_target()

    def _resume_movement(self) -> None:
        if self._held or not self._node_is_valid():
            return
        self.node.velocity = (0.0, 0.0, 0.0)
        self._set_random_target()

    def handlemessage(self, msg: Any) -> Any:
        if isinstance(msg, bs.PickedUpMessage):
            self._held = True
            self._movement_generation += 1
            self._stop_motion()
            return None

        if isinstance(msg, bs.DroppedMessage):
            self._held = False
            if self._node_is_valid():
                self.node.velocity = (0.0, 0.0, 0.0)
                bs.timer(
                    self._RESTART_DELAY,
                    bs.WeakCallStrict(self._resume_movement),
                )
            return None

        if isinstance(msg, bs.OutOfBoundsMessage):
            if self._node_is_valid():
                self._held = False
                self._movement_generation += 1
                self._stop_motion()
                self.node.velocity = (0.0, 0.0, 0.0)
                self.node.position = self._random_position()
                bs.timer(
                    self._RESTART_DELAY,
                    bs.WeakCallStrict(self._resume_movement),
                )
            return True

        if isinstance(msg, bs.DieMessage):
            self._movement_generation += 1
            self._stop_motion()
            if self.node is not None and self.node.exists():
                self.node.delete()
            return None

        return super().handlemessage(msg)


def _schedule_mine_for_map(_game_map: Map) -> None:
    """Schedule a single mine for the currently active gameplay activity."""
    try:
        activity = bs.getactivity(doraise=False)
        game_activity_class = getattr(bs, "GameActivity", None)
        if (
            activity is None
            or game_activity_class is None
            or not isinstance(activity, game_activity_class)
        ):
            return
        if getattr(activity, "_lucifer_flying_mine_scheduled", False):
            return

        activity._lucifer_flying_mine_scheduled = True

        def spawn_mine() -> None:
            try:
                current_activity = bs.getactivity(doraise=False)
                if current_activity is not activity:
                    return
                if not isinstance(current_activity, game_activity_class):
                    return
                if current_activity.has_ended():
                    return
                if getattr(current_activity, "_lucifer_flying_mine_spawned", False):
                    return

                with current_activity.context:
                    Mine().autoretain()
                    current_activity._lucifer_flying_mine_spawned = True
                _LOG.info("Spawned the Flying Mine for the active game.")
            except Exception:
                _LOG.exception("Could not spawn the Flying Mine.")

        # One delayed one-shot timer; no polling or per-frame Python callbacks.
        bs.timer(0.15, spawn_mine)
    except Exception:
        _LOG.exception("Could not schedule the Flying Mine.")


def enable() -> bool:
    """Install the map-init hook once; safe to call from custom_hooks.py."""
    global _ENABLED
    if _ENABLED:
        return True

    original_init = Map.__init__
    if getattr(original_init, "_lucifer_flying_mine_wrapped", False):
        _ENABLED = True
        return True

    def wrapped_map_init(self: Map, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        _schedule_mine_for_map(self)

    wrapped_map_init._lucifer_flying_mine_wrapped = True  # type: ignore[attr-defined]
    Map.__init__ = wrapped_map_init  # type: ignore[method-assign]
    _ENABLED = True
    _LOG.info("Installed performance-focused API 9 Flying Mine hook.")
    return True
