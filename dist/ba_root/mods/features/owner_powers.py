# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Owner-only effects: arrival sequence, nuke, bomb shower, airstrike, smite."""

from __future__ import annotations

import logging
import random
import time
import weakref
from typing import Any

import babase
import bascenev1 as bs

_LOG = logging.getLogger("Lucious.OwnerPowers")

# ----------------------------- tweak these ---------------------------------
ARRIVAL_TEXT = "OWNER HAS ARRIVED IN SERVER"
ARRIVAL_SECONDS = 9.0
ARRIVAL_COOLDOWN = 90.0            # seconds before the same owner can trigger again
ARRIVAL_ACCOUNT_IDS: set[str] = set()   # empty = every owner-role account
SHOWER_SECONDS = 120.0             # 2 minutes
SHOWER_INTERVAL = 0.15             # one bomb every 0.15s (raise it if the server lags)
SHOWER_TYPES = ("normal", "ice", "sticky", "impact")
NUKE_HEIGHT = 30.0
NUKE_RADIUS = 14.0                 # impact bombs scale this down a bit internally
NUKE_SCALE = 4.0
FLASH_TIMES = (0.6, 1.7, 2.2, 3.6, 5.0, 6.5)
# ---------------------------------------------------------------------------

_EXPLOSIONS = ("explosion01", "explosion02", "explosion03", "explosion04", "explosion05")
_DARK_TINT = (0.07, 0.07, 0.13)
_DARK_AMBIENT = (0.25, 0.25, 0.35)
_FLASH_TINT = (2.4, 2.4, 2.8)
_PALETTE = ((1.0, 0.1, 0.1), (1.0, 0.85, 0.1), (1.0, 1.0, 1.0), (0.2, 0.8, 1.0))
_BASE = "lucious_fx_base"
_DEPTH = "lucious_fx_depth"

_SHOWER: dict[str, Any] = {"timer": None, "end": 0.0}
_RUNNING = False
_FIRST_SCAN = True
_SEEN: set[tuple[int, str]] = set()
_LAST_FIRE: dict[str, float] = {}


# ------------------------------- helpers -----------------------------------
def get_activity() -> Any | None:
    try:
        return bs.get_foreground_host_activity()
    except Exception:
        return None


def player_position(player: Any) -> tuple[float, float, float] | None:
    """Return a live player's position, or None."""
    try:
        node = player.actor.node
        if node is None or not node.exists():
            return None
        pos = node.position
        return (float(pos[0]), float(pos[1]), float(pos[2]))
    except Exception:
        return None


def _bounds(activity: Any) -> tuple[float, float, float, float]:
    try:
        xmin, _, zmin, xmax, _, zmax = activity.map.get_def_bound_box("map_bounds")
        return float(xmin), float(xmax), float(zmin), float(zmax)
    except Exception:
        return -10.0, 10.0, -8.0, 8.0


def _map_center(activity: Any) -> tuple[float, float]:
    xmin, xmax, zmin, zmax = _bounds(activity)
    return (xmin + xmax) / 2.0, (zmin + zmax) / 2.0


def _random_xz(activity: Any) -> tuple[float, float]:
    xmin, xmax, zmin, zmax = _bounds(activity)
    cx, cz = (xmin + xmax) / 2.0, (zmin + zmax) / 2.0
    hx = min((xmax - xmin) / 2.0 * 0.7, 14.0)
    hz = min((zmax - zmin) / 2.0 * 0.7, 10.0)
    return cx + random.uniform(-hx, hx), cz + random.uniform(-hz, hz)


def _boom(activity: Any, volume: float = 3.0) -> None:
    try:
        with activity.context:
            sound = bs.getsound(random.choice(_EXPLOSIONS))
            sound.play(volume=volume)
            sound.play(volume=volume)  # layered = louder
    except Exception:
        _LOG.debug("Could not play boom.", exc_info=True)


