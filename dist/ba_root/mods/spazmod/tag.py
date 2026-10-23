"""Animated role tags for BombSquad API 9 (1.8.x-compatible code).

Tags render above the player's character. Each tag independently rotates through
five animation styles. No chat prefixes, chat commands, or tagcolor configuration
are used in this phase.
"""

# Owner/Editor: Lucifer

from __future__ import annotations

import logging
import random
import time
import weakref

import babase
from typing import Any

import bascenev1 as bs

from tools.account_ids import get_v2_account_id, is_v2_account_id
from playersdata import pdata

try:
    import setting as _settings
except Exception:
    _settings = None

_LOG = logging.getLogger("RoleTags")

try:
    from stats import mystats
except Exception:
    mystats = None
    _LOG.debug("Stats display is unavailable; tag/health/ping displays remain enabled.", exc_info=True)

try:
    from _bascenev1 import get_client_ping as _get_client_ping
except Exception:
    _get_client_ping = None

_ANIMATIONS = (
    "spectrum_cascade",
    "diamond_glint",
    "neon_beat",
    "solar_flare",
    "crystal_twinkle",
)

# BombSquad's tag-font icon escapes used by the source scripts.
_ICON_REPLACEMENTS = {
    r"\d": "\ue048",
    r"\c": "\ue043",
    r"\h": "\ue049",
    r"\s": "\ue046",
    r"\n": "\ue04b",
    r"\t": "\ue01f",
    r"\f": "\ue04f",
}

_BASE_SCALE = 0.0096
_ADVANCE = 0.18
_GAP = 0.018
_MAX_WIDTH = 3.60
# Keep pathological custom tags from creating hundreds of per-character nodes.
_MAX_TAG_CHARS = 32
# Health changes are shown at 3.3 Hz instead of polling five times per second.
_HEALTH_UPDATE_INTERVAL = 0.3
_MAX_LOGGED_V2_IDS = 4096
_LOGGED_V2_IDS: set[str] = set()

# Settings are read on spawn, not each frame. Cache the top-level snapshot briefly
# so several display toggles for the same spawn do not repeatedly parse settings.
_SETTINGS_CACHE_TTL = 1.0
_SETTINGS_CACHE_DATA: dict[str, Any] | None = None
_SETTINGS_CACHE_TIME = 0.0

# Stats are displayed every 2.3s, but reading five independent stats can involve
# repeated storage work. Refresh the snapshot every 4.6s while continuing to rotate
# the visible line at the original cadence.
_STATS_CACHE_TTL = 4.6


def _settings_data() -> dict[str, Any]:
    """Return a briefly cached settings snapshot, falling back to a stale copy."""
    global _SETTINGS_CACHE_DATA, _SETTINGS_CACHE_TIME
    if _settings is None:
        return _SETTINGS_CACHE_DATA or {}
    now = time.monotonic()
    if (
        _SETTINGS_CACHE_DATA is not None
        and now - _SETTINGS_CACHE_TIME < _SETTINGS_CACHE_TTL
    ):
        return _SETTINGS_CACHE_DATA
    try:
        data = _settings.get_settings_data()
        if isinstance(data, dict):
            # Copy to avoid holding a reference that settings code mutates in place.
            _SETTINGS_CACHE_DATA = dict(data)
            _SETTINGS_CACHE_TIME = now
            return _SETTINGS_CACHE_DATA
        _LOG.warning("Settings data is not a dictionary; using defaults.")
    except Exception:
        _LOG.debug("Could not load settings snapshot.", exc_info=True)
    return _SETTINGS_CACHE_DATA or {}


def _setting_enabled(name: str, default: bool = True) -> bool:
    """Read a boolean option from settings, failing open to current behavior."""
    value = _settings_data().get(name, default)
    return value if isinstance(value, bool) else default


def _node_exists(node: Any) -> bool:
    try:
        return node is not None and node.exists()
    except Exception:
        return False


def _safe_delete(node: Any) -> None:
    if _node_exists(node):
        try:
            node.delete()
        except Exception:
            pass


def _normalize_tag(value: str) -> str:
    """Normalize icon escapes and bound tag length to cap per-tag node count."""
    text = str(value)
    for old, new in _ICON_REPLACEMENTS.items():
        text = text.replace(old, new)
    if len(text) > _MAX_TAG_CHARS:
        # One code point is reserved for an ellipsis. This keeps exceptionally
        # long custom tags from spawning an unbounded number of text/position nodes.
        text = text[: _MAX_TAG_CHARS - 1] + "…"
    return text


