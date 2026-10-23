# ba_meta require api 9
"""Custom visual effects for BombSquad API 9.

Manually assigned effects are persisted by playersdata.pdata in effects.json.
Optional automatic Top 3 effects are resolved from the central mods/setting.json
configuration and the existing V2-only stats leaderboard.
"""

from __future__ import annotations

import functools
import logging
import math
import random
import weakref
from typing import Any, Sequence

import babase
import bascenev1 as bs
from bascenev1lib.actor.spazfactory import SpazFactory
from bascenev1lib.gameutils import SharedObjects

from playersdata import pdata
from tools.account_ids import get_v2_account_id, is_v2_account_id

_LOG = logging.getLogger("Lucifer.SpazEffects")

try:
    import setting as _settings
except Exception:
    _settings = None
    _LOG.warning(
        "Settings module could not be imported; settings-dependent effects "
        "will use safe fallbacks.",
        exc_info=True,
    )

try:
    from stats import mystats as _mystats
except Exception:
    _mystats = None
    _LOG.warning(
        "Stats module could not be imported; automatic Top 3 effects may be unavailable.",
        exc_info=True,
    )

EFFECT_NAMES = frozenset({
    "spark", "sparkground", "sweat", "sweatground", "distortion", "glow",
    "shine", "highlightshine", "scorch", "ice", "iceground", "slime",
    "metal", "splinter", "rainbow", "fairydust", "surrounder", "fire",
    "stars", "new_rainbow", "footprint", "chispitas", "darkmagic",
    "colorfullspark", "ring", "brust", "ringstars", "noeffect",
})

# Reused by the high-frequency particle effects. Caching these values avoids
# repeatedly constructing equivalent materials and resolving the same assets.
_STAR_TEXTURE_NAMES = (
    "bombStickyColor", "aliColor", "aliColorMask", "eggTex3",
)
_PARTICLE_OFFSETS = (
    (0.3, 0.0, 0.0),
    (0.0, 0.0, 0.3),
    (-0.3, 0.0, 0.0),
    (0.0, 0.0, -0.3),
)
_NEW_RAINBOW_ANIMATION = {
    0.0: (2.0, 0.0, 0.0),
    0.2: (2.0, 1.5, 0.5),
    0.4: (2.0, 2.0, 0.0),
    0.6: (0.0, 2.0, 0.0),
    0.8: (0.0, 2.0, 2.0),
    1.0: (0.0, 0.0, 2.0),
}


def normalize_effect_name(value: Any) -> str | None:
    """Return a canonical supported effect name, or None for unknown input."""
    if not isinstance(value, str):
        return None
    name = value.strip().casefold()
    return name if name in EFFECT_NAMES else None


def _configured_top3_effect(account_id: str | None) -> str | None:
    """Return the configured automatic effect for this account's current Top 3 rank.

    Ranking uses the same get_sorted_stats() source as TextOnMap's leaderboard.
    Only validated V2 account IDs are considered. Automatic effects are not saved
    to effects.json, so manually assigned effects remain untouched.
    """
    if not is_v2_account_id(account_id):
        return None

    settings_module = _settings
    if settings_module is None:
        try:
            import setting as settings_module  # type: ignore[no-redef]
        except Exception:
            _LOG.warning(
                "Top 3 effects unavailable: settings module could not load.",
                exc_info=True,
            )
            return None

    try:
        settings_data = settings_module.get_settings_data()
        if not isinstance(settings_data, dict) or settings_data.get("enableTop3effects") is not True:
            return None
        rank_effects = settings_data.get("top3Effects", {})
        if not isinstance(rank_effects, dict):
            return None

        stats_module = _mystats
        if stats_module is None:
            try:
                from stats import mystats as stats_module  # type: ignore[no-redef]
            except Exception:
                _LOG.warning(
                    "Top 3 effects unavailable: stats module could not load.",
                    exc_info=True,
                )
                return None

        stats_data = stats_module.get_cached_stats()
        entries = stats_module.get_sorted_stats(stats_data)
        for rank, entry in enumerate(entries[:3], start=1):
            if not isinstance(entry, (tuple, list)) or len(entry) < 6:
                continue
            ranked_account_id = entry[5]
            if not is_v2_account_id(ranked_account_id) or ranked_account_id != account_id:
                continue

            setting_key = f"rank{rank}"
            effect_name = normalize_effect_name(rank_effects.get(setting_key))
            if effect_name is None:
                _LOG.warning(
                    "Unsupported Top 3 effect configured for %s: %r.",
                    setting_key,
                    rank_effects.get(setting_key),
                )
                return None
            # "noeffect" means no additional automatic rank effect for this slot.
            return None if effect_name == "noeffect" else effect_name
    except Exception:
        _LOG.warning(
            "Could not resolve automatic Top 3 effect for account %s.",
            account_id,
            exc_info=True,
        )
    return None


def _player_v2_id(player_or_session_player: Any) -> str | None:
    """Read only a validated V2 ID; never fall back to a V1 identifier."""
    if player_or_session_player is None:
        return None
    # A SessionPlayer exposes get_account_id() directly.
    if callable(getattr(player_or_session_player, "get_account_id", None)):
        return get_v2_account_id(player_or_session_player)
    # API 9 Player objects expose their SessionPlayer through sessionplayer.
    # Do not import a convenience helper that may not exist in older installs,
    # and never fall back to V1 account identifiers.
    session_player = getattr(player_or_session_player, "sessionplayer", None)
    if session_player is None:
        session_player = getattr(player_or_session_player, "_sessionplayer", None)
    if session_player is not None:
        return get_v2_account_id(session_player)
    return None


def _timer(seconds: float, callback: Any) -> Any:
    """Create a reference-managed, cancellable-by-release scene timer.

    ``bs.timer`` returns ``None`` and cannot be cancelled. This controller stores
    timer objects so clearing ``_timers`` releases pending timers during cleanup.
    The scheduled callbacks also check controller state before doing any work.
    Call this while the intended activity context is current.
    """
    return bs.Timer(max(0.0, float(seconds)), callback)


def _get_activity_or_none() -> Any | None:
    """Return the current activity, or None during lobby/transition contexts."""
    try:
        return bs.getactivity()
    except babase._error.ActivityNotFoundError:
        # This is a normal condition when no gameplay activity is foregrounded.
        return None


class SurroundBallFactory:
    """Per-activity resource cache used by the legacy surrounder effect."""
    def __init__(self) -> None:
        self.bonesTex = bs.gettexture("powerupCurse")
        self.bearTex = bs.gettexture("bearColor")
        self.aliTex = bs.gettexture("aliColor")
        self.b9000Tex = bs.gettexture("cyborgColor")
        self.frostyTex = bs.gettexture("frostyColor")
        self.cubeTex = bs.gettexture("crossOutMask")
        self.bonesModel = bs.getmesh("bonesHead")
        self.bearModel = bs.getmesh("bearHead")
        self.aliModel = bs.getmesh("aliHead")
        self.b9000Model = bs.getmesh("cyborgHead")
        self.frostyModel = bs.getmesh("frostyHead")
        self.cubeModel = bs.getmesh("powerup")
        try:
            self.mikuModel = bs.getmesh("operaSingerHead")
            self.mikuTex = bs.gettexture("operaSingerColor")
        except Exception:
            _LOG.exception("Optional opera-singer mesh is unavailable; using fallback.")
            self.mikuModel = self.bonesModel
            self.mikuTex = self.bonesTex
        self.ballMaterial = bs.Material()
        self.impactSound = bs.getsound("impactMedium")
        self.ballMaterial.add_actions(
            actions=("modify_node_collision", "collide", False)
        )