def _fake_explosion(activity: Any, pos: tuple[float, float, float], radius: float = 4.0) -> None:
    """Visual-only explosion (no damage)."""
    try:
        with activity.context:
            node = bs.newnode(
                "explosion",
                attrs={"position": pos, "velocity": (0, 0, 0), "radius": radius, "big": True},
            )
            bs.timer(1.0, node.delete)
            bs.emitfx(position=pos, velocity=(0, 3, 0), count=40, scale=1.5,
                      spread=2.0, chunk_type="spark")
    except Exception:
        _LOG.debug("Could not spawn fake explosion.", exc_info=True)


def _drop(activity: Any, bomb_type: str, pos: tuple[float, float, float],
          velocity: tuple[float, float, float] | None = None,
          radius: float | None = None, scale: float = 1.0) -> Any:
    from bascenev1lib.actor.bomb import Bomb

    kwargs: dict[str, Any] = {}
    if radius is not None:
        kwargs["blast_radius"] = radius
    vel = velocity or (random.uniform(-2, 2), -1.0, random.uniform(-2, 2))
    with activity.context:
        return Bomb(position=pos, velocity=vel, bomb_type=bomb_type,
                    bomb_scale=scale, **kwargs).autoretain()


# --------------------- tint / darkness (safe stacking) ----------------------
def _fx_begin(activity: Any) -> tuple[Any, tuple[Any, Any]]:
    g = activity.globalsnode
    cd = activity.customdata
    depth = cd.get(_DEPTH, 0)
    if depth == 0:
        cd[_BASE] = (tuple(g.tint), tuple(g.ambient_color))
    cd[_DEPTH] = depth + 1
    return g, cd[_BASE]


def _fx_end(activity: Any, g: Any, base: tuple[Any, Any], delay: float) -> None:
    cd = activity.customdata

    def restore() -> None:
        try:
            cd[_DEPTH] = max(0, cd.get(_DEPTH, 1) - 1)
            if cd[_DEPTH] == 0:
                g.tint, g.ambient_color = base
        except Exception:
            pass

    bs.timer(delay, restore)


def flash(activity: Any, seconds: float = 1.2) -> None:
    try:
        with activity.context:
            g, base = _fx_begin(activity)
            bs.animate_array(g, "tint", 3, {0.0: (4.0, 4.0, 4.0), 0.15: (2.0, 2.0, 2.4),
                                            seconds: base[0]})
            _fx_end(activity, g, base, seconds + 0.1)
    except Exception:
        _LOG.debug("Flash failed.", exc_info=True)


def darken(activity: Any, seconds: float, lightning: bool = False) -> None:
    """Fade the map to near-black for `seconds`, optionally with lightning flashes."""
    try:
        with activity.context:
            g, base = _fx_begin(activity)
            cur_tint, cur_amb = tuple(g.tint), tuple(g.ambient_color)
            frames: dict[float, Any] = {0.0: cur_tint, 0.5: _DARK_TINT}
            if lightning:
                for ft in FLASH_TIMES:
                    if ft + 0.3 >= seconds - 1.0:
                        break
                    frames[ft - 0.02] = _DARK_TINT
                    frames[ft] = _FLASH_TINT
                    frames[ft + 0.07] = _DARK_TINT
                    frames[ft + 0.12] = (1.2, 1.2, 1.6)
                    frames[ft + 0.25] = _DARK_TINT
            frames[seconds - 1.0] = _DARK_TINT
            frames[seconds] = base[0]
            bs.animate_array(g, "tint", 3, dict(sorted(frames.items())))
            bs.animate_array(g, "ambient_color", 3, {
                0.0: cur_amb, 0.5: _DARK_AMBIENT,
                seconds - 1.0: _DARK_AMBIENT, seconds: base[1]})
            _fx_end(activity, g, base, seconds + 0.2)
    except Exception:
        _LOG.exception("Could not darken the map.")


def _thunder_strike(activity: Any) -> None:
    _boom(activity, 3.0)
    x, z = _random_xz(activity)
    _fake_explosion(activity, (x, 1.0, z), radius=5.0)
    try:
        bs.camerashake(intensity=1.5)
    except Exception:
        pass


def thunder(activity: Any, seconds: float) -> None:
    """Schedule loud thunder/bomb booms in sync with the lightning flashes."""
    try:
        with activity.context:
            for ft in FLASH_TIMES:
                if ft + 0.3 >= seconds - 1.0:
                    break
                bs.timer(ft, bs.Call(_thunder_strike, activity))
    except Exception:
        _LOG.exception("Could not schedule thunder.")