def _glyph_factor(char: str) -> float:
    if char.isspace():
        return 0.55
    if char in ".,:;!|'iIljtfr":
        return 0.68
    if char in "WwMmQO@#%&G8":
        return 1.20
    if char.isdigit():
        return 0.90
    try:
        if ord(char) > 0xFF:
            return 1.08
    except Exception:
        pass
    return 1.0


def _layout(chars: list[str]) -> tuple[float, float, list[float]]:
    """Return a glyph scale, layout scale, and centered X positions."""
    if not chars:
        return _BASE_SCALE, 1.0, []

    widths = [_ADVANCE * _glyph_factor(char) for char in chars]
    raw_width = sum(widths) + _GAP * max(0, len(chars) - 1)
    factor = min(1.0, _MAX_WIDTH / raw_width) if raw_width > 0 else 1.0
    scaled_widths = [width * factor for width in widths]
    gap = _GAP * factor
    total_width = sum(scaled_widths) + gap * max(0, len(chars) - 1)

    cursor = -0.5 * total_width
    positions: list[float] = []
    for width in scaled_widths:
        positions.append(cursor + width * 0.5)
        cursor += width + gap

    return _BASE_SCALE * factor, factor, positions


def _curve(colors: tuple[tuple[float, float, float], ...],
           duration: float) -> dict[float, tuple[float, float, float]]:
    """Distribute colors over one full loop, closing back to the first color."""
    if not colors:
        return {0.0: (1.0, 1.0, 1.0), duration: (1.0, 1.0, 1.0)}
    result: dict[float, tuple[float, float, float]] = {}
    for index, color in enumerate(colors):
        result[duration * index / float(len(colors))] = color
    result[duration] = colors[0]
    return result