class SurroundBall(bs.Actor):
    """Orbiting decorative prop used by the ``surrounder`` effect.

    The Activity retains Actors until ``exists()`` returns False.  This actor
    therefore marks itself dead and drops its Python references from both normal
    death and Activity expiry paths.  Its timer uses a weak callback so the timer
    cannot keep this Actor alive.
    """

    def __init__(self, spaz: Any, shape: str = "bones") -> None:
        super().__init__()
        self._dead = False
        self.spazRef: Any | None = weakref.ref(spaz)
        self.surroundTimer: Any | None = None
        self.surroundRadius = 1.0
        self.angleDelta = math.pi / 12.0
        self.curAngle = random.random() * math.pi * 2.0
        self.curHeight = 0.0
        self.curHeightDir = 1
        self.heightDelta = 0.2
        self.heightMax = 1.0
        self.heightMin = 0.1
        self.node: Any | None = None

        factory = self.getFactory()
        s_model, s_texture = {
            "bones": (factory.bonesModel, factory.bonesTex),
            "bear": (factory.bearModel, factory.bearTex),
            "ali": (factory.aliModel, factory.aliTex),
            "b9000": (factory.b9000Model, factory.b9000Tex),
            "miku": (factory.mikuModel, factory.mikuTex),
            "frosty": (factory.frostyModel, factory.frostyTex),
            "RedCube": (factory.cubeModel, factory.cubeTex),
        }.get(shape, (factory.bonesModel, factory.bonesTex))
        node_obj = bs.newnode(
            "prop",
            attrs={
                "mesh": s_model,
                "body": "sphere",
                "color_texture": s_texture,
                "reflection": "soft",
                "mesh_scale": 0.5,
                "body_scale": 0.1,
                "density": 0.1,
                "reflection_scale": [0.15],
                "shadow_size": 0.6,
                "position": spaz.node.position,
                "velocity": (0, 0, 0),
                "materials": [SharedObjects.get().object_material, factory.ballMaterial],
            },
            delegate=self,
        )
        self.node = node_obj
        self.initTimer(spaz.node.position)

    @classmethod
    def getFactory(cls) -> SurroundBallFactory:
        activity = _get_activity_or_none()
        if activity is None:
            raise RuntimeError("No current activity for surrounder effect")
        factory = getattr(activity, "_sharedSurroundBallFactory", None)
        if factory is None:
            factory = SurroundBallFactory()
            activity._sharedSurroundBallFactory = factory
        return factory

    def getTargetPosition(self, spazPos: Sequence[float]) -> tuple[float, float, float]:
        p = spazPos
        pt = (
            p[0] + self.surroundRadius * math.cos(self.curAngle),
            p[1] + self.curHeight,
            p[2] + self.surroundRadius * math.sin(self.curAngle),
        )
        self.curAngle += self.angleDelta
        self.curHeight += self.heightDelta * self.curHeightDir
        if self.curHeight > self.heightMax or self.curHeight < self.heightMin:
            self.curHeightDir = -self.curHeightDir
        return pt

    def initTimer(self, position: Sequence[float]) -> None:
        node_obj = self.node
        if not self._dead and node_obj is not None and node_obj.exists():
            node_obj.position = self.getTargetPosition(position)
        self._schedule_move()

    def _schedule_move(self) -> None:
        if self._dead or self.expired or not self.exists():
            self.surroundTimer = None
            return
        # WeakCallStrict is important: a pending timer must not be a strong
        # reference path back to this Actor.
        self.surroundTimer = _timer(
            1.0 / 30.0, babase.WeakCallStrict(self.circleMove)
        )

    def circleMove(self) -> None:
        self.surroundTimer = None
        if self._dead or self.expired or not self.exists():
            return
        parent_ref = self.spazRef
        parent = parent_ref() if callable(parent_ref) else None
        try:
            parent_node = getattr(parent, "node", None) if parent is not None else None
            parent_alive = (
                parent is not None
                and parent.is_alive()
                and parent_node is not None
                and parent_node.exists()
            )
        except Exception:
            _LOG.debug("Could not validate the surrounder parent; removing prop.", exc_info=True)
            parent_alive = False
        if not parent_alive:
            self.handlemessage(bs.DieMessage(immediate=True))
            return
        node_obj = self.node
        if node_obj is None or not node_obj.exists():
            self.handlemessage(bs.DieMessage(immediate=True))
            return
        target = self.getTargetPosition(parent_node.position)
        current = node_obj.position
        direction = (
            target[0] - current[0],
            target[1] - current[1],
            target[2] - current[2],
        )
        node_obj.velocity = self.getMaxSpeedByDir(direction)
        self._schedule_move()

    @staticmethod
    def getMaxSpeedByDir(direction: Sequence[float]) -> tuple[float, float, float]:
        divisor = max(max(abs(x) for x in direction), 0.0001)
        factor = 7.0 / divisor
        return tuple(x * factor for x in direction)  # type: ignore[return-value]

    def _release_parent_reference(self) -> None:
        """Unlink this dead actor from its owning EffectsController."""
        parent_ref = self.spazRef
        parent = parent_ref() if callable(parent_ref) else None
        if parent is not None:
            try:
                if getattr(parent, "surround", None) is self:
                    parent.surround = None
            except Exception:
                _LOG.debug("Could not clear controller's SurroundBall reference.", exc_info=True)
        self.spazRef = None

    def handlemessage(self, message: Any) -> Any:
        if isinstance(message, bs.OutOfBoundsMessage):
            return self.handlemessage(bs.DieMessage(immediate=True))

        if isinstance(message, bs.DieMessage):
            if self._dead:
                return None
            self._dead = True
            self.surroundTimer = None  # Releasing a bs.Timer cancels its pending callback.
            self._release_parent_reference()
            node_obj = self.node
            # Do not modify scene nodes after Activity expiry; the Activity owns teardown then.
            try:
                if not self.expired and node_obj is not None and node_obj.exists():
                    node_obj.delete()
            except Exception:
                _LOG.debug("Could not delete the surrounder prop during death.", exc_info=True)
            self.node = None
            return None

        return super().handlemessage(message)

    def exists(self) -> bool:
        """Return False once dead, expired, or the backing node is gone."""
        if self._dead or self.expired:
            return False
        node_obj = self.node
        try:
            return node_obj is not None and bool(node_obj.exists())
        except Exception:
            return False

    def is_alive(self) -> bool:
        if not self.exists():
            return False
        parent_ref = self.spazRef
        parent = parent_ref() if callable(parent_ref) else None
        try:
            parent_node = getattr(parent, "node", None) if parent is not None else None
            return bool(
                parent is not None
                and parent.is_alive()
                and parent_node is not None
                and parent_node.exists()
            )
        except Exception:
            return False

    def on_expire(self) -> None:
        """Release all references when the owning Activity expires."""
        parent_ref = self.spazRef
        parent = parent_ref() if callable(parent_ref) else None
        self._dead = True
        self.surroundTimer = None
        self._release_parent_reference()
        # Scene operations are forbidden once expired; the Activity deletes nodes.
        self.node = None
        super().on_expire()
        # The SurroundBall is the Activity-owned Actor that receives on_expire.
        # Use that lifecycle callback to explicitly stop its non-Actor controller
        # too; that drops controller-owned timers/node wrappers before they can
        # retain stale scene state across activity transitions.
        if parent is not None:
            stop = getattr(parent, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    _LOG.debug(
                        "Could not stop the effects controller on Activity expiry.",
                        exc_info=True,
                    )


def effect(repeat_interval: float = 0.0):
    """Decorate a method as an effect loop owned by one EffectsController."""
    def _activator(method: Any):
        @functools.wraps(method)
        def _inner_activator(self: "EffectsController") -> None:
            # Pass the unbound function. A lambda capturing self would create a
            # strong-reference path through the timer callback during teardown.
            self._schedule_repeating_effect(method.__name__, repeat_interval, method)
        return _inner_activator
    return _activator


def node(check_interval: float = 0.0):
    """Decorate a method that creates one node attached to the character."""
    def _activator(method: Any):
        @functools.wraps(method)
        def _inner_activator(self: "EffectsController") -> None:
            created_node = method(self)
            if created_node is not None:
                self._track_node(created_node)
                self._schedule_node_watch(method.__name__, created_node, check_interval)
        return _inner_activator
    return _activator


class _EffectVisualsMixin:
    @effect(repeat_interval=0.1)
    def _add_spark(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 10),
            scale=0.5,
            spread=0.2,
            chunk_type="spark",
        )

    def _add_surround(self):
         self.surround = SurroundBall(self, shape="bones")
        
    @effect(repeat_interval=0.1)
    def _add_sparkground(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 5),
            scale=0.2,
            spread=0.1,
            chunk_type="spark",
            emit_type="stickers",
        )

    @effect(repeat_interval=0.08)
    def _add_sweat(self):
        random_speed = 4.0
        position = tuple(
            coordinate - 0.25 + random.uniform(0, 0.5)
            for coordinate in self.node.torso_position
        )
        node_velocity = self.node.velocity
        velocity = (
            random.uniform(-random_speed, random_speed) + node_velocity[0] * 2,
            random.uniform(-random_speed, random_speed) + node_velocity[1] * 4,
            random.uniform(-random_speed, random_speed) + node_velocity[2] * 2,
        )
        bs.emitfx(
            position=position,
            velocity=velocity,
            count=5,
            scale=random.uniform(0.3, 1.4),
            spread=0.1,
            chunk_type="sweat",
        )

    @effect(repeat_interval=0.08)
    def _add_sweatground(self):
        random_speed = 1.2
        position = tuple(
            coordinate - 0.25 + random.uniform(0, 0.5)
            for coordinate in self.node.torso_position
        )
        node_velocity = self.node.velocity
        velocity = (
            random.uniform(-random_speed, random_speed) + node_velocity[0] * 2,
            random.uniform(-random_speed, random_speed) + node_velocity[1] * 4,
            random.uniform(-random_speed, random_speed) + node_velocity[2] * 2,
        )
        bs.emitfx(
            position=position,
            velocity=velocity,
            count=5,
            scale=random.uniform(0.1, 1.2),
            spread=0.1,
            chunk_type="sweat",
            emit_type="stickers",
        )

    @effect(repeat_interval=1.0)
    def _add_distortion(self):
        bs.emitfx(
            position=self.node.position,
            spread=1.0,
            emit_type="distortion"
        )
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 5),
            emit_type="tendrils",
            tendril_type="smoke",
        )

    @effect(repeat_interval=3.0)
    def _add_shine(self):
        shine_factor = 1.2
        dim_factor = 0.90

        default_color = self.node.color
        shiny_color = tuple(channel * shine_factor for channel in default_color)
        dimmy_color = tuple(channel * dim_factor for channel in default_color)
        animation = {
            0: default_color,
            1: dimmy_color,
            2: shiny_color,
            3: default_color,
        }
        bs.animate_array(self.node, "color", 3, animation)

    @effect(repeat_interval=9.0)
    def _add_highlightshine(self):
        shine_factor = 1.2
        dim_factor = 0.90

        default_highlight = self.node.highlight
        shiny_highlight = tuple(
            channel * shine_factor for channel in default_highlight)
        dimmy_highlight = tuple(
            channel * dim_factor for channel in default_highlight)
        animation = {
            0: default_highlight,
            3: dimmy_highlight,
            6: shiny_highlight,
            9: default_highlight,
        }
        bs.animate_array(self.node, "highlight", 3, animation)

    @effect(repeat_interval=2.0)
    def _add_rainbow(self):
        highlight = tuple(random.random() for _ in range(3))
        highlight = babase.safecolor(highlight)
        animation = {
            0: self.node.highlight,
            2: highlight,
        }
        bs.animate_array(self.node, "highlight", 3, animation)

    @node(check_interval=0.5)
    def _add_glow(self):
        glowing_light = bs.newnode(
            "light",
            attrs={
                "color": (1.0, 0.4, 0.5),
                "height_attenuated": False,
                "radius": 0.4}
        )
        self.node.connectattr("position", glowing_light, "position")
        bs.animate(
            glowing_light,
            "intensity",
            {0: 0.0, 1: 0.2, 2: 0.0},
            loop=True)
        return glowing_light

    @node(check_interval=0.5)
    def _add_scorch(self):
        scorcher = bs.newnode(
            "scorch",
            attrs={
                "position": self.node.position,
                "size": 1.00,
                "big": True}
        )
        self.node.connectattr("position", scorcher, "position")
        animation = {
            0: (1, 0, 0),
            1: (0, 1, 0),
            2: (1, 0, 1),
            3: (0, 1, 1),
            4: (1, 0, 0),
        }
        bs.animate_array(scorcher, "color", 3, animation, loop=True)
        return scorcher

    @effect(repeat_interval=0.5)
    def _add_ice(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(2, 8),
            scale=0.4,
            spread=0.2,
            chunk_type="ice",
        )

    @effect(repeat_interval=0.1)
    def _add_iceground(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 2),
            scale=random.uniform(0, 0.5),
            spread=1.0,
            chunk_type="ice",
            emit_type="stickers",
        )

    @effect(repeat_interval=0.25)
    def _add_slime(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 10),
            scale=0.4,
            spread=0.2,
            chunk_type="slime",
        )

    @effect(repeat_interval=0.25)
    def _add_metal(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 4),
            scale=0.4,
            spread=0.1,
            chunk_type="metal",
        )

    @effect(repeat_interval=0.75)
    def _add_splinter(self):
        bs.emitfx(
            position=self.node.position,
            velocity=self.node.velocity,
            count=random.randint(1, 5),
            scale=0.5,
            spread=0.2,
            chunk_type="splinter",
        )

    @effect(repeat_interval=0.1)
    def _add_fairydust(self):
        random_speed = 2.0
        position = tuple(
            coordinate - 0.25 + random.uniform(0, 0.5)
            for coordinate in self.node.torso_position
        )
        node_velocity = self.node.velocity
        velocity = (
            random.uniform(-random_speed, random_speed) + node_velocity[0] * 6,
            random.uniform(-random_speed, random_speed) + node_velocity[1] * 8,
            random.uniform(-random_speed, random_speed) + node_velocity[2] * 8,
        )
        bs.emitfx(
                position=position,
                velocity=velocity,
                count=random.randint(8, 16),
                spread=2.5,
                emit_type="fairydust",
    )
    
    
    @effect(repeat_interval=0.2)
    def _add_fire(self) -> None:
        if not self.node.exists():
            self._cm_effect_timer = None
        else:
            bs.emitfx(
                position=self.node.position,
                scale=2.5,
                count=12,
                spread=0.3,
                chunk_type="sweat",
            )


    @effect(repeat_interval=0.2)
    def _add_stars(self) -> None:
        if not _node_is_live(self.node) or self._dead:
            return

        position = self.node.position
        mesh, textures = self._get_star_assets()
        material = self._get_particle_collision_material()

        for offset in _PARTICLE_OFFSETS:
            pos = (
                position[0] + offset[0],
                position[1] + offset[1],
                position[2] + offset[2],
            )
            velocity = (
                random.uniform(-1.5, 1.5),
                random.uniform(2.0, 7.0),
                random.uniform(-1.5, 1.5),
            )
            prop = self._new_temporary_node(
                "prop",
                attrs={
                    "body": "sphere",
                    "position": pos,
                    "velocity": velocity,
                    "mesh": mesh,
                    "mesh_scale": 0.1,
                    "body_scale": 0.0,
                    "shadow_size": 0.0,
                    "gravity_scale": 0.5,
                    "color_texture": random.choice(textures),
                    "reflection": "soft",
                    "reflection_scale": [1.5],
                    "materials": [material],
                },
            )
            if prop is None:
                return
            light = bs.newnode(
                "light",
                owner=prop,
                attrs={
                    "intensity": 0.3,
                    "volume_intensity_scale": 0.5,
                    "color": (
                        random.uniform(0.5, 1.5),
                        random.uniform(0.5, 1.5),
                        random.uniform(0.5, 1.5),
                    ),
                    "radius": 0.035,
                },
            )
            prop.connectattr("position", light, "position")
            # The light is owned by the prop; when the prop dies, Ballistica
            # deletes the light too. Tracking every short-lived child wrapper
            # separately only grows _owned_nodes unnecessarily.
            self._schedule_delayed(
                0.25,
                functools.partial(self._shrink_and_delete_node, prop),
                "temporary-prop-cleanup",
            )


    @effect(repeat_interval=0.0)
    def _add_new_rainbow(self) -> None:
        # Let the engine run both looping animations. Reapplying these every
        # 1.2 seconds creates repeated Python callbacks without improving the
        # animation; keeping them engine-driven also makes the cycling smoother.
        bs.animate_array(
            self.node, "highlight", 3, _NEW_RAINBOW_ANIMATION, loop=True
        )
        bs.animate_array(
            self.node, "color", 3, _NEW_RAINBOW_ANIMATION, loop=True
        )

    @effect(repeat_interval=0.25)
    def _add_footprint(self) -> None:
        if not self.node.exists():
            self._cm_effect_timer = None
        else:
            loc = bs.newnode('locator', owner=self.node,
              attrs={
                     'position': self.node.position,
                     'shape': 'circle',
                     'color': (random.uniform(0.5, 1.5),
                               random.uniform(0.5, 1.5),
                               random.uniform(0.5, 1.5)),
                     'size': [0.2],
                     'draw_beauty': False,
                     'additive': False})
            self._track_node(loc)
            bs.animate(loc, "opacity", {0: 1.0, 1.4: 0.0})
            self._schedule_delayed(
                1.5,
                functools.partial(self._delete_node_if_live, loc),
                "footprint-cleanup",
            )


    @effect(repeat_interval=0.2)
    def _add_chispitas(self) -> None:
        if not _node_is_live(self.node) or self._dead:
            return

        position = self.node.position
        texture = self._get_chispitas_texture()
        material = self._get_particle_collision_material()

        for offset in _PARTICLE_OFFSETS:
            pos = (
                position[0] + offset[0],
                position[1] + offset[1],
                position[2] + offset[2],
            )
            velocity = (
                random.uniform(-1.5, 1.5),
                random.uniform(2.0, 7.0),
                random.uniform(-1.5, 1.5),
            )
            bomb = self._new_temporary_node(
                "bomb",
                attrs={
                    "body": "sphere",
                    "position": pos,
                    "velocity": velocity,
                    "mesh": None,
                    "mesh_scale": 0.1,
                    "body_scale": 0.0,
                    "color_texture": texture,
                    "fuse_length": 0.1,
                    "materials": [material],
                },
            )
            if bomb is None:
                return
            light = bs.newnode(
                "light",
                owner=bomb,
                attrs={
                    "intensity": 0.2,
                    "volume_intensity_scale": 0.4,
                    "color": (
                        random.uniform(0.5, 1.5),
                        random.uniform(0.5, 1.5),
                        random.uniform(0.5, 1.5),
                    ),
                    "radius": 0.025,
                },
            )
            bomb.connectattr("position", light, "position")
            # The light is owned by the bomb node and is removed with it.
            self._schedule_delayed(
                0.25,
                functools.partial(self._shrink_and_delete_node, bomb),
                "temporary-prop-cleanup",
            )


    @effect(repeat_interval=0.25)
    def _add_darkmagic(self) -> None:
        if not _node_is_live(self.node) or self._dead:
            return

        position = self.node.position
        mesh, texture = self._get_darkmagic_assets()
        material = self._get_particle_collision_material()

        # Two opposite emit points preserve the effect's symmetry while
        # halving short-lived prop/light creation.
        for offset in _PARTICLE_OFFSETS[::2]:
            pos = (
                position[0] + offset[0],
                position[1] + offset[1],
                position[2] + offset[2],
            )
            velocity = (
                random.uniform(-1.5, 1.5),
                30.0,
                random.uniform(-1.5, 1.5),
            )
            prop = self._new_temporary_node(
                "prop",
                attrs={
                    "body": "sphere",
                    "position": pos,
                    "velocity": velocity,
                    "mesh": mesh,
                    "mesh_scale": 0.4,
                    "body_scale": 0.0,
                    "shadow_size": 0.0,
                    "gravity_scale": 0.5,
                    "color_texture": texture,
                    "reflection": "soft",
                    "reflection_scale": [0.0],
                    "materials": [material],
                },
            )
            if prop is None:
                return
            light = bs.newnode(
                "light",
                owner=prop,
                attrs={
                    "intensity": 0.8,
                    "volume_intensity_scale": 0.5,
                    "color": (0.5, 0.0, 1.0),
                    "radius": 0.035,
                },
            )
            prop.connectattr("position", light, "position")
            # The light is owned by the prop and follows its lifetime.
            self._schedule_delayed(
                0.25,
                functools.partial(self._shrink_and_delete_node, prop),
                "temporary-prop-cleanup",
            )


    def _add_aure(self) -> None:
        def anim(node: bs.Node) -> None:
            bs.animate_array(node, 'color', 3,
                {0: (1,1,0), 0.1: (0,1,0),
                 0.2: (1,0,0), 0.3: (0,0.5,1),
                 0.4: (1,0,1)}, loop=True)
            bs.animate_array(node, 'size', 1,
                {0: [1.0], 0.2: [1.5], 0.3: [1.0]}, loop=True)

        attrs = ['torso_position', 'position_center', 'position']
        for i, pos in enumerate(attrs):
            loc = bs.newnode('locator', owner=self.node,
                  attrs={'shape': 'circleOutline',
                         'color': self.node.color,
                         'opacity': 1.0,
                         'draw_beauty': True,
                         'additive': False})
            self._track_node(loc)
            self.node.connectattr(pos, loc, 'position')
            self._schedule_delayed(
                0.1 * i, lambda loc=loc: anim(loc), "ring-animation"
            )


    def _add_galactic_burst(self) -> None:
        self._add_new_rainbow()
        self._add_fairydust()

 
    def _add_colorful_spark(self) -> None:
        self._add_spark()
        self._add_sweatground()
        self._add_new_rainbow()


    def _add_star_ring(self) -> None:
        self._add_fairydust()
        self._add_aure()