# ------------------------------ big banner ----------------------------------
def banner(activity: Any, text: str, subtitle: str | None = None, seconds: float = 7.0) -> None:
    """Large flashing screen text, visible to everyone."""
    try:
        with activity.context:
            node = bs.newnode("text", attrs={
                "text": text, "scale": 0.0, "position": (0, 40), "maxwidth": 1000,
                "h_align": "center", "v_align": "center",
                "h_attach": "center", "v_attach": "center",
                "color": _PALETTE[0], "shadow": 1.0, "flatness": 0.4,
            })
            bs.animate(node, "scale", {0.0: 0.0, 0.25: 2.3, 0.4: 1.8,
                                       seconds - 0.5: 1.8, seconds: 0.0})
            sub = None
            if subtitle:
                sub = bs.newnode("text", attrs={
                    "text": subtitle, "scale": 1.2, "position": (0, -40), "maxwidth": 800,
                    "h_align": "center", "v_align": "center",
                    "h_attach": "center", "v_attach": "center",
                    "color": (1.0, 0.9, 0.3), "shadow": 1.0, "flatness": 0.5,
                })
            state: dict[str, Any] = {"i": 0, "timer": None}

            def flicker() -> None:
                if node.exists():
                    node.color = _PALETTE[state["i"] % len(_PALETTE)]
                    state["i"] += 1

            def cleanup() -> None:
                state["timer"] = None
                for n in (node, sub):
                    if n is not None and n.exists():
                        n.delete()

            state["timer"] = bs.Timer(0.12, flicker, repeat=True)
            bs.timer(seconds + 0.1, cleanup)
    except Exception:
        _LOG.exception("Could not show banner.")


# ------------------------------- arrival ------------------------------------
def arrival(name: str | None = None) -> bool:
    activity = get_activity()
    if activity is None:
        return False
    try:
        bs.broadcastmessage(ARRIVAL_TEXT, color=(1.0, 0.1, 0.1), top=True)
    except Exception:
        _LOG.debug("Arrival broadcast failed.", exc_info=True)
    darken(activity, ARRIVAL_SECONDS, lightning=True)
    thunder(activity, ARRIVAL_SECONDS)
    banner(activity, ARRIVAL_TEXT, name, 7.0)
    return True


def _scan() -> None:
    global _FIRST_SCAN
    try:
        from playersdata import pdata
        from tools.account_ids import is_v2_account_id

        now = time.monotonic()
        current: set[tuple[int, str]] = set()
        for entry in bs.get_game_roster():
            if not isinstance(entry, dict):
                continue
            try:
                client_id = int(entry.get("client_id"))
            except (TypeError, ValueError):
                continue
            account_id = entry.get("account_id")
            if client_id < 0 or not is_v2_account_id(account_id):
                continue
            key = (client_id, account_id)
            current.add(key)
            if key in _SEEN or _FIRST_SCAN:
                continue
            if ARRIVAL_ACCOUNT_IDS and account_id not in ARRIVAL_ACCOUNT_IDS:
                continue
            if not pdata.is_owner_account_id(account_id):
                continue
            if now - _LAST_FIRE.get(account_id, -1e9) < ARRIVAL_COOLDOWN:
                continue
            _LAST_FIRE[account_id] = now
            name = str(entry.get("display_string") or "OWNER")[:32]
            # Small delay so the owner is actually in the game when it plays.
            babase.apptimer(1.5, lambda n=name: arrival(n))
        _SEEN.clear()
        _SEEN.update(current)
        _FIRST_SCAN = False
    except Exception:
        _LOG.exception("Owner-arrival scan failed.")
    finally:
        if _RUNNING:
            babase.apptimer(1.0, _scan)


def install() -> bool:
    global _RUNNING
    if _RUNNING:
        return True
    _RUNNING = True
    _scan()
    _LOG.info("Owner arrival monitor started.")
    return True


