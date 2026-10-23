# ba_meta require api 9
"""
Lucious Celebration FX - score-screen effects for BombSquad (API 9).

Turns every score / victory screen into a party: a rainbow
"MADE BY LUCIOUS" banner, fireworks, lightning, meteors, bomb rain,
powerup rain, falling bots, shaking bots, disco tint and more.

Author:  Lucious
Version: 2.0.0
License: MIT
"""

from __future__ import annotations

import functools
import importlib
import logging
import math
import random
import weakref
from dataclasses import dataclass
from typing import Any, Callable

import babase
import bascenev1 as bs

__author__ = 'Lucious'
__version__ = '2.0.0'

_log = logging.getLogger('lucious_fx')


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LuciousConfig:
    """All tweakable settings live here."""

    signature: str = 'MADE BY LUCIOUS'
    show_banner: bool = True
    signature_chance: float = 0.5      # chance per tick of a signature popup
    tick_interval: float = 0.25        # seconds between ambient effects
    effects_per_tick: tuple[int, int] = (1, 2)
    rain_interval: float = 2.2         # seconds between rain waves
    enable_bombs: bool = True
    enable_bots: bool = True
    enable_powerups: bool = True
    enable_disco_tint: bool = True
    max_bots: int = 8
    max_bombs: int = 10
    max_powerups: int = 8
    max_failures: int = 3              # auto-disable a broken effect
    area_x: float = 15.0
    area_y: tuple[float, float] = (5.0, 15.0)
    area_z: float = 15.0


CONFIG = LuciousConfig()

Color = tuple[float, float, float]
Vec = tuple[float, float, float]

_PALETTES: tuple[tuple[Color, ...], ...] = (
    ((2.0, 0.2, 0.2), (2.0, 1.2, 0.0), (2.0, 2.0, 0.3)),  # fire
    ((0.2, 0.8, 2.0), (0.4, 2.0, 2.0), (1.6, 1.6, 2.0)),  # ice
    ((2.0, 0.3, 1.6), (1.2, 0.3, 2.0), (0.4, 0.4, 2.0)),  # neon
    ((0.3, 2.0, 0.4), (1.2, 2.0, 0.2), (0.2, 2.0, 1.2)),  # toxic
    ((2.0, 1.6, 0.4), (2.0, 2.0, 2.0), (1.8, 1.2, 0.2)),  # gold
)
_HYPE_WORDS = (
    'GG!', 'EPIC!', 'BOOM!', 'LEGEND!', 'WOW!', 'CHAMPION!', 'MVP!',
    'UNSTOPPABLE!', 'FLAWLESS!', 'GOD MODE!', 'TOO EASY!', 'CLUTCH!',
)
_STINGS = (
    'cheer', 'orchestraHit', 'orchestraHit2', 'gong', 'dingSmallHigh',
    'achievement', 'shieldUp', 'powerup01', 'cashRegister', 'woo',
)
_POWERUP_TYPES = (
    'triple_bombs', 'ice_bombs', 'punch', 'impact_bombs', 'land_mines',
    'sticky_bombs', 'shield', 'health', 'curse',
)
_BOT_CLASS_NAMES = (
    'BomberBot', 'BomberBotPro', 'BomberBotProShielded', 'BrawlerBot',
    'BrawlerBotPro', 'BrawlerBotProShielded', 'TriggerBot', 'TriggerBotPro',
    'TriggerBotProShielded', 'ChargerBot', 'ChargerBotPro',
    'ChargerBotProShielded', 'StickyBot', 'BouncyBot',
)

_WeakCall = getattr(bs, 'WeakCallStrict', None) or bs.WeakCall


# ---------------------------------------------------------------------------
# Per-activity state
# ---------------------------------------------------------------------------


@dataclass
class _Shaker:
    ref: weakref.ref
    base: Vec
    ends_at: float


class _FxState:
    """Bookkeeping for everything spawned on one score screen."""

    def __init__(self) -> None:
        self.running = False
        self.nodes: list[bs.Node] = []
        self.actors: list[weakref.ref] = []
        self.bots: list[weakref.ref] = []
        self.bombs: list[weakref.ref] = []
        self.powerups: list[weakref.ref] = []
        self.shakers: list[_Shaker] = []
        self.timers: list[bs.Timer] = []
        self.base_tint: Color | None = None

    def prune(self) -> None:
        self.nodes = [n for n in self.nodes if n.exists()]
        for name in ('actors', 'bots', 'bombs', 'powerups'):
            alive = [r for r in getattr(self, name) if _actor_alive(r())]
            setattr(self, name, alive)


_STATE_ATTR = '_lucious_fx_state'
_SCORES_SHOWN_ATTR = '_lucious_scores_shown'
_FFA_SKIP_ATTR = '_lucious_ffa_skip_scores'


def _state(activity: Any) -> _FxState:
    st = getattr(activity, _STATE_ATTR, None)
    if st is None:
        st = _FxState()
        setattr(activity, _STATE_ATTR, st)
    return st


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _safe_delete(node: Any) -> None:
    try:
        if node is not None and node.exists():
            node.delete()
    except Exception:
        _log.exception('Lucious FX: failed to delete node')