def _node_is_live(node_obj: Any) -> bool:
    """Return whether a BombSquad scene node still exists."""
    try:
        return node_obj is not None and bool(node_obj.exists())
    except Exception:
        return False


def _safe_delete_node(node_obj: Any) -> None:
    """Delete a live scene node, logging failures without retaining it."""
    if not _node_is_live(node_obj):
        return
    try:
        node_obj.delete()
    except Exception:
        _LOG.debug("Could not delete a scene node during effects cleanup.", exc_info=True)


class _TemporaryEffectNodeDelegate:
    """Immediately remove short-lived visual nodes that leave the map.

    Ballistica forwards standard node messages to a node's delegate. These
    particles are not player actors, so their out-of-bounds handling must not
    use the character's delegate or affect the character itself.
    """

    def __init__(self, controller: "EffectsController") -> None:
        self._controller_ref = weakref.ref(controller)
        self.node: Any | None = None
        self._removing = False

    def handlemessage(self, message: Any) -> None:
        if isinstance(message, (bs.OutOfBoundsMessage, bs.DieMessage)):
            self._remove_node()

    def _remove_node(self) -> None:
        node_obj = self.node
        if node_obj is None or self._removing:
            return

        self._removing = True
        try:
            if _node_is_live(node_obj):
                node_obj.delete()
        except Exception:
            _LOG.debug(
                "Could not remove an out-of-bounds temporary effect node.",
                exc_info=True,
            )

        # Release references only after the engine confirms that the node is
        # gone. The controller's scheduled cleanup can retry a failed deletion.
        if not _node_is_live(node_obj):
            self.node = None
            controller = self._controller_ref()
            if controller is not None:
                controller._forget_node(node_obj)
        else:
            self._removing = False