# -------------------------------- nuke --------------------------------------
def _force_explode(bomb: Any) -> None:
    try:
        from bascenev1lib.actor.bomb import ExplodeMessage
        if bomb.node and bomb.node.exists():
            bomb.handlemessage(ExplodeMessage())
    except Exception:
        pass


def nuke(activity: Any, centers: list[tuple[float, float] | None]) -> None:
    """Drop one massive impact bomb per center (None = map center)."""
    try:
        bs.broadcastmessage("NUKE INCOMING!", color=(1.0, 0.1, 0.1), top=True, transient=True)
    except Exception:
        pass
    with activity.context:
        for i in range(3):
            bs.timer(0.4 * i, lambda: bs.getsound("warnBeep").play(volume=2.0))
    for center in centers:
        x, z = center if center is not None else _map_center(activity)
        pos = (x, NUKE_HEIGHT, z)
        bomb = _drop(activity, "impact", pos, velocity=(0.0, -6.0, 0.0),
                     radius=NUKE_RADIUS, scale=NUKE_SCALE)
        state: dict[str, Any] = {"timer": None}

        def check(b: Any = bomb, s: dict[str, Any] = state, p: tuple = pos) -> None:
            try:
                alive = b.node is not None and b.node.exists()
            except Exception:
                alive = False
            if alive:
                return
            s["timer"] = None
            flash(activity, 1.6)
            _boom(activity, 4.0)
            _fake_explosion(activity, (p[0], 1.0, p[2]), radius=8.0)
            try:
                bs.camerashake(intensity=5.0)
            except Exception:
                pass

        with activity.context:
            state["timer"] = bs.Timer(0.05, check, repeat=True)
            bs.timer(8.0, lambda b=bomb: _force_explode(b))  # fallback


# ----------------------------- bomb shower ----------------------------------
def shower_active() -> bool:
    return _SHOWER["timer"] is not None and time.monotonic() < _SHOWER["end"]


def stop_shower() -> None:
    _SHOWER["timer"] = None
    _SHOWER["end"] = 0.0


def start_shower(activity: Any, seconds: float = SHOWER_SECONDS) -> None:
    """Rain normal/ice/sticky/impact bombs over the whole map."""
    stop_shower()
    _SHOWER["end"] = time.monotonic() + seconds
    ref = weakref.ref(activity)

    def tick() -> None:
        act = ref()
        if act is None or time.monotonic() >= _SHOWER["end"]:
            stop_shower()
            return
        x, z = _random_xz(act)
        try:
            _drop(act, random.choice(SHOWER_TYPES), (x, random.uniform(14.0, 20.0), z))
        except Exception:
            _LOG.debug("Shower drop failed.", exc_info=True)

    with activity.context:
        _SHOWER["timer"] = bs.Timer(SHOWER_INTERVAL, tick, repeat=True)
    try:
        bs.broadcastmessage("BOMB SHOWER!", color=(1.0, 0.5, 0.1), top=True, transient=True)
    except Exception:
        pass


# --------------------------- airstrike / smite ------------------------------
def airstrike(activity: Any, players: list[Any], count: int = 10) -> int:
    hit = 0
    for player in players:
        pos = player_position(player)
        if pos is None:
            continue
        hit += 1
        for i in range(count):
            def drop(p: tuple = pos) -> None:
                _drop(activity, random.choice(SHOWER_TYPES),
                      (p[0] + random.uniform(-3, 3), p[1] + random.uniform(10, 14),
                       p[2] + random.uniform(-3, 3)))
            with activity.context:
                bs.timer(0.12 * i, drop)
    return hit


def smite(activity: Any, players: list[Any]) -> int:
    from bascenev1lib.actor.bomb import Blast

    hit = 0
    for player in players:
        pos = player_position(player)
        if pos is None:
            continue
        hit += 1
        flash(activity, 0.8)
        _boom(activity, 3.0)
        _fake_explosion(activity, pos, radius=4.0)
        try:
            with activity.context:
                Blast(position=pos, blast_radius=3.0, blast_type="normal").autoretain()
                player.actor.handlemessage(bs.DieMessage())
        except Exception:
            _LOG.debug("Smite blast failed.", exc_info=True)
    return hit