def _actor_alive(actor: Any) -> bool:
    node = getattr(actor, 'node', None) if actor is not None else None
    try:
        return node is not None and node.exists()
    except Exception:
        return False


def _rand_pos(y_range: tuple[float, float] | None = None) -> Vec:
    low, high = y_range or CONFIG.area_y
    return (
        random.uniform(-CONFIG.area_x, CONFIG.area_x),
        random.uniform(low, high),
        random.uniform(-CONFIG.area_z, CONFIG.area_z),
    )


def _rand_color() -> Color:
    return (random.random() * 2.0, random.random() * 2.0, random.random() * 2.0)


def _pick_color() -> Color:
    if random.random() < 0.6:
        return random.choice(random.choice(_PALETTES))
    return _rand_color()


def _track_node(st: _FxState, node: bs.Node, lifetime: float | None = None) -> bs.Node:
    st.nodes.append(node)
    if lifetime is not None:
        bs.timer(lifetime, functools.partial(_safe_delete, node))
    return node


def _retain(bucket: list[weakref.ref], actor: Any) -> Any:
    actor.autoretain()
    bucket.append(weakref.ref(actor))
    return actor


def _later(st: _FxState, delay: float, fn: Callable[..., Any], *args: Any) -> None:
    """Run fn after delay, only while the effects are still running."""

    def _run() -> None:
        if not st.running:
            return
        try:
            fn(*args)
        except Exception:
            _log.exception('Lucious FX: delayed call failed')

    bs.timer(delay, _run)


def _kill_later(st: _FxState, actor: Any, delay: float, immediate: bool = False) -> None:
    ref = weakref.ref(actor)

    def _kill() -> None:
        target = ref()
        if _actor_alive(target):
            target.handlemessage(bs.DieMessage(immediate=immediate))

    _later(st, delay, _kill)


# ---------------------------------------------------------------------------
# Visual / audio primitives
# ---------------------------------------------------------------------------


def _explosion(st: _FxState, pos: Vec, color: Color, radius: float = 3.0,
               big: bool = False) -> None:
    node = bs.newnode('explosion', attrs={'position': pos, 'radius': radius, 'big': big})
    node.color = color
    _track_node(st, node, 1.0)


def _light_pulse(st: _FxState, pos: Vec, color: Color, radius: float = 0.6,
                 peak: float = 1.5, duration: float = 0.8) -> None:
    light = bs.newnode('light', attrs={
        'position': pos, 'color': color, 'radius': radius, 'intensity': 0.0,
        'height_attenuated': False, 'volume_intensity_scale': 10.0,
    })
    bs.animate(light, 'intensity', {0.0: 0.0, duration * 0.25: peak, duration: 0.0})
    _track_node(st, light, duration + 0.05)


def _flash(st: _FxState, pos: Vec, color: Color, size: float = 1.0) -> None:
    node = bs.newnode('flash', attrs={'position': pos, 'size': size, 'color': color})
    _track_node(st, node, 0.12)


def _chunks(pos: Vec, chunk_type: str, count: int = 30, scale: float = 1.0,
            spread: float = 1.0, velocity: Vec = (0.0, 0.0, 0.0)) -> None:
    bs.emitfx(position=pos, velocity=velocity, count=count, scale=scale,
              spread=spread, chunk_type=chunk_type)


def _tendrils(pos: Vec, tendril_type: str, count: int = 10, spread: float = 1.0) -> None:
    bs.emitfx(position=pos, emit_type='tendrils', tendril_type=tendril_type,
              count=count, spread=spread)


def _boom_sound(pos: Vec) -> None:
    from bascenev1lib.actor.bomb import BombFactory
    BombFactory.get().random_explode_sound().play(position=pos)


def _sound(name: str, pos: Vec | None = None, volume: float = 1.0) -> None:
    bs.getsound(name).play(volume=volume, position=pos)


def _popup(st: _FxState, text: str, pos: Vec, color: Color, scale: float = 2.5) -> None:
    from bascenev1lib.actor.popuptext import PopupText
    popup = PopupText(text, position=pos, color=(color[0], color[1], color[2], 1.0),
                      scale=scale)
    _retain(st.actors, popup)


def _firework(st: _FxState, pos: Vec, color: Color, radius: float = 4.0) -> None:
    _explosion(st, pos, color, radius)
    _chunks(pos, 'spark', count=40, scale=1.6, spread=1.2)
    bs.emitfx(position=pos, emit_type='distortion', spread=2.0)
    _boom_sound(pos)


# ---------------------------------------------------------------------------
# Effect registry
# ---------------------------------------------------------------------------

Effect = Callable[[Any, _FxState], None]
_EFFECTS: list[tuple[float, Effect]] = []
_RAINS: list[tuple[float, Effect]] = []
_FAILURES: dict[str, int] = {}


def effect(weight: float = 1.0, *, rain: bool = False) -> Callable[[Effect], Effect]:
    """Register an effect. Higher weight = shows up more often."""

    def deco(fn: Effect) -> Effect:
        (_RAINS if rain else _EFFECTS).append((weight, fn))
        return fn

    return deco