class AnimatedRoleTag:
    """A floating, individually animated role tag attached to a Spaz node."""

    def __init__(self, owner_node: Any, tag_text: str):
        self.owner_node = owner_node
        self.tag_text = _normalize_tag(tag_text)
        self.anchor = None
        self.char_nodes: list[Any] = []
        self.char_position_nodes: list[Any] = []
        self.light = None
        self.stopped = False
        self.current_index: int | None = None
        self.queue: list[int] = []
        self.duration = 2.5
        self.phase = random.random()
        self.direction = random.choice((-1, 1))
        self.char_scale = _BASE_SCALE
        self.layout_scale = 1.0
        self._switch_timer = None
        # Reuse identical animation keyframes between characters during one
        # animation cycle instead of rebuilding the same dictionaries per node.
        self._curve_cache: dict[tuple[Any, ...], dict[float, Any]] = {}
        self._pulse_cache: dict[tuple[Any, ...], dict[float, float]] = {}

        if not _node_exists(owner_node) or not self.tag_text:
            self.stopped = True
            return

        try:
            # Fixed anchor: only character color/scale and the small light animate.
            self.anchor = bs.newnode(
                "math",
                owner=owner_node,
                attrs={"input1": (0.0, 1.65, 0.0), "operation": "add"},
            )
            owner_node.connectattr("torso_position", self.anchor, "input2")
            self._start_next_animation(initial=True)
        except Exception:
            self.destroy()
            _LOG.exception("Could not create animated role tag.")

    def _choose_animation(self) -> int:
        if not self.queue:
            if self.current_index is None:
                self.queue = list(range(len(_ANIMATIONS)))
            else:
                self.queue = [
                    index for index in range(len(_ANIMATIONS))
                    if index != self.current_index
                ]
            random.shuffle(self.queue)
        return self.queue.pop()

    def _choose_duration(self, index: int) -> float:
        # Seconds per loop, matching the API 9 animation time units.
        ranges = (
            (2.2, 3.0),  # Spectrum Cascade
            (2.5, 3.4),  # Diamond Glint
            (1.9, 2.7),  # Neon Beat
            (2.3, 3.2),  # Solar Flare
            (2.8, 3.6),  # Crystal Twinkle
        )
        low, high = ranges[index]
        return random.uniform(low, high)

    def _build_characters(self, initial: bool) -> None:
        chars = list(self.tag_text)
        self.char_scale, self.layout_scale, positions = _layout(chars)

        for char, x in zip(chars, positions):
            char_position = bs.newnode(
                "math",
                owner=self.owner_node,
                attrs={"input1": (x, 0.0, 0.0), "operation": "add"},
            )
            self.anchor.connectattr("output", char_position, "input2")

            char_node = bs.newnode(
                "text",
                owner=self.owner_node,
                attrs={
                    "text": char,
                    "in_world": True,
                    "shadow": 1.25,
                    "flatness": 1.0,
                    "color": (1.0, 1.0, 1.0),
                    "scale": self.char_scale,
                    "h_align": "center",
                },
            )
            char_position.connectattr("output", char_node, "position")
            self.char_position_nodes.append(char_position)
            self.char_nodes.append(char_node)

    def _build_light(self) -> None:
        try:
            self.light = bs.newnode(
                "light",
                owner=self.owner_node,
                attrs={
                    "position": (0.0, 0.0, 0.0),
                    "color": (0.45, 0.70, 1.0),
                    "radius": 0.08,
                    "intensity": 0.10,
                },
            )
            self.anchor.connectattr("output", self.light, "position")
        except Exception:
            self.light = None
            _LOG.debug("Optional role-tag light node is unavailable.", exc_info=True)

    def _visual_characters_are_live(self) -> bool:
        """Check whether this tag can reuse its current character/position nodes."""
        expected = len(self.tag_text)
        if (
            len(self.char_nodes) != expected
            or len(self.char_position_nodes) != expected
            or not self.char_nodes
        ):
            return False
        return all(_node_exists(node) for node in self.char_nodes) and all(
            _node_exists(node) for node in self.char_position_nodes
        )

    def _start_next_animation(self, initial: bool = False) -> None:
        if self.stopped or not _node_exists(self.owner_node):
            self.destroy()
            return

        self.current_index = self._choose_animation()
        self.duration = self._choose_duration(self.current_index)
        self.phase = random.random()
        self.direction = random.choice((-1, 1))

        # Character text and its position nodes do not change between styles.
        # Reusing them avoids creating/deleting 2 nodes per tag character every
        # few seconds, which reduces scene-graph churn on busy servers.
        if not self._visual_characters_are_live():
            self._clear_visual_nodes()
            self._build_characters(initial=initial)
        if self.light is None or not _node_exists(self.light):
            self._build_light()

        # Cached keyframe dictionaries are specific to one animation cycle.
        self._curve_cache.clear()
        self._pulse_cache.clear()
        self._run_animation(self.current_index)

        # One selected style loops exactly twice before changing to the next.
        # Timer objects are cancellable by dropping our reference. Use a weak
        # callback so the timer cannot keep this tag alive after its owner dies.
        self._switch_timer = bs.Timer(
            self.duration * 2.0,
            babase.WeakCallStrict(self._switch_to_next_animation),
        )

    def _animate_color(self, node: Any, colors: tuple[tuple[float, float, float], ...],
                       offset: float = 0.0) -> None:
        key = (colors, self.duration)
        curve = self._curve_cache.get(key)
        if curve is None:
            curve = _curve(colors, self.duration)
            self._curve_cache[key] = curve
        bs.animate_array(
            node,
            "color",
            3,
            curve,
            loop=True,
            offset=offset,
        )

    def _animate_scale_pulse(self, node: Any, times: tuple[float, ...],
                             peaks: tuple[float, ...], offset: float = 0.0) -> None:
        key = (times, peaks, self.char_scale, self.duration)
        values = self._pulse_cache.get(key)
        if values is None:
            values = {0.0: self.char_scale}
            for time_value, peak in zip(times, peaks):
                t = max(0.0, min(self.duration, time_value * self.duration))
                values[t] = self.char_scale * peak
                after = min(self.duration, t + 0.07)
                values[after] = self.char_scale
            values[self.duration] = self.char_scale
            # Sort once per unique pulse curve, not once per character node.
            values = dict(sorted(values.items(), key=lambda item: item[0]))
            self._pulse_cache[key] = values
        bs.animate(node, "scale", values, loop=True, offset=offset)

    def _run_animation(self, index: int) -> None:
        duration = self.duration
        count = max(1, len(self.char_nodes))

        if index == 0:
            # 1. Spectrum Cascade: a rainbow sweeps character by character.
            colors = (
                (1.00, 0.16, 0.18),
                (1.00, 0.46, 0.05),
                (1.00, 0.96, 0.08),
                (0.18, 1.00, 0.26),
                (0.05, 0.92, 1.00),
                (0.14, 0.42, 1.00),
                (0.62, 0.16, 1.00),
                (1.00, 0.18, 0.82),
            )
            for i, node in enumerate(self.char_nodes):
                offset = self.direction * (i / float(count)) * duration
                self._animate_color(node, colors, offset)
                self._animate_scale_pulse(node, (0.48, 0.64), (1.20, 1.05), offset)
            light_colors = colors

        elif index == 1:
            # 2. Diamond Glint: blue/ice tones with a travelling white highlight.
            colors = (
                (0.22, 0.64, 1.00),
                (0.38, 0.82, 1.00),
                (0.66, 0.93, 1.00),
                (0.98, 1.00, 1.00),
                (0.66, 0.93, 1.00),
                (0.38, 0.82, 1.00),
            )
            for i, node in enumerate(self.char_nodes):
                offset = self.direction * (i / float(count)) * duration
                self._animate_color(node, colors, offset)
                self._animate_scale_pulse(node, (0.45, 0.51), (1.18, 1.28), offset)
            light_colors = (
                (0.20, 0.62, 1.00),
                (0.55, 0.90, 1.00),
                (1.00, 1.00, 1.00),
                (0.46, 0.86, 1.00),
            )

        elif index == 2:
            # 3. Neon Beat: rhythmic cyan, violet and magenta double-beats.
            colors = (
                (0.04, 0.98, 1.00),
                (0.32, 0.42, 1.00),
                (0.78, 0.18, 1.00),
                (1.00, 0.18, 0.74),
                (1.00, 0.98, 1.00),
                (0.78, 0.18, 1.00),
                (0.04, 0.98, 1.00),
                (0.32, 0.42, 1.00),
                (0.78, 0.18, 1.00),
                (1.00, 0.18, 0.74),
                (1.00, 0.98, 1.00),
            )
            for i, node in enumerate(self.char_nodes):
                offset = (self.phase * duration + i * duration * 0.70 / count) % duration
                self._animate_color(node, colors, offset)
                self._animate_scale_pulse(node, (0.38, 0.72), (1.18, 1.25), offset)
            light_colors = (
                (0.04, 0.98, 1.00),
                (0.78, 0.18, 1.00),
                (1.00, 0.18, 0.74),
                (1.00, 0.98, 1.00),
                (0.78, 0.18, 1.00),
            )

        elif index == 3:
            # 4. Solar Flare: amber/gold flashes expand from the tag centre.
            colors = (
                (0.72, 0.10, 0.03),
                (1.00, 0.32, 0.03),
                (1.00, 0.78, 0.08),
                (1.00, 1.00, 0.82),
                (1.00, 0.78, 0.08),
                (1.00, 0.32, 0.03),
            )
            center = (count - 1) * 0.5
            max_distance = max(1.0, center)
            for i, node in enumerate(self.char_nodes):
                distance = abs(i - center) / max_distance
                offset = (distance * 0.65 * duration + self.phase * duration) % duration
                self._animate_color(node, colors, offset)
                self._animate_scale_pulse(node, (0.35, 0.72), (1.30, 1.20), offset)
            light_colors = (
                (0.72, 0.10, 0.03),
                (1.00, 0.32, 0.03),
                (1.00, 0.78, 0.08),
                (1.00, 1.00, 0.82),
                (1.00, 0.32, 0.03),
            )

        else:
            # 5. Crystal Twinkle: independently phased icy-violet sparkles.
            colors = (
                (0.40, 0.18, 0.90),
                (0.70, 0.58, 1.00),
                (0.82, 0.92, 1.00),
                (1.00, 1.00, 1.00),
                (0.82, 0.92, 1.00),
                (0.70, 0.58, 1.00),
            )
            for i, node in enumerate(self.char_nodes):
                offset = random.random() * duration + i * 0.037
                self._animate_color(node, colors, offset)
                self._animate_scale_pulse(node, (0.30, 0.58), (1.30, 1.24), offset)
            light_colors = (
                (0.40, 0.18, 0.90),
                (0.70, 0.58, 1.00),
                (0.82, 0.92, 1.00),
                (1.00, 1.00, 1.00),
                (0.70, 0.58, 1.00),
            )

        if self.light is not None and _node_exists(self.light):
            self._animate_color(self.light, light_colors, self.phase * duration)
            # A restrained glow keeps the text legible without a large light pool.
            bs.animate(
                self.light,
                "radius",
                {
                    0.0: 0.065,
                    duration * 0.35: 0.13,
                    duration * 0.48: 0.19,
                    duration * 0.62: 0.08,
                    duration: 0.065,
                },
                loop=True,
                offset=self.phase * duration,
            )
            bs.animate(
                self.light,
                "intensity",
                {
                    0.0: 0.06,
                    duration * 0.35: 0.18,
                    duration * 0.48: 0.35,
                    duration * 0.62: 0.08,
                    duration: 0.06,
                },
                loop=True,
                offset=self.phase * duration,
            )

    def _clear_visual_nodes(self) -> None:
        _safe_delete(self.light)
        self.light = None
        for node in self.char_nodes:
            _safe_delete(node)
        for node in self.char_position_nodes:
            _safe_delete(node)
        self.char_nodes = []
        self.char_position_nodes = []

    def _switch_to_next_animation(self) -> None:
        # The previous one-shot timer has fired; release its handle before
        # creating the next one.
        self._switch_timer = None
        if self.stopped:
            return
        if not _node_exists(self.owner_node):
            self.destroy()
            return
        try:
            self._start_next_animation(initial=False)
        except Exception:
            _LOG.exception("Animated role-tag switch failed; stopping this tag.")
            self.destroy()

    def destroy(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        # bs.Timer stops when no longer referenced; bs.timer() returns None and
        # cannot be cancelled. The callback above is weak, so this also avoids a
        # self -> timer -> bound-method reference cycle.
        self._switch_timer = None
        self._clear_visual_nodes()
        _safe_delete(self.anchor)
        self.anchor = None
        self.owner_node = None



def _client_id_for_player(player: Any) -> int | None:
    """Return a connected player's client ID without assuming one wrapper type."""
    candidates = (player, getattr(player, "sessionplayer", None))
    for candidate in candidates:
        if candidate is None:
            continue
        device = getattr(candidate, "inputdevice", None)
        value = getattr(device, "client_id", None) if device is not None else None
        if isinstance(value, bool):
            continue
        try:
            client_id = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if client_id >= 0:
            return client_id
    return None


def _cancel_display_timer(display: Any) -> None:
    """Release a cancellable Timer handle during explicit display teardown."""
    # bascenev1.Timer is lifetime-managed: dropping the final reference stops it.
    # bs.timer() returns None and cannot be cancelled, so display timers below
    # must store Timer objects instead.
    display._timer = None


class RotatingStatsDisplay:
    """Rotate rank, score, kills, deaths, and K/D above a character."""

    _INTERVAL = 2.3

    def __init__(self, owner_node: Any, account_id: str):
        self.owner_node = owner_node
        self.account_id = account_id
        self.index = 0
        self.text_node = None
        self.position_node = None
        self.stopped = False
        self._timer = None
        self._entries_cache: list[str] | None = None
        self._entries_cache_time = 0.0
        self._last_text: str | None = None
        self.position_node = bs.newnode(
            "math",
            owner=owner_node,
            attrs={"input1": (0.0, 1.20, 0.0), "operation": "add"},
        )
        owner_node.connectattr("torso_position", self.position_node, "input2")
        self.text_node = bs.newnode(
            "text",
            owner=owner_node,
            attrs={
                "text": "",
                "in_world": True,
                "shadow": 1.0,
                "flatness": 1.0,
                "color": (1.0, 1.0, 1.0),
                "scale": 0.008,
                "h_align": "center",
            },
        )
        self.position_node.connectattr("output", self.text_node, "position")
        self._update()

    def _get_entries(self) -> list[str]:
        now = time.monotonic()
        if (
            self._entries_cache is not None
            and now - self._entries_cache_time < _STATS_CACHE_TTL
        ):
            return self._entries_cache
        aid = self.account_id
        if mystats is None:
            entries = ["Stats unavailable"]
        else:
            try:
                kd = float(mystats.get_kd(aid))
                entries = [
                    f"\ue043 Rank: {mystats.get_rank(aid)}",
                    f"\ue047 Score: {mystats.get_score(aid)}",
                    f"\ue047 Kills: {mystats.get_kills(aid)}",
                    f"\ue047 Deaths: {mystats.get_deaths(aid)}",
                    f"\ue047 KD: {kd:g}",
                ]
            except Exception:
                _LOG.debug("Could not read rotating statistics for %s.", aid, exc_info=True)
                entries = ["Stats unavailable"]
        self._entries_cache = entries
        self._entries_cache_time = now
        return entries

    def _update(self) -> None:
        # The scheduled one-shot timer has fired; release its handle before
        # deciding whether another update is needed.
        self._timer = None
        if self.stopped or not _node_exists(self.owner_node) or not _node_exists(self.text_node):
            return
        entries = self._get_entries()
        current_text = entries[self.index % len(entries)]
        if current_text != self._last_text:
            self.text_node.text = current_text
            self._last_text = current_text
        try:
            bs.animate(
                self.text_node,
                "opacity",
                {0.0: 0.0, 0.3: 1.0, 1.8: 1.0, 2.1: 0.0},
            )
        except Exception:
            # Text remains useful even if opacity animation is unavailable.
            self.text_node.opacity = 1.0
        self.index = (self.index + 1) % len(entries)
        self._timer = bs.Timer(
            self._INTERVAL, babase.WeakCallStrict(self._update)
        )

    def destroy(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        _cancel_display_timer(self)
        _safe_delete(self.text_node)
        _safe_delete(self.position_node)
        self.text_node = None
        self.position_node = None


class HealthDisplay:
    """Display live health below the tag and above the rotating statistics."""

    def __init__(self, owner_node: Any, actor: Any):
        self.owner_node = owner_node
        # Do not strongly retain the Spaz actor here. The display is stored on
        # that actor, so a strong reference would create actor -> display -> actor
        # and delay Ballistica's actor-death checks when a round ends.
        try:
            self._actor_ref = weakref.ref(actor)
        except TypeError:
            self._actor_ref = None
        self.stopped = False
        self._timer = None
        self._last_text: str | None = None
        self._last_color: tuple[float, float, float] | None = None
        self.position_node = bs.newnode(
            "math",
            owner=owner_node,
            attrs={"input1": (0.0, 1.42, 0.0), "operation": "add"},
        )
        owner_node.connectattr("torso_position", self.position_node, "input2")
        self.text_node = bs.newnode(
            "text",
            owner=owner_node,
            attrs={
                "text": "",
                "in_world": True,
                "shadow": 1.0,
                "flatness": 1.0,
                "color": (1.0, 1.0, 1.0),
                "scale": 0.009,
                "h_align": "center",
            },
        )
        self.position_node.connectattr("output", self.text_node, "position")
        self._update()

    def _update(self) -> None:
        self._timer = None
        if self.stopped or not _node_exists(self.owner_node) or not _node_exists(self.text_node):
            return
        actor = self._actor_ref() if self._actor_ref is not None else None
        if actor is None:
            self.destroy()
            return
        try:
            health = max(0, int(getattr(actor, "hitpoints")))
        except Exception:
            self.destroy()
            return
        health_value = health / 10.0
        current_text = f"\ue047{health_value:g}\ue047"
        current_color = (1.0, 1.0, 1.0) if health_value >= 20 else (1.0, 0.2, 0.2)
        if current_text != self._last_text:
            self.text_node.text = current_text
            self._last_text = current_text
        if current_color != self._last_color:
            self.text_node.color = current_color
            self._last_color = current_color
        self._timer = bs.Timer(
            _HEALTH_UPDATE_INTERVAL, babase.WeakCallStrict(self._update)
        )

    def destroy(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        _cancel_display_timer(self)
        _safe_delete(self.text_node)
        _safe_delete(self.position_node)
        self.text_node = None
        self.position_node = None
        self._actor_ref = None


class PingDisplay:
    """Display live network ping near the player's lower body."""

    def __init__(self, owner_node: Any, client_id: int | None):
        self.owner_node = owner_node
        self.client_id = client_id
        self.stopped = False
        self._timer = None
        self._last_text: str | None = None
        self._last_color: tuple[float, float, float] | None = None
        self.position_node = bs.newnode(
            "math",
            owner=owner_node,
            attrs={"input1": (0.0, -1.0, 0.0), "operation": "add"},
        )
        owner_node.connectattr("torso_position", self.position_node, "input2")
        self.text_node = bs.newnode(
            "text",
            owner=owner_node,
            attrs={
                "text": "-- ms",
                "in_world": True,
                "shadow": 1.0,
                "flatness": 1.0,
                "color": (0.75, 0.75, 0.75),
                "scale": 0.009,
                "h_align": "center",
            },
        )
        self.position_node.connectattr("output", self.text_node, "position")
        self._update()

    def _update(self) -> None:
        self._timer = None
        if self.stopped or not _node_exists(self.owner_node) or not _node_exists(self.text_node):
            return
        ping = None
        if _get_client_ping is not None and self.client_id is not None:
            try:
                raw_ping = int(_get_client_ping(self.client_id))
                # Negative values mean unavailable; don't misleadingly display 0 ms.
                ping = raw_ping if raw_ping >= 0 else None
            except Exception:
                ping = None
        if ping is None:
            current_text = "-- ms"
            current_color = (0.75, 0.75, 0.75)
        else:
            current_text = f"{ping} ms"
            if ping < 80:
                current_color = (0.0, 1.0, 0.0)
            elif ping < 150:
                current_color = (1.0, 1.0, 0.0)
            else:
                current_color = (1.0, 0.0, 0.0)
        if current_text != self._last_text:
            self.text_node.text = current_text
            self._last_text = current_text
        if current_color != self._last_color:
            self.text_node.color = current_color
            self._last_color = current_color
        self._timer = bs.Timer(
            1.0, babase.WeakCallStrict(self._update)
        )

    def destroy(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        _cancel_display_timer(self)
        _safe_delete(self.text_node)
        _safe_delete(self.position_node)
        self.text_node = None
        self.position_node = None


class PlayerOverheadDisplays:
    """Own the role tag and the extra per-player overhead displays together."""

    def __init__(
        self,
        tag_effect: AnimatedRoleTag | None,
        stats_display: RotatingStatsDisplay | None,
        health_display: HealthDisplay | None,
        ping_display: PingDisplay | None,
    ):
        self.tag_effect = tag_effect
        self.stats_display = stats_display
        self.health_display = health_display
        self.ping_display = ping_display
        self.stopped = False

    def destroy(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for effect in (
            self.tag_effect,
            self.stats_display,
            self.health_display,
            self.ping_display,
        ):
            if effect is not None:
                try:
                    effect.destroy()
                except Exception:
                    _LOG.debug("Could not clean up a player overhead display.", exc_info=True)
        self.tag_effect = None
        self.stats_display = None
        self.health_display = None
        self.ping_display = None


def _resolve_spaz_actor(owner_node: Any, player: Any, supplied_actor: Any = None) -> Any | None:
    """Resolve the live Spaz associated with this player/node."""
    candidates = [supplied_actor]
    try:
        session_player = getattr(player, "sessionplayer", None)
    except Exception:
        session_player = None
    for source in (player, session_player):
        if source is not None:
            try:
                nested_player = getattr(source, "player", None)
                candidates.extend((
                    getattr(source, "actor", None),
                    getattr(nested_player, "actor", None) if nested_player is not None else None,
                ))
            except Exception:
                continue
    for candidate in candidates:
        actor = _resolve_actor(candidate)
        actor_node = getattr(actor, "node", None) if actor is not None else None
        if not _node_exists(actor_node):
            continue
        try:
            if actor_node is owner_node or actor_node == owner_node:
                return actor
        except Exception:
            pass
    # A supplied actor is authoritative even if Node wrappers compare by value
    # rather than object identity; this is the normal spawn-hook path.
    if supplied_actor is not None and _node_exists(getattr(supplied_actor, "node", None)):
        return supplied_actor
    return None


def addtag(
    owner_node: Any,
    player: Any,
    actor: Any = None,
) -> PlayerOverheadDisplays | None:
    """Attach the role/custom tag plus stats, health, and ping displays.

    Role and stats lookups are V2-only. Health and ping do not depend on an
    account ID, so they can still display for players without a valid V2 ID.
    """
    if not _node_exists(owner_node):
        return None

    try:
        account_id = get_v2_account_id(player)
    except Exception:
        account_id = None
    if not is_v2_account_id(account_id):
        account_id = None

    # Log V2 IDs server-side once so the operator can add them to roles.json.
    if account_id and account_id not in _LOGGED_V2_IDS:
        if len(_LOGGED_V2_IDS) >= _MAX_LOGGED_V2_IDS:
            # Bound lifetime memory usage on public servers with many unique joins.
            _LOGGED_V2_IDS.clear()
        _LOGGED_V2_IDS.add(account_id)
        _LOG.info("V2 account ID observed: %s", account_id)

    tag_effect = None
    if account_id and _setting_enabled("enabletags", True):
        try:
            assignment = pdata.get_player_tag(account_id)
        except Exception:
            assignment = None
            _LOG.exception("Could not retrieve player tag for V2 account %s.", account_id)
        if assignment is not None:
            try:
                tag_kind, tag_name, tag_text = assignment
                tag_effect = AnimatedRoleTag(owner_node, tag_text)
                if tag_effect.stopped:
                    tag_effect = None
                elif tag_kind == "custom":
                    _LOG.debug("Attached animated custom tag.")
                else:
                    _LOG.debug("Attached animated role tag for role %s.", tag_name)
            except Exception:
                _LOG.exception("Could not attach animated player tag.")
                if tag_effect is not None:
                    tag_effect.destroy()
                    tag_effect = None
    else:
        _LOG.debug("No valid V2 account ID; skipping tag/stats lookup (no V1 fallback).")

    spaz_actor = _resolve_spaz_actor(owner_node, player, actor)
    stats_display = None
    if (
        _setting_enabled("enablestats", True)
        and account_id
        and mystats is not None
    ):
        try:
            stats_display = RotatingStatsDisplay(owner_node, account_id)
        except Exception:
            _LOG.exception("Could not create rotating stats display.")

    health_display = None
    if _setting_enabled("enablehptag", True) and spaz_actor is not None:
        try:
            health_display = HealthDisplay(owner_node, spaz_actor)
        except Exception:
            _LOG.exception("Could not create health display.")

    ping_display = None
    client_id = _client_id_for_player(player)
    if _setting_enabled("enablePingDisplay", True) and client_id is not None:
        try:
            ping_display = PingDisplay(owner_node, client_id)
        except Exception:
            _LOG.exception("Could not create ping display.")

    return PlayerOverheadDisplays(tag_effect, stats_display, health_display, ping_display)


def _resolve_actor(value: Any) -> Any | None:
    """Resolve actor-like properties/methods safely."""
    actor = value
    if callable(actor) and not hasattr(actor, "node"):
        try:
            actor = actor()
        except Exception:
            return None
    return actor


def _live_actor(actor: Any) -> bool:
    """Return True only for a character with an existing node and live state."""
    actor = _resolve_actor(actor)
    node = getattr(actor, "node", None) if actor is not None else None
    if not _node_exists(node):
        return False
    alive = getattr(actor, "is_alive", None)
    if callable(alive):
        try:
            return bool(alive())
        except Exception:
            return False
    return True


def _find_live_character(client_id: int) -> tuple[Any, Any] | None:
    """Find (actor, SessionPlayer) for a connected client in the live activity."""
    session_players: dict[int, Any] = {}
    try:
        session = bs.get_foreground_host_session()
    except Exception:
        session = None
    if session is not None:
        for session_player in getattr(session, "sessionplayers", ()):
            try:
                candidate_id = int(session_player.inputdevice.client_id)
            except Exception:
                continue
            session_players[candidate_id] = session_player

    # Prefer the Player/actor owned by the active gameplay activity. Its actor
    # is the actual character currently present in the scene.
    activity = None
    try:
        activity = bs.get_foreground_host_activity()
    except Exception:
        try:
            activity = bs.getactivity()
        except Exception:
            activity = None
    if activity is not None:
        for activity_player in getattr(activity, "players", ()):
            session_player = getattr(activity_player, "sessionplayer", None)
            try:
                candidate_id = int(session_player.inputdevice.client_id)
            except Exception:
                continue
            if candidate_id != client_id:
                continue
            actor = _resolve_actor(getattr(activity_player, "actor", None))
            if _live_actor(actor):
                return actor, session_player or session_players.get(client_id)

    # Fallback for API versions/session states where the live actor is exposed
    # only from SessionPlayer. Never use an actor without a valid live node.
    session_player = session_players.get(client_id)
    if session_player is not None:
        candidates = (
            getattr(session_player, "actor", None),
            getattr(getattr(session_player, "player", None), "actor", None),
        )
        for candidate in candidates:
            actor = _resolve_actor(candidate)
            if _live_actor(actor):
                return actor, session_player
    return None


def _activity_for_actor(actor: Any) -> Any | None:
    """Resolve the activity which owns a live character actor."""
    getter = getattr(actor, "_activity", None) if actor is not None else None
    if callable(getter):
        try:
            activity = getter()
            if activity is not None:
                return activity
        except Exception:
            pass
    try:
        return bs.get_foreground_host_activity()
    except Exception:
        try:
            return bs.getactivity()
        except Exception:
            return None


def refresh_client_tag(client_id: int) -> bool:
    """Immediately refresh a live character tag in its owning activity context.

    Saved tag data remains valid when no live character exists; the spawn hook
    will attach it when the player next receives a character.
    """
    try:
        target_id = int(client_id)
        if target_id < 0:
            return False
        resolved = _find_live_character(target_id)
        if resolved is None:
            return False
        actor, session_player = resolved
        activity = _activity_for_actor(actor)
        if activity is None:
            return False

        # bs.newnode/Node.delete require the scene's Activity context. Chat
        # commands may run in a different context, even for a live character.
        with activity.context:
            node = getattr(actor, "node", None)
            if not _node_exists(node) or not _live_actor(actor):
                return False

            old_effect = getattr(actor, "_roles_tags_effect", None)
            if old_effect is not None:
                try:
                    old_effect.destroy()
                except Exception:
                    _LOG.exception("Could not destroy an old tag during live refresh.")
            try:
                actor._roles_tags_effect = None
            except Exception:
                pass

            # The role tag may be absent (for example after /removetag), but
            # health, ping, and V2-backed stats displays should remain available.
            effect = addtag(node, session_player, actor=actor)
            if effect is not None:
                try:
                    actor._roles_tags_effect = effect
                except Exception:
                    effect.destroy()
                    return False
            return True
    except Exception:
        _LOG.exception("Could not refresh tag for client %s.", client_id)
        return False


def refresh_tag_settings_cache() -> None:
    """Force settings to be reread on the next overhead-display creation."""
    global _SETTINGS_CACHE_DATA, _SETTINGS_CACHE_TIME
    _SETTINGS_CACHE_DATA = None
    _SETTINGS_CACHE_TIME = 0.0