class EffectsController(_EffectVisualsMixin):
    """Manages effects for one live character without replacing PlayerSpaz."""
    def __init__(self, spaz: Any, effects: Sequence[str]) -> None:
        self._spaz_ref = weakref.ref(spaz)
        self.effects = list(dict.fromkeys(effects))
        self._stopped = False
        # Integer keys allow multiple timers for a combined effect to coexist.
        # A name-keyed dictionary could overwrite handles and leave old loops alive.
        self._timers: dict[int, Any] = {}
        self._next_timer_id = 0
        self._active_effect_loops: set[str] = set()
        # Keying by wrapper identity makes node tracking O(1) instead of
        # rescanning all previously-created particle nodes on every emission.
        self._owned_nodes: dict[int, Any] = {}
        # Ballistica stores node delegates weakly, so retain each temporary
        # node's handler until the corresponding node has been deleted.
        self._temporary_node_delegates: dict[int, _TemporaryEffectNodeDelegate] = {}
        self._particle_collision_material: Any | None = None
        self._star_mesh: Any | None = None
        self._star_textures: tuple[Any, ...] | None = None
        self._chispitas_texture: Any | None = None
        self._darkmagic_mesh: Any | None = None
        self._darkmagic_texture: Any | None = None
        self._original_color = tuple(spaz.node.color) if spaz.node.exists() else None
        self._original_highlight = tuple(spaz.node.highlight) if spaz.node.exists() else None
        self.surround = None

    @property
    def spaz(self) -> Any | None:
        spaz_ref = self._spaz_ref
        return spaz_ref() if callable(spaz_ref) else None

    @property
    def node(self) -> Any:
        spaz = self.spaz
        return getattr(spaz, "node", None) if spaz is not None else None

    @property
    def _dead(self) -> bool:
        return not self._controller_active()

    def is_alive(self) -> bool:
        spaz = self.spaz
        if spaz is None:
            return False
        checker = getattr(spaz, "is_alive", None)
        try:
            return bool(checker()) if callable(checker) else bool(self.node and self.node.exists())
        except Exception:
            return False

    def _activity(self) -> Any | None:
        spaz = self.spaz
        getter = getattr(spaz, "_activity", None) if spaz is not None else None
        if callable(getter):
            try:
                activity = getter()
                if activity is not None:
                    return activity
            except babase._error.ActivityNotFoundError:
                # A PlayerSpaz can briefly outlive its activity during transitions.
                return None
            except Exception:
                _LOG.warning(
                    "Could not retrieve character activity through PlayerSpaz.",
                    exc_info=True,
                )
        return _get_activity_or_none()

    def _controller_active(self) -> bool:
        if self._stopped:
            return False
        node_obj = self.node
        try:
            return bool(self.is_alive() and node_obj is not None and node_obj.exists())
        except Exception:
            return False

    def _next_timer_key(self) -> int:
        self._next_timer_id += 1
        return self._next_timer_id

    def _track_node(self, node_obj: Any) -> None:
        """Track a live visual node by wrapper identity in constant time."""
        if _node_is_live(node_obj):
            self._owned_nodes[id(node_obj)] = node_obj

    def _forget_node(self, node_obj: Any) -> None:
        """Release our references to a scene node and its message delegate."""
        if node_obj is not None:
            key = id(node_obj)
            self._owned_nodes.pop(key, None)
            delegate = self._temporary_node_delegates.pop(key, None)
            if delegate is not None:
                delegate.node = None

    def _new_temporary_node(self, node_type: str, attrs: dict[str, Any]) -> Any | None:
        """Create a short-lived visual node with explicit OOB handling."""
        owner_node = self.node
        if self._stopped or not _node_is_live(owner_node):
            return None

        delegate = _TemporaryEffectNodeDelegate(self)
        node_obj = bs.newnode(
            node_type,
            owner=owner_node,
            delegate=delegate,
            attrs=attrs,
        )
        delegate.node = node_obj
        node_key = id(node_obj)
        self._owned_nodes[node_key] = node_obj
        self._temporary_node_delegates[node_key] = delegate
        return node_obj

    def _get_particle_collision_material(self) -> Any:
        """Create the common non-colliding punch-material rule only once."""
        material = self._particle_collision_material
        if material is None:
            factory = SpazFactory.get()
            material = bs.Material()
            material.add_actions(
                conditions=("they_have_material", factory.punch_material),
                actions=(
                    ("modify_part_collision", "collide", False),
                    ("modify_part_collision", "physical", False),
                ),
            )
            self._particle_collision_material = material
        return material

    def _get_star_assets(self) -> tuple[Any, tuple[Any, ...]]:
        """Cache the star prop mesh and the four randomly selected textures."""
        if self._star_mesh is None:
            self._star_mesh = bs.getmesh("flash")
        if self._star_textures is None:
            self._star_textures = tuple(
                bs.gettexture(name) for name in _STAR_TEXTURE_NAMES
            )
        return self._star_mesh, self._star_textures

    def _get_chispitas_texture(self) -> Any:
        """Cache the null texture used by chispitas particles."""
        if self._chispitas_texture is None:
            self._chispitas_texture = bs.gettexture("null")
        return self._chispitas_texture

    def _get_darkmagic_assets(self) -> tuple[Any, Any]:
        """Cache the mesh and texture shared by darkmagic particles."""
        if self._darkmagic_mesh is None:
            self._darkmagic_mesh = bs.getmesh("impactBomb")
        if self._darkmagic_texture is None:
            self._darkmagic_texture = bs.gettexture("impactBombColor")
        return self._darkmagic_mesh, self._darkmagic_texture

    def _delete_node_if_live(self, node_obj: Any) -> None:
        """Delete a temporary node and untrack it once it is actually gone."""
        if not _node_is_live(node_obj):
            self._forget_node(node_obj)
            return
        try:
            node_obj.delete()
        except Exception:
            # Keep tracking it so controller.stop() can make a final cleanup
            # attempt instead of discarding the last reference after a failure.
            _LOG.warning("Could not delete a temporary effect node.", exc_info=True)
            return
        if not _node_is_live(node_obj):
            self._forget_node(node_obj)

    def _shrink_and_delete_node(self, node_obj: Any) -> None:
        """Shrink a temporary visual prop, then delete it even if animation fails."""
        if not _node_is_live(node_obj):
            self._forget_node(node_obj)
            return
        try:
            current_scale = node_obj.mesh_scale
            bs.animate(node_obj, "mesh_scale", {0: current_scale, 0.1: 0})
        except Exception:
            _LOG.warning(
                "Could not animate a temporary effect node before cleanup.",
                exc_info=True,
            )
        self._schedule_delayed(
            0.1,
            functools.partial(self._delete_node_if_live, node_obj),
            "temporary-particle-delete",
        )

    def _set_base_color_if_active(self, color: Sequence[float]) -> None:
        """Apply one new-rainbow color only while this controller is active."""
        if self._controller_active():
            node_obj = self.node
            if node_obj is not None and node_obj.exists():
                node_obj.color = color

    def _describe_delayed_callback(self, callback: Any) -> tuple[Any, ...]:
        """Describe callbacks without storing a bound reference to this controller."""
        if isinstance(callback, functools.partial):
            function = callback.func
            if getattr(function, "__self__", None) is self:
                return (
                    "method",
                    function.__name__,
                    tuple(callback.args),
                    dict(callback.keywords or {}),
                )
        if getattr(callback, "__self__", None) is self:
            return ("method", callback.__name__, (), {})
        # The only non-method callback used here is a small standalone animation
        # closure; it does not capture this controller.
        return ("callable", callback)

    def _run_delayed_callback(
        self, key: int, callback_spec: tuple[Any, ...], label: str
    ) -> None:
        """Run one delayed task via a weak timer callback."""
        self._timers.pop(key, None)
        if self._stopped:
            return
        if not self._controller_active():
            self.stop()
            return
        try:
            if callback_spec[0] == "method":
                _kind, method_name, args, kwargs = callback_spec
                getattr(self, method_name)(*args, **kwargs)
            else:
                callback_spec[1]()
        except Exception:
            _LOG.exception("Delayed custom-effect callback '%s' failed.", label)

    def _schedule_delayed(self, delay: float, callback: Any, label: str = "delayed") -> None:
        """Schedule a one-shot task without making the timer retain this controller."""
        if self._stopped:
            return
        key = self._next_timer_key()
        callback_spec = self._describe_delayed_callback(callback)
        self._timers[key] = _timer(
            max(0.0, float(delay)),
            babase.WeakCallStrict(self._run_delayed_callback, key, callback_spec, label),
        )

    def _run_repeating_effect(
        self,
        name: str,
        interval: float,
        callback: Any,
        timer_key: int | None = None,
    ) -> None:
        """Run an effect and schedule its next iteration using a weak callback."""
        if timer_key is not None:
            self._timers.pop(timer_key, None)
        if self._stopped or name not in self._active_effect_loops:
            self._active_effect_loops.discard(name)
            return
        if not self._controller_active():
            self._active_effect_loops.discard(name)
            self.stop()
            return
        activity = self._activity()
        if activity is None or activity.expired:
            self._active_effect_loops.discard(name)
            self.stop()
            return
        try:
            with activity.context:
                # callback is an unbound function; it does not capture self.
                callback(self)
        except Exception:
            self._active_effect_loops.discard(name)
            _LOG.exception("Custom effect loop '%s' failed.", name)
            return

        if interval <= 0:
            self._active_effect_loops.discard(name)
            return
        if self._stopped or name not in self._active_effect_loops:
            return
        key = self._next_timer_key()
        try:
            with activity.context:
                self._timers[key] = _timer(
                    max(0.0, float(interval)),
                    babase.WeakCallStrict(
                        self._run_repeating_effect, name, interval, callback, key
                    ),
                )
        except Exception:
            self._active_effect_loops.discard(name)
            _LOG.exception("Could not schedule custom effect loop '%s'.", name)

    def _schedule_repeating_effect(self, name: str, interval: float, callback: Any) -> None:
        """Apply an effect immediately, then repeat it at the configured interval."""
        if self._stopped or name in self._active_effect_loops:
            return
        self._active_effect_loops.add(name)
        self._run_repeating_effect(name, interval, callback)

    def _schedule_node_watch(self, name: str, node_obj: Any, interval: float) -> None:
        """Watch an effect-owned node without a timer retaining its wrapper."""
        if self._stopped or interval <= 0 or node_obj is None:
            return
        self._run_node_watch(name, id(node_obj), interval)

    def _run_node_watch(
        self,
        name: str,
        node_key: int,
        interval: float,
        timer_key: int | None = None,
    ) -> None:
        """Run one weakly scheduled node-liveness check."""
        if timer_key is not None:
            self._timers.pop(timer_key, None)
        if self._stopped:
            return
        node_obj = self._owned_nodes.get(node_key)
        if node_obj is None:
            return
        if not self._controller_active():
            self.stop()
            return
        if not _node_is_live(node_obj):
            self._forget_node(node_obj)
            return
        activity = self._activity()
        if activity is None or activity.expired:
            self.stop()
            return
        key = self._next_timer_key()
        try:
            with activity.context:
                self._timers[key] = _timer(
                    max(0.0, float(interval)),
                    babase.WeakCallStrict(
                        self._run_node_watch, name, node_key, interval, key
                    ),
                )
        except Exception:
            _LOG.exception("Could not schedule node watch for effect '%s'.", name)


    def start(self) -> None:
        if not self._controller_active():
            return
        # Store unbound functions, not bound methods. Bound methods capture this
        # controller and can form a self-cycle that delays Activity teardown.
        self._effect_mappings = {
            "spark": _EffectVisualsMixin._add_spark,
            "sparkground": _EffectVisualsMixin._add_sparkground,
            "sweat": _EffectVisualsMixin._add_sweat,
            "sweatground": _EffectVisualsMixin._add_sweatground,
            "distortion": _EffectVisualsMixin._add_distortion,
            "glow": _EffectVisualsMixin._add_glow,
            "shine": _EffectVisualsMixin._add_shine,
            "highlightshine": _EffectVisualsMixin._add_highlightshine,
            "scorch": _EffectVisualsMixin._add_scorch,
            "ice": _EffectVisualsMixin._add_ice,
            "iceground": _EffectVisualsMixin._add_iceground,
            "slime": _EffectVisualsMixin._add_slime,
            "metal": _EffectVisualsMixin._add_metal,
            "splinter": _EffectVisualsMixin._add_splinter,
            "rainbow": _EffectVisualsMixin._add_rainbow,
            "fairydust": _EffectVisualsMixin._add_fairydust,
            "surrounder": _EffectVisualsMixin._add_surround,
            "fire": _EffectVisualsMixin._add_fire,
            "stars": _EffectVisualsMixin._add_stars,
            "new_rainbow": _EffectVisualsMixin._add_new_rainbow,
            "footprint": _EffectVisualsMixin._add_footprint,
            "chispitas": _EffectVisualsMixin._add_chispitas,
            "darkmagic": _EffectVisualsMixin._add_darkmagic,
            "colorfullspark": _EffectVisualsMixin._add_colorful_spark,
            "ring": _EffectVisualsMixin._add_aure,
            "brust": _EffectVisualsMixin._add_galactic_burst,
            "ringstars": _EffectVisualsMixin._add_star_ring,
            "noeffect": lambda _controller: None,
        }
        activity = self._activity()
        if activity is None:
            _LOG.debug("Skipping custom effects because no gameplay activity is active.")
            return
        for effect_name in self.effects:
            trigger = self._effect_mappings.get(effect_name)
            if trigger is None:
                _LOG.warning("Ignoring unknown stored effect name %r.", effect_name)
                continue
            try:
                with activity.context:
                    trigger(self)
            except Exception:
                _LOG.exception("Could not start custom effect '%s'.", effect_name)

    def stop(self) -> None:
        """Stop effect callbacks and release every owned reference safely."""
        if self._stopped:
            return
        # Mark stopped before releasing timers so already-queued callbacks are inert.
        self._stopped = True
        self._active_effect_loops.clear()
        self._timers.clear()  # bs.Timer is reference-managed; dropping handles cancels it.

        activity = self._activity()
        can_touch_scene = activity is not None and not activity.expired
        surround = self.surround
        self.surround = None
        owned_nodes = list(self._owned_nodes.values())
        self._owned_nodes.clear()
        temporary_delegates = list(self._temporary_node_delegates.values())
        self._temporary_node_delegates.clear()

        def cleanup_scene() -> None:
            if surround is not None:
                try:
                    surround.handlemessage(bs.DieMessage(immediate=True))
                except Exception:
                    _LOG.debug("Could not remove surrounder node during cleanup.", exc_info=True)
            for node_obj in owned_nodes:
                _safe_delete_node(node_obj)
            for delegate in temporary_delegates:
                delegate.node = None
            node_obj = self.node
            if node_obj is not None and _node_is_live(node_obj):
                try:
                    if self._original_color is not None:
                        bs.animate_array(node_obj, "color", 3, {0.0: self._original_color})
                        node_obj.color = self._original_color
                    if self._original_highlight is not None:
                        bs.animate_array(node_obj, "highlight", 3, {0.0: self._original_highlight})
                        node_obj.highlight = self._original_highlight
                except Exception:
                    _LOG.debug("Could not restore character colors during effects cleanup.", exc_info=True)

        if can_touch_scene:
            try:
                with activity.context:
                    cleanup_scene()
            except Exception:
                _LOG.debug("Scene cleanup could not complete in its Activity context.", exc_info=True)
        else:
            # No scene operations are valid during/after Activity expiry. The
            # Activity tears down nodes; still notify the actor to clear Python refs.
            if surround is not None:
                try:
                    surround.on_expire()
                except Exception:
                    _LOG.debug("Could not release expired surrounder references.", exc_info=True)
            for delegate in temporary_delegates:
                delegate.node = None

        # Ensure neither the controller nor its bound callbacks keep scene wrappers,
        # cached assets, the character weakref, or original color tuples alive.
        self._effect_mappings = {}
        self.effects.clear()
        self._particle_collision_material = None
        self._star_mesh = None
        self._star_textures = None
        self._chispitas_texture = None
        self._darkmagic_mesh = None
        self._darkmagic_texture = None
        self._original_color = None
        self._original_highlight = None
        self._spaz_ref = None
        self.surround = None