def _choose(pool: list[tuple[float, Effect]]) -> Effect | None:
    usable = [(w, fn) for w, fn in pool
              if _FAILURES.get(fn.__name__, 0) < CONFIG.max_failures]
    if not usable:
        return None
    return random.choices([fn for _, fn in usable],
                          weights=[w for w, _ in usable], k=1)[0]


def _run_effect(activity: Any, st: _FxState, fn: Effect | None) -> None:
    if fn is None:
        return
    try:
        fn(activity, st)
    except Exception:
        _FAILURES[fn.__name__] = _FAILURES.get(fn.__name__, 0) + 1
        _log.exception('Lucious FX: effect %s failed', fn.__name__)


# --- Signature ------------------------------------------------------------


def fx_signature_popup(_act: Any, st: _FxState) -> None:
    pos = _rand_pos()
    color = _pick_color()
    _popup(st, CONFIG.signature, pos, color, scale=random.uniform(2.2, 3.4))
    _explosion(st, pos, color, radius=random.uniform(2.0, 5.0))
    _chunks(pos, 'spark', count=25, scale=1.4, spread=1.0)
    _boom_sound(pos)


@effect(0.5)
def fx_signature_wave(_act: Any, st: _FxState) -> None:
    text = CONFIG.signature
    y, z = random.uniform(8.0, 12.0), random.uniform(-4.0, 4.0)
    step = 22.0 / max(len(text), 1)
    for i, char in enumerate(text):
        if char == ' ':
            continue
        hue = i / max(len(text), 1) * math.tau
        color = (1.0 + math.sin(hue), 1.0 + math.sin(hue + 2.1), 1.0 + math.sin(hue + 4.2))
        _later(st, i * 0.06, _popup, st, char, (-11.0 + i * step, y, z), color, 3.2)


# --- Fireworks & explosions ----------------------------------------------


@effect(3.0)
def fx_firework(_act: Any, st: _FxState) -> None:
    _firework(st, _rand_pos(), _pick_color(), random.uniform(2.0, 5.0))


@effect(1.5)
def fx_double_firework(_act: Any, st: _FxState) -> None:
    pos = _rand_pos()
    _firework(st, pos, _pick_color(), 3.0)
    offset = (pos[0] + random.uniform(-3, 3), pos[1] + 2.0, pos[2] + random.uniform(-3, 3))
    _later(st, 0.2, _firework, st, offset, _pick_color(), 3.0)


@effect(1.0)
def fx_big_boom(_act: Any, st: _FxState) -> None:
    pos = _rand_pos()
    _explosion(st, pos, _pick_color(), radius=6.0, big=True)
    _chunks(pos, 'rock', count=30, scale=1.2, spread=1.5)
    bs.camerashake(intensity=2.0)
    _boom_sound(pos)


@effect(0.6)
def fx_color_chain(_act: Any, st: _FxState) -> None:
    y, z = random.uniform(6.0, 12.0), random.uniform(-5.0, 5.0)
    palette = random.choice(_PALETTES)
    for i in range(7):
        pos = (-12.0 + i * 4.0, y, z)
        _later(st, i * 0.08, _explosion, st, pos, palette[i % len(palette)], 2.0)
    _later(st, 0.3, _boom_sound, (0.0, y, z))


@effect(0.8)
def fx_meteor(_act: Any, st: _FxState) -> None:
    side = random.choice((-1.0, 1.0))
    start = (side * random.uniform(8.0, 14.0), 18.0, random.uniform(-6.0, 6.0))
    end = (start[0] - side * 12.0, 4.0, start[2])
    color = random.choice(_PALETTES[0])
    steps = 10
    for i in range(steps):
        t = i / (steps - 1)
        pos = tuple(a + (b - a) * t for a, b in zip(start, end))
        _later(st, i * 0.04, _meteor_step, st, pos, color, i == steps - 1)


def _meteor_step(st: _FxState, pos: Vec, color: Color, final: bool) -> None:
    if final:
        _explosion(st, pos, color, radius=4.5, big=True)
        _chunks(pos, 'rock', count=25, spread=1.5)
        bs.camerashake(intensity=1.5)
        _boom_sound(pos)
    else:
        _explosion(st, pos, color, radius=1.0)
        _chunks(pos, 'spark', count=8, scale=1.0, spread=0.4)


@effect(0.8)
def fx_lightning(_act: Any, st: _FxState) -> None:
    x = random.uniform(-CONFIG.area_x, CONFIG.area_x)
    z = random.uniform(-CONFIG.area_z, CONFIG.area_z)
    color = (1.6, 1.6, 2.0)
    steps = 7
    for i in range(steps):
        y = 18.0 - 15.0 * i / (steps - 1)
        pos = (x + random.uniform(-0.8, 0.8), y, z + random.uniform(-0.8, 0.8))
        _later(st, i * 0.03, _explosion, st, pos, color, 0.8)
    _later(st, steps * 0.03, _lightning_strike, st, (x, 3.0, z), color)


def _lightning_strike(st: _FxState, pos: Vec, color: Color) -> None:
    _light_pulse(st, pos, color, radius=2.0, peak=3.0, duration=0.4)
    _chunks(pos, 'spark', count=50, scale=1.5, spread=1.5)
    bs.camerashake(intensity=2.0)
    _boom_sound(pos)


# --- Lights ----------------------------------------------------------------


@effect(1.5)
def fx_light_pulse(_act: Any, st: _FxState) -> None:
    _light_pulse(st, _rand_pos(), _pick_color(), radius=random.uniform(0.5, 1.2))


@effect(1.0)
def fx_flash_pop(_act: Any, st: _FxState) -> None:
    pos = _rand_pos()
    _flash(st, pos, _pick_color(), size=random.uniform(1.0, 2.5))
    _light_pulse(st, pos, (2.0, 2.0, 2.0), radius=0.4, peak=2.0, duration=0.25)


@effect(0.7)
def fx_ring_of_lights(_act: Any, st: _FxState) -> None:
    cx, cy, cz = _rand_pos()
    color = _pick_color()
    for i in range(8):
        ang = i / 8 * math.tau
        pos = (cx + math.cos(ang) * 3.5, cy, cz + math.sin(ang) * 3.5)
        _later(st, i * 0.05, _light_pulse, st, pos, color, 0.4, 1.2, 0.6)


@effect(0.5)
def fx_rainbow_orb(_act: Any, st: _FxState) -> None:
    light = bs.newnode('light', attrs={
        'position': _rand_pos(), 'radius': 0.8, 'intensity': 1.5,
        'height_attenuated': False, 'volume_intensity_scale': 10.0,
        'color': (2.0, 0.0, 0.0),
    })
    bs.animate_array(light, 'color', 3, {
        0.0: (2.0, 0.0, 0.0), 0.25: (2.0, 2.0, 0.0), 0.5: (0.0, 2.0, 0.0),
        0.75: (0.0, 1.0, 2.0), 1.0: (2.0, 0.0, 2.0), 1.25: (2.0, 0.0, 0.0),
    }, loop=True)
    _track_node(st, light, 2.5)


@effect(0.5)
def fx_aurora(_act: Any, st: _FxState) -> None:
    y, z = random.uniform(10.0, 14.0), random.uniform(-6.0, 6.0)
    palette = random.choice(_PALETTES)
    for i in range(6):
        pos = (-12.5 + i * 5.0, y, z)
        _later(st, i * 0.12, _light_pulse, st, pos, palette[i % len(palette)], 1.2, 1.0, 1.5)


# --- Particle effects --------------------------------------------------------


@effect(1.5)
def fx_spark_fountain(_act: Any, st: _FxState) -> None:
    pos = _rand_pos((3.0, 6.0))
    for i in range(5):
        _later(st, i * 0.08, _chunks, pos, 'spark', 15, 1.2, 0.5, (0.0, 8.0, 0.0))


@effect(0.7)
def fx_spiral_sparks(_act: Any, st: _FxState) -> None:
    cx, _, cz = _rand_pos()
    for i in range(14):
        ang = i * 0.6
        rad = 1.0 + i * 0.2
        pos = (cx + math.cos(ang) * rad, 4.0 + i * 0.6, cz + math.sin(ang) * rad)
        _later(st, i * 0.04, _chunks, pos, 'spark', 12, 1.0, 0.3)


def _chunk_effect(chunk_type: str, weight: float, count: int = 40) -> None:
    def fx(_act: Any, st: _FxState) -> None:
        del st
        _chunks(_rand_pos(), chunk_type, count=count, scale=1.3, spread=1.4)

    fx.__name__ = f'fx_{chunk_type}_burst'
    effect(weight)(fx)


def _tendril_effect(tendril_type: str, weight: float) -> None:
    def fx(_act: Any, st: _FxState) -> None:
        del st
        _tendrils(_rand_pos(), tendril_type, count=12, spread=1.5)

    fx.__name__ = f'fx_{tendril_type}_tendrils'
    effect(weight)(fx)


for _ctype, _w in (('ice', 1.0), ('metal', 1.0), ('slime', 1.0),
                   ('splinter', 1.0), ('rock', 1.0), ('sweat', 0.8)):
    _chunk_effect(_ctype, _w)
for _ttype, _w in (('smoke', 0.8), ('ice', 0.8), ('thin_smoke', 0.6)):
    _tendril_effect(_ttype, _w)


@effect(0.8)
def fx_sticker_burst(_act: Any, st: _FxState) -> None:
    del st
    bs.emitfx(position=_rand_pos(), emit_type='stickers', count=20, spread=1.5)


@effect(1.0)
def fx_distortion_wave(_act: Any, st: _FxState) -> None:
    del st
    pos = _rand_pos()
    for _ in range(3):
        bs.emitfx(position=pos, emit_type='distortion', spread=3.0)


# --- Screen / audio ----------------------------------------------------------


@effect(1.5)
def fx_hype_word(_act: Any, st: _FxState) -> None:
    _popup(st, random.choice(_HYPE_WORDS), _rand_pos(), _pick_color(), scale=2.6)


@effect(0.6)
def fx_camera_shake(_act: Any, st: _FxState) -> None:
    del st
    bs.camerashake(intensity=random.uniform(0.8, 2.5))


@effect(0.6)
def fx_disco_tint(act: Any, st: _FxState) -> None:
    if not CONFIG.enable_disco_tint:
        return
    gnode = act.globalsnode
    if st.base_tint is None:
        st.base_tint = tuple(gnode.tint)
    base = st.base_tint
    flash = tuple(random.uniform(0.6, 1.5) for _ in range(3))
    bs.animate_array(gnode, 'tint', 3, {0.0: base, 0.15: flash, 0.7: base})