def refresh_player_effects(spaz: Any, account_id: str | None) -> bool:
    """Immediately refresh a live character using its saved V2 effects."""
    if spaz is None or not is_v2_account_id(account_id):
        return False

    activity = None
    getter = getattr(spaz, "_activity", None)
    if callable(getter):
        try:
            activity = getter()
        except babase._error.ActivityNotFoundError:
            # Activity teardown can race with effect refresh.
            activity = None
        except Exception:
            _LOG.warning(
                "Could not retrieve activity while refreshing custom effects.",
                exc_info=True,
            )
            activity = None
    if activity is None:
        activity = _get_activity_or_none()
    if activity is None:
        return False

    try:
        with activity.context:
            node_obj = getattr(spaz, "node", None)
            if node_obj is None or not node_obj.exists():
                return False

            previous = getattr(spaz, "_lucifer_custom_effect_controller", None)
            if previous is not None:
                try:
                    previous.stop()
                except Exception:
                    _LOG.exception("Could not stop old custom effects before refresh.")
                try:
                    spaz._lucifer_custom_effect_controller = None
                except Exception:
                    _LOG.warning(
                        "Could not clear the previous custom-effect controller "
                        "reference after refresh.",
                        exc_info=True,
                    )

            try:
                effects = pdata.get_custom_effects(account_id)
            except Exception:
                _LOG.exception("Could not load stored custom effects for %s.", account_id)
                return False

            # Preserve every valid manually assigned effect, then add the one
            # rank-based effect selected by setting.json when Top 3 effects are on.
            # The automatic effect is intentionally not persisted in effects.json.
            normalized = [
                name for name in (normalize_effect_name(item) for item in effects)
                if name
            ]
            automatic_effect = _configured_top3_effect(account_id)
            if automatic_effect and automatic_effect not in normalized:
                normalized.append(automatic_effect)

            if not normalized:
                return True

            controller = EffectsController(spaz, normalized)
            spaz._lucifer_custom_effect_controller = controller
            controller.start()
            # start() safely skips unavailable activities, but refresh only counts
            # as successful when there was a valid live character node.
            return True
    except Exception:
        _LOG.exception("Could not initialize custom effects for V2 account %s.", account_id)
        return False