@effect(0.5)
def fx_sound_sting(_act: Any, st: _FxState) -> None:
    del st
    _sound(random.choice(_STINGS), volume=0.6)


# --- Rains (run on their own slower timer) ---------------------------------


def _bomb_rain(st: _FxState, bomb_type: str) -> None:
    if not CONFIG.enable_bombs:
        return
    from bascenev1lib.actor.bomb import Bomb

    st.prune()
    for _ in range(random.randint(3, 6)):
        if len(st.bombs) >= CONFIG.max_bombs:
            return
        vel = (random.uniform(-2, 2), random.uniform(-6, -2), random.uniform(-2, 2))
        bomb = _retain(st.bombs, Bomb(position=_rand_pos((14.0, 18.0)),
                                      velocity=vel, bomb_type=bomb_type))
        _later(st, random.uniform(0.5, 1.3), _detonate, weakref.ref(bomb))


def _detonate(ref: weakref.ref) -> None:
    bomb = ref()
    if _actor_alive(bomb):
        bomb.explode()


@effect(1.5, rain=True)
def rain_bombs(_act: Any, st: _FxState) -> None:
    _bomb_rain(st, 'normal')


@effect(1.0, rain=True)
def rain_ice_bombs(_act: Any, st: _FxState) -> None:
    _bomb_rain(st, 'ice')


@effect(0.8, rain=True)
def rain_sticky_bombs(_act: Any, st: _FxState) -> None:
    _bomb_rain(st, 'sticky')


@effect(0.8, rain=True)
def rain_impact_bombs(_act: Any, st: _FxState) -> None:
    _bomb_rain(st, 'impact')


@effect(0.5, rain=True)
def rain_land_mines(_act: Any, st: _FxState) -> None:
    _bomb_rain(st, 'land_mine')


@effect(1.0, rain=True)
def rain_powerups(_act: Any, st: _FxState) -> None:
    if not CONFIG.enable_powerups:
        return
    from bascenev1lib.actor.powerupbox import PowerupBox

    st.prune()
    for _ in range(random.randint(3, 5)):
        if len(st.powerups) >= CONFIG.max_powerups:
            return
        box = _retain(st.powerups, PowerupBox(
            position=_rand_pos((14.0, 18.0)),
            poweruptype=random.choice(_POWERUP_TYPES),
            expire=False,
        ))
        _kill_later(st, box, 2.5)


@functools.cache
def _bot_types() -> tuple[type, ...]:
    try:
        module = importlib.import_module('bascenev1lib.actor.spazbot')
    except Exception:
        return ()
    found = (getattr(module, name, None) for name in _BOT_CLASS_NAMES)
    return tuple(t for t in found if isinstance(t, type))


def _spawn_bot(st: _FxState, pos: Vec) -> Any:
    types = _bot_types()
    if not CONFIG.enable_bots or not types:
        return None
    st.prune()
    if len(st.bots) >= CONFIG.max_bots:
        return None
    bot = random.choice(types)()
    if bot.node:
        bot.node.is_area_of_interest = False  # don't drag the camera around
        bot.node.handlemessage('flash')
    bot.handlemessage(bs.StandMessage(pos, random.uniform(0.0, 360.0)))
    return _retain(st.bots, bot)


@effect(1.2, rain=True)
def rain_bots(_act: Any, st: _FxState) -> None:
    for _ in range(random.randint(2, 4)):
        bot = _spawn_bot(st, _rand_pos((15.0, 18.0)))
        if bot is None:
            return
        _kill_later(st, bot, 3.0, immediate=True)


@effect(0.8, rain=True)
def rain_ragdoll_bots(_act: Any, st: _FxState) -> None:
    for _ in range(random.randint(2, 3)):
        bot = _spawn_bot(st, _rand_pos((15.0, 18.0)))
        if bot is None:
            return
        if bot.node:
            bot.node.handlemessage('knockout', 1000.0)
        _kill_later(st, bot, 3.0, immediate=True)


@effect(1.2, rain=True)
def rain_shake_bots(_act: Any, st: _FxState) -> None:
    for _ in range(random.randint(2, 3)):
        base = (random.uniform(-10, 10), random.uniform(7, 12), random.uniform(-4, 4))
        bot = _spawn_bot(st, base)
        if bot is None:
            return
        bot.handlemessage(bs.CelebrateMessage(duration=3.0))
        st.shakers.append(_Shaker(weakref.ref(bot), base,
                                  bs.time() + random.uniform(2.0, 3.0)))


def _update_shakers(st: _FxState) -> None:
    now = bs.time()
    keep: list[_Shaker] = []
    for shaker in st.shakers:
        bot = shaker.ref()
        if not _actor_alive(bot):
            continue
        if now >= shaker.ends_at:
            pos = tuple(bot.node.position)
            _explosion(st, pos, _pick_color(), radius=1.5)
            _chunks(pos, 'spark', count=30, scale=1.2, spread=0.8)
            _boom_sound(pos)
            bot.handlemessage(bs.DieMessage(immediate=True))
            continue
        bx, by, bz = shaker.base
        jitter = (bx + random.uniform(-0.35, 0.35),
                  by + random.uniform(-0.15, 0.25),
                  bz + random.uniform(-0.35, 0.35))
        bot.handlemessage(bs.StandMessage(jitter, random.uniform(0.0, 360.0)))
        if random.random() < 0.3:
            bot.on_punch_press()
            bot.on_punch_release()
        keep.append(shaker)
    st.shakers = keep


# ---------------------------------------------------------------------------
# Effect lifecycle
# ---------------------------------------------------------------------------


def _create_banner(st: _FxState) -> None:
    node = bs.newnode('text', attrs={
        'text': CONFIG.signature, 'h_attach': 'center', 'v_attach': 'bottom',
        'h_align': 'center', 'v_align': 'center', 'position': (0, 55),
        'scale': 1.1, 'shadow': 1.0, 'flatness': 1.0, 'maxwidth': 600,
        'color': (1.0, 1.0, 1.0, 1.0),
    })
    bs.animate_array(node, 'color', 4, {
        0.0: (1.0, 0.2, 0.2, 1.0), 0.4: (1.0, 1.0, 0.2, 1.0),
        0.8: (0.2, 1.0, 0.3, 1.0), 1.2: (0.2, 0.8, 1.0, 1.0),
        1.6: (0.9, 0.3, 1.0, 1.0), 2.0: (1.0, 0.2, 0.2, 1.0),
    }, loop=True)
    bs.animate(node, 'scale', {0.0: 1.0, 0.6: 1.18, 1.2: 1.0}, loop=True)
    _track_node(st, node)


def _make_tick(activity: Any, handler: Callable[[Any, _FxState], None]) -> Callable[[], None]:
    ref = weakref.ref(activity)

    def _tick() -> None:
        act = ref()
        if act is None:
            return
        st = _state(act)
        if act.expired or not st.running:
            _stop_effects(act)
            return
        with act.context:
            handler(act, st)

    return _tick


def _ambient_tick(act: Any, st: _FxState) -> None:
    st.prune()
    if random.random() < CONFIG.signature_chance:
        _run_effect(act, st, fx_signature_popup)
    low, high = CONFIG.effects_per_tick
    for _ in range(random.randint(low, high)):
        _run_effect(act, st, _choose(_EFFECTS))


def _rain_tick(act: Any, st: _FxState) -> None:
    _run_effect(act, st, _choose(_RAINS))


def _shake_tick(_act: Any, st: _FxState) -> None:
    if st.shakers:
        try:
            _update_shakers(st)
        except Exception:
            _log.exception('Lucious FX: shaker update failed')


def start_effects(activity: Any) -> None:
    if getattr(activity, 'expired', True):
        return
    st = _state(activity)
    if st.running:
        return
    st.running = True
    with activity.context:
        if CONFIG.show_banner:
            try:
                _create_banner(st)
            except Exception:
                _log.exception('Lucious FX: failed to create banner')
        _run_effect(activity, st, fx_signature_popup)
        st.timers = [
            bs.Timer(CONFIG.tick_interval, _make_tick(activity, _ambient_tick), repeat=True),
            bs.Timer(CONFIG.rain_interval, _make_tick(activity, _rain_tick), repeat=True),
            bs.Timer(0.1, _make_tick(activity, _shake_tick), repeat=True),
        ]


def _stop_effects(activity: Any) -> None:
    st = getattr(activity, _STATE_ATTR, None)
    if st is None:
        return
    st.running = False
    st.timers.clear()


def cleanup_effects(activity: Any) -> None:
    st = getattr(activity, _STATE_ATTR, None)
    setattr(activity, _SCORES_SHOWN_ATTR, False)
    if st is None:
        return
    _stop_effects(activity)
    for node in st.nodes:
        _safe_delete(node)
    for bucket in (st.actors, st.bots, st.bombs, st.powerups):
        for ref in bucket:
            _safe_delete(getattr(ref(), 'node', None))
    setattr(activity, _STATE_ATTR, _FxState())


# ---------------------------------------------------------------------------
# Background suppression (score screens without the default backdrop)
# ---------------------------------------------------------------------------


class _BackgroundSuppressor:
    depth = 0

    def __enter__(self) -> None:
        _BackgroundSuppressor.depth += 1

    def __exit__(self, *exc: Any) -> None:
        _BackgroundSuppressor.depth -= 1


_NO_BACKGROUND = _BackgroundSuppressor()


def _claim(cls: type, key: str) -> bool:
    """Mark cls (its own __dict__ only) as patched; False if already done."""
    attr = f'_lucious_patched_{key}'
    if attr in cls.__dict__:
        return False
    setattr(cls, attr, True)
    return True


def _patch_background_actor() -> None:
    try:
        cls = importlib.import_module('bascenev1lib.actor.background').Background
    except (ImportError, AttributeError):
        return
    if not _claim(cls, 'background_init'):
        return
    original = cls.__init__

    @functools.wraps(original)
    def patched_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if _BackgroundSuppressor.depth == 0:
            original(self, *args, **kwargs)
            return
        bs.Actor.__init__(self)
        self._dying = True
        self.fade_time = 0.0
        session = bs.getsession()
        self._session = weakref.ref(session) if session is not None else (lambda: None)
        self.node = None
        self.logo = None

    cls.__init__ = patched_init