def apply_player_effects(spaz: Any, player_or_session_player: Any) -> bool:
    """Load and apply the stored custom effects using only a validated V2 ID."""
    account_id = _player_v2_id(player_or_session_player)
    if not is_v2_account_id(account_id):
        # Use the same Activity-context-aware cleanup path used by live refresh.
        clear_player_effects(spaz)
        return False
    return refresh_player_effects(spaz, account_id)


def clear_player_effects(spaz: Any) -> None:
    """Stop managed custom effects on a character without touching its tag."""
    if spaz is None:
        return
    activity = None
    getter = getattr(spaz, "_activity", None)
    if callable(getter):
        try:
            activity = getter()
        except babase._error.ActivityNotFoundError:
            # Activity teardown can race with effect cleanup.
            activity = None
        except Exception:
            _LOG.warning(
                "Could not retrieve activity while clearing custom effects.",
                exc_info=True,
            )
            activity = None
    if activity is None:
        activity = _get_activity_or_none()

    def _clear() -> None:
        previous = getattr(spaz, "_lucifer_custom_effect_controller", None)
        if previous is not None:
            try:
                previous.stop()
            except Exception:
                _LOG.exception("Could not clean up a character's custom effects.")
            try:
                spaz._lucifer_custom_effect_controller = None
            except Exception:
                _LOG.warning(
                    "Could not clear the custom-effect controller reference "
                    "during cleanup.",
                    exc_info=True,
                )

    try:
        if activity is None:
            _clear()
        else:
            with activity.context:
                _clear()
    except Exception:
        _LOG.exception("Could not clear custom effects in the character activity context.")
# Powerup-compatible visuals. These intentionally avoid the PlayerSpaz-specific
# EffectsController above: powerup boxes expose a position, not torso_position,
# highlight, or the other character-only attributes.
POWERUP_EFFECT_NAMES = frozenset({
    "spark", "sparkground", "sweat", "sweatground", "distortion", "glow",
    "scorch", "ice", "iceground", "slime", "metal", "splinter",
    "fairydust", "fire", "stars", "chispitas", "darkmagic", "footprint",
    "ring", "colorfullspark", "brust", "ringstars", "noeffect",
})


def normalize_powerup_effect_name(value: Any) -> str | None:
    """Return a supported powerup effect name, or None for incompatible effects."""
    if not isinstance(value, str):
        return None
    name = value.strip().casefold()
    return name if name in POWERUP_EFFECT_NAMES else None


class PowerupEffectController:
    """Emit a safe visual effect at a powerup's position until it is removed.

    The controller is owned by the Powerup Manager's record. It uses weak timer
    callbacks and cleans up temporary light/scorch/locator nodes when stopped.
    Character-only effects are deliberately not reused against powerup nodes.
    """

    def __init__(
        self,
        node: Any,
        effect_name: str,
        continuous: bool = True,
        interval_seconds: float = 0.25,
        bs_module: Any = None,
    ) -> None:
        self._node = node
        self.requested_effect_name = str(effect_name)
        self.effect_name = normalize_powerup_effect_name(effect_name)
        self.continuous = bool(continuous)
        try:
            interval = float(interval_seconds)
        except (TypeError, ValueError):
            interval = 0.25
        self.interval_seconds = min(5.0, max(0.15, interval))
        self._bs = bs_module or bs
        self._stopped = False
        self._started = False
        self._repeat_timer: Any | None = None
        self._cleanup_timers: dict[int, Any] = {}
        self._temporary_nodes: dict[int, Any] = {}

    def start(self) -> bool:
        """Emit once and optionally schedule repeated emissions."""
        if self._started or self._stopped:
            return False
        if self.effect_name is None:
            _LOG.warning(
                "Unsupported powerup effect %r. Supported powerup effects: %s",
                self.requested_effect_name,
                ", ".join(sorted(POWERUP_EFFECT_NAMES)),
            )
            return False
        if not _node_is_live(self._node):
            return False
        self._started = True
        if self.effect_name == "noeffect":
            return True
        self._emit_once()
        if self.continuous and not self._stopped and _node_is_live(self._node):
            self._schedule_next()
        return not self._stopped

    def _schedule_next(self) -> None:
        if self._stopped:
            return
        try:
            activity = _get_activity_or_none()
            if activity is None or activity.expired:
                self.stop()
                return
            with activity.context:
                self._repeat_timer = self._bs.Timer(
                    self.interval_seconds,
                    babase.WeakCallStrict(self._tick),
                )
        except Exception:
            _LOG.exception("Could not schedule powerup effect %r.", self.effect_name)
            self.stop()

    def _tick(self) -> None:
        self._repeat_timer = None
        if self._stopped:
            return
        if not _node_is_live(self._node):
            self.stop()
            return
        self._emit_once()
        if not self._stopped and _node_is_live(self._node):
            self._schedule_next()
        else:
            self.stop()

    def _emit_once(self) -> None:
        """Emit the selected effect from the powerup's current world position."""
        if self._stopped or not _node_is_live(self._node):
            self.stop()
            return
        activity = _get_activity_or_none()
        if activity is None or activity.expired:
            self.stop()
            return
        try:
            with activity.context:
                raw_pos = self._node.position
                position = (float(raw_pos[0]), float(raw_pos[1]), float(raw_pos[2]))
                self._emit_at(position)
        except Exception:
            _LOG.exception("Powerup effect %r failed; stopping this effect controller.", self.effect_name)
            self.stop()

    def _emit_particles(
        self,
        position: Sequence[float],
        *,
        chunk_type: str | None = None,
        emit_type: str | None = None,
        tendril_type: str | None = None,
        count: int = 4,
        scale: float = 0.4,
        spread: float = 0.25,
        velocity: Sequence[float] = (0.0, 0.15, 0.0),
    ) -> None:
        kwargs: dict[str, Any] = {
            "position": position,
            "velocity": velocity,
            "count": count,
            "scale": scale,
            "spread": spread,
        }
        if chunk_type is not None:
            kwargs["chunk_type"] = chunk_type
        if emit_type is not None:
            kwargs["emit_type"] = emit_type
        if tendril_type is not None:
            kwargs["tendril_type"] = tendril_type
        self._bs.emitfx(**kwargs)

    def _add_temporary_node(self, node_obj: Any, lifetime: float = 0.8) -> None:
        if not _node_is_live(node_obj):
            return
        key = id(node_obj)
        self._temporary_nodes[key] = node_obj
        try:
            activity = _get_activity_or_none()
            if activity is None or activity.expired:
                return
            with activity.context:
                self._cleanup_timers[key] = self._bs.Timer(
                    max(0.1, float(lifetime)),
                    babase.WeakCallStrict(self._delete_temporary_node, key),
                )
        except Exception:
            _LOG.debug("Could not schedule temporary powerup-effect cleanup.", exc_info=True)

    def _delete_temporary_node(self, key: int) -> None:
        self._cleanup_timers.pop(key, None)
        node_obj = self._temporary_nodes.pop(key, None)
        if node_obj is None:
            return
        activity = _get_activity_or_none()
        if activity is None or activity.expired:
            return
        try:
            with activity.context:
                _safe_delete_node(node_obj)
        except Exception:
            _LOG.debug("Could not delete a temporary powerup-effect node.", exc_info=True)

    def _create_glow(self, position: Sequence[float]) -> None:
        light = self._bs.newnode(
            "light",
            owner=self._node,
            attrs={
                "position": position,
                "color": (0.65, 0.15, 1.0) if self.effect_name in {"darkmagic", "ringstars"} else (1.0, 0.75, 0.25),
                "height_attenuated": False,
                "radius": 0.32,
            },
        )
        self._node.connectattr("position", light, "position")
        self._bs.animate(light, "intensity", {0.0: 0.0, 0.1: 0.45, 0.45: 0.15, 0.8: 0.0})
        self._add_temporary_node(light, 0.85)

    def _create_scorch(self, position: Sequence[float]) -> None:
        scorch = self._bs.newnode(
            "scorch",
            owner=self._node,
            attrs={"position": position, "size": 0.65, "big": False},
        )
        self._node.connectattr("position", scorch, "position")
        self._bs.animate_array(
            scorch,
            "color",
            3,
            {0.0: (1.0, 0.25, 0.0), 0.2: (0.75, 0.0, 1.0), 0.5: (0.15, 0.0, 0.4)},
        )
        self._add_temporary_node(scorch, 0.65)

    def _create_ring(self, position: Sequence[float]) -> None:
        locator = self._bs.newnode(
            "locator",
            owner=self._node,
            attrs={
                "position": position,
                "shape": "circleOutline",
                "color": (0.8, 0.3, 1.0),
                "opacity": 0.9,
                "draw_beauty": True,
                "additive": True,
                "size": [0.25],
            },
        )
        self._node.connectattr("position", locator, "position")
        self._bs.animate_array(locator, "size", 1, {0.0: [0.15], 0.25: [0.8], 0.55: [1.1]})
        self._bs.animate(locator, "opacity", {0.0: 0.9, 0.35: 0.6, 0.6: 0.0})
        self._add_temporary_node(locator, 0.65)

    def _emit_at(self, position: Sequence[float]) -> None:
        name = self.effect_name
        if name == "noeffect":
            return
        if name == "spark":
            self._emit_particles(
                position, chunk_type="spark", count=random.randint(18, 30),
                scale=1.25, spread=0.75, velocity=(0.0, 0.8, 0.0),
            )
        elif name == "sparkground":
            self._emit_particles(position, chunk_type="spark", emit_type="stickers", count=3, scale=0.25, spread=0.5)
        elif name == "sweat":
            self._emit_particles(
                position, chunk_type="sweat", count=random.randint(18, 28),
                scale=1.35, spread=0.55, velocity=(0.0, 0.75, 0.0),
            )
        elif name == "sweatground":
            self._emit_particles(position, chunk_type="sweat", emit_type="stickers", count=3, scale=0.35, spread=0.4)
        elif name == "distortion":
            self._emit_particles(position, emit_type="distortion", count=2, scale=0.4, spread=0.45)
            self._emit_particles(position, emit_type="tendrils", tendril_type="smoke", count=2, scale=0.35, spread=0.25)
        elif name == "glow":
            self._create_glow(position)
        elif name == "scorch":
            self._create_scorch(position)
        elif name == "ice":
            self._emit_particles(
                position, chunk_type="ice", count=random.randint(16, 28),
                scale=1.2, spread=0.7, velocity=(0.0, 0.65, 0.0),
            )
        elif name == "iceground":
            self._emit_particles(position, chunk_type="ice", emit_type="stickers", count=2, scale=0.3, spread=0.55)
        elif name == "slime":
            self._emit_particles(
                position, chunk_type="slime", count=random.randint(16, 30),
                scale=1.25, spread=0.7, velocity=(0.0, 0.65, 0.0),
            )
        elif name == "metal":
            self._emit_particles(
                position, chunk_type="metal", count=random.randint(16, 28),
                scale=1.25, spread=0.65, velocity=(0.0, 0.65, 0.0),
            )
        elif name == "splinter":
            self._emit_particles(position, chunk_type="splinter", count=random.randint(2, 4), scale=0.45)
        elif name == "fairydust":
            self._emit_particles(position, emit_type="fairydust", count=6, scale=0.45, spread=0.75)
        elif name == "fire":
            # Match the existing custom effect's sweat-style particle implementation.
            self._emit_particles(position, chunk_type="sweat", count=7, scale=0.75, spread=0.35, velocity=(0.0, 0.6, 0.0))
        elif name == "stars":
            self._emit_particles(position, chunk_type="spark", count=4, scale=0.3, spread=0.4, velocity=(0.0, 0.5, 0.0))
            self._emit_particles(position, emit_type="fairydust", count=3, scale=0.3, spread=0.4)
        elif name == "chispitas":
            self._emit_particles(position, chunk_type="spark", count=3, scale=0.25, spread=0.45)
            self._emit_particles(position, emit_type="fairydust", count=3, scale=0.25, spread=0.4)
        elif name == "darkmagic":
            self._create_glow(position)
            self._emit_particles(position, emit_type="distortion", count=2, scale=0.35, spread=0.4)
            self._emit_particles(position, emit_type="tendrils", tendril_type="smoke", count=2, scale=0.35, spread=0.25, velocity=(0.0, 0.25, 0.0))
        elif name == "footprint":
            locator = self._bs.newnode(
                "locator",
                owner=self._node,
                attrs={
                    "position": position,
                    "shape": "circle",
                    "color": (0.7, 0.25, 1.0),
                    "opacity": 0.8,
                    "draw_beauty": False,
                    "additive": True,
                    "size": [0.25],
                },
            )
            self._node.connectattr("position", locator, "position")
            self._bs.animate(locator, "opacity", {0.0: 0.8, 0.5: 0.0})
            self._add_temporary_node(locator, 0.55)
        elif name == "ring":
            self._create_ring(position)
        elif name == "colorfullspark":
            self._emit_particles(position, chunk_type="spark", count=4, scale=0.4, spread=0.4)
            self._emit_particles(position, chunk_type="sweat", emit_type="stickers", count=2, scale=0.3, spread=0.4)
            self._emit_particles(position, emit_type="fairydust", count=3, scale=0.3, spread=0.45)
        elif name == "brust":
            self._create_glow(position)
            self._emit_particles(position, emit_type="distortion", count=2, scale=0.3, spread=0.45)
            self._emit_particles(position, emit_type="fairydust", count=5, scale=0.4, spread=0.7)
            self._emit_particles(position, chunk_type="spark", count=3, scale=0.35, spread=0.4)
        elif name == "ringstars":
            self._create_ring(position)
            self._emit_particles(position, emit_type="fairydust", count=5, scale=0.35, spread=0.55)
            self._emit_particles(position, chunk_type="spark", count=2, scale=0.3, spread=0.35)

    def stop(self) -> None:
        """Cancel timers and delete temporary nodes while scene access is valid."""
        if self._stopped:
            return
        self._stopped = True
        self._repeat_timer = None
        # Releasing reference-managed bs.Timer objects cancels their pending calls.
        self._cleanup_timers.clear()
        nodes = list(self._temporary_nodes.values())
        self._temporary_nodes.clear()
        activity = _get_activity_or_none()
        if activity is None or activity.expired:
            # The Activity/owning powerup node will tear down scene nodes.
            return
        try:
            with activity.context:
                for node_obj in nodes:
                    _safe_delete_node(node_obj)
        except Exception:
            _LOG.debug("Could not clean temporary powerup effects during stop.", exc_info=True)