def _wrap(cls: type, name: str, key: str,
          factory: Callable[[Callable[..., Any]], Callable[..., Any]],
          inherited: bool = False) -> None:
    original = getattr(cls, name, None) if inherited else cls.__dict__.get(name)
    if original is None or not _claim(cls, f'{key}_{name}'):
        return
    setattr(cls, name, functools.wraps(original)(factory(original)))


def _no_background(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with _NO_BACKGROUND:
            return original(self, *args, **kwargs)
    return wrapper


def _with_cleanup(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        cleanup_effects(self)
        return original(self, *args, **kwargs)
    return wrapper


def _with_stop(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        _stop_effects(self)
        return original(self, *args, **kwargs)
    return wrapper


def _with_effects(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(self, *args, **kwargs)
        start_effects(self)
        return result
    return wrapper


def _scores_once(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(self, _SCORES_SHOWN_ATTR, False):
            return None
        setattr(self, _SCORES_SHOWN_ATTR, True)
        return original(self, *args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Score-screen discovery & patching
# ---------------------------------------------------------------------------

_SCREEN_CLASS_NAMES = (
    'MultiTeamScoreScreenActivity', 'TeamScoreScreenActivity',
    'TeamVictoryScoreScreenActivity', 'TeamSeriesVictoryScoreScreenActivity',
    'FreeForAllScoreScreenActivity', 'FreeForAllVictoryScoreScreenActivity',
    'DrawScoreScreenActivity', 'CoopScoreScreen',
)
_SCREEN_MODULE_NAMES = (
    'bascenev1lib.activity.multiteamscore', 'bascenev1lib.activity.dualteamscore',
    'bascenev1lib.activity.multiteamvictory', 'bascenev1lib.activity.freeforallscore',
    'bascenev1lib.activity.freeforallscorescreen',
    'bascenev1lib.activity.freeforallvictory', 'bascenev1lib.activity.drawscore',
    'bascenev1lib.activity.coopscore',
)


def _collect_screen_classes() -> list[type]:
    classes: list[type] = []

    def add(cls: Any) -> None:
        if isinstance(cls, type) and cls not in classes:
            classes.append(cls)

    def walk(cls: type) -> None:
        for sub in cls.__subclasses__():
            if sub.__name__ in _SCREEN_CLASS_NAMES:
                add(sub)
            walk(sub)

    base = getattr(bs, 'ScoreScreenActivity', None)
    if base is not None:
        add(base)
        walk(base)
    for module_name in _SCREEN_MODULE_NAMES:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for class_name in _SCREEN_CLASS_NAMES:
            add(getattr(module, class_name, None))
    return classes


def _patch_ffa_victory(classes: list[type]) -> None:
    cls = next((c for c in classes
                if c.__name__ == 'FreeForAllVictoryScoreScreenActivity'), None)
    if cls is None:
        return
    if 'on_begin' not in cls.__dict__ or 'show_player_scores' not in cls.__dict__:
        return

    def skip_scores(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(self, _FFA_SKIP_ATTR, False):
                return None
            return original(self, *args, **kwargs)
        return wrapper

    def flag_begin(original: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            setattr(self, _FFA_SKIP_ATTR, True)
            try:
                return original(self, *args, **kwargs)
            finally:
                setattr(self, _FFA_SKIP_ATTR, False)
        return wrapper

    _wrap(cls, 'show_player_scores', 'ffa', skip_scores)
    _wrap(cls, 'on_begin', 'ffa', flag_begin)


def _patch_coop_score() -> None:
    try:
        cls = importlib.import_module('bascenev1lib.activity.coopscore').CoopScoreScreen
    except Exception:
        return
    _wrap(cls, 'on_transition_in', 'coop_nobg', _no_background, inherited=True)


# ---------------------------------------------------------------------------
# Two-team side-by-side layout
# ---------------------------------------------------------------------------

_TWO_TEAM_NAME_Y = 260
_TWO_TEAM_SCORE_Y = 190
_TWO_TEAM_X = 250


def _two_team_slot(pos_v: float, y: float) -> tuple[float, float]:
    return (-_TWO_TEAM_X, y) if pos_v == 65 else (_TWO_TEAM_X, y)


def _patch_two_team_layout() -> None:
    try:
        module = importlib.import_module('bascenev1lib.activity.dualteamscore')
        from bascenev1 import _classicassets
        from bascenev1lib.actor.zoomtext import ZoomText
    except Exception:
        _log.exception('Lucious FX: two-team layout unavailable')
        return

    cls = getattr(module, 'TeamVictoryScoreScreenActivity', None)
    if cls is None or not _claim(cls, 'two_team_layout'):
        return
    strings = _classicassets.strings.multi_team

    def _zoom(text: str, pos: tuple[float, float], shiftpos: tuple[float, float],
              shiftdelay: float, **kwargs: Any) -> None:
        ZoomText(text, position=pos, shiftposition=shiftpos, shiftdelay=shiftdelay,
                 flash=kwargs.pop('flash', False), trail=kwargs.pop('trail', False),
                 jitter=1.0, **kwargs).autoretain()

    def on_begin(self: Any) -> None:
        bs.set_analytics_screen('Teams Score Screen')
        super(cls, self).on_begin()
        height = 130
        team_count = len(self.teams)
        vval = (height * team_count) / 2 - height / 2
        shift_time = 2.5
        session = self.session
        assert isinstance(session, bs.MultiTeamSession)
        if team_count != 2:
            ZoomText(
                strings.best_of_series(count=session.get_series_length()),
                position=(0, 175), shiftposition=(-250, 175), shiftdelay=2.5,
                flash=False, trail=False, h_align='center', scale=0.25,
                color=(0.5, 0.5, 0.5, 1.0), jitter=3.0,
            ).autoretain()
        for i, team in enumerate(session.sessionteams):
            pos_v = vval - i * height
            bs.timer(i * 0.15 + 0.15, _WeakCall(
                self._show_team_name, pos_v, team, i * 0.2,
                shift_time - (i * 0.15 + 0.15)))
            bs.timer(i * 0.15 + 0.5, self._score_display_sound_small.play)
            scored = team is self._winner
            delay = 0.2
            if scored:
                delay = 1.2
                bs.timer(i * 0.15 + 0.2, _WeakCall(
                    self._show_team_old_score, pos_v, team,
                    shift_time - (i * 0.15 + 0.2)))
                bs.timer(i * 0.15 + 1.5, self._score_display_sound.play)
            bs.timer(i * 0.15 + delay, _WeakCall(
                self._show_team_score, pos_v, team, scored, i * 0.2 + 0.1,
                shift_time - (i * 0.15 + delay)))
        self.show_player_scores()

    def _show_team_name(self: Any, pos_v: float, team: Any,
                        kill_delay: float, shiftdelay: float) -> None:
        del kill_delay
        if len(self.teams) != 2:
            _zoom(strings.team_label(name=team.name), (100, pos_v), (-150, pos_v),
                  shiftdelay, h_align='right', maxwidth=300, color=team.color)
            return
        slot = _two_team_slot(pos_v, _TWO_TEAM_NAME_Y)
        _zoom(team.name, slot, slot, shiftdelay, h_align='center',
              maxwidth=300, scale=0.45, color=team.color)

    def _show_team_old_score(self: Any, pos_v: float, sessionteam: Any,
                             shiftdelay: float) -> None:
        text = str(sessionteam.customdata['score'] - 1)
        if len(self.teams) != 2:
            _zoom(text, (150, pos_v), (-100, pos_v), shiftdelay, maxwidth=100,
                  color=(0.6, 0.6, 0.7), lifespan=1.0, h_align='left')
            return
        slot = _two_team_slot(pos_v, _TWO_TEAM_SCORE_Y)
        _zoom(text, slot, slot, shiftdelay, maxwidth=100, color=(0.6, 0.6, 0.7),
              lifespan=1.0, scale=0.56, h_align='center')

    def _show_team_score(self: Any, pos_v: float, sessionteam: Any, scored: bool,
                         kill_delay: float, shiftdelay: float) -> None:
        del kill_delay
        text = str(sessionteam.customdata['score'])
        color = (1.0, 0.9, 0.5) if scored else (0.6, 0.6, 0.7)
        common = dict(maxwidth=100, color=color, flash=scored, trail=scored,
                      trailcolor=(1, 0.8, 0.0, 0))
        if len(self.teams) != 2:
            _zoom(text, (150, pos_v), (-100, pos_v), shiftdelay,
                  h_align='left', **common)
            return
        slot = _two_team_slot(pos_v, _TWO_TEAM_SCORE_Y)
        _zoom(text, slot, slot, shiftdelay, scale=0.56, h_align='center', **common)

    cls.on_begin = on_begin
    cls._show_team_name = _show_team_name
    cls._show_team_old_score = _show_team_old_score
    cls._show_team_score = _show_team_score


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def _patch_score_screens() -> None:
    _patch_two_team_layout()
    classes = _collect_screen_classes()
    _patch_background_actor()
    _patch_ffa_victory(classes)
    for cls in classes:
        _wrap(cls, 'show_player_scores', 'once', _scores_once)
        for method in ('__init__', 'on_transition_in', 'on_begin'):
            _wrap(cls, method, 'nobg', _no_background)
    _patch_coop_score()
    for cls in classes:
        _wrap(cls, 'on_expire', 'cleanup', _with_cleanup)
        _wrap(cls, 'end', 'stop', _with_stop)
        _wrap(cls, 'on_transition_out', 'stop', _with_stop)
        _wrap(cls, 'on_begin', 'fx', _with_effects)


def enable() -> None:
    """Safe to call multiple times; every patch is idempotent."""
    try:
        _patch_score_screens()
        _log.info('Lucious FX %s enabled (%d effects, %d rains)',
                  __version__, len(_EFFECTS), len(_RAINS))
    except Exception:
        _log.exception('Lucious FX: failed to patch score screens')


# ba_meta export babase.Plugin
class LuciousFxPlugin(babase.Plugin):
    """Auto-enables Lucious FX when the app starts."""

    def on_app_running(self) -> None:
        enable()
