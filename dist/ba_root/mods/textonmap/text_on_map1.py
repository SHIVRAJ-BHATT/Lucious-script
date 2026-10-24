# Released under the MIT License. See LICENSE for details.
"""LUCIOUS HUD - in-game text overlays for BombSquad API 9 (1.8.x servers).

Screen formation
    top-center ...... big animated LUCIOUS brand + glow bar + tagline
    top-right ....... TOP 5 leaderboard (smooth row transitions)
    bottom-left ..... owner / editor card
    bottom-center ... thank-you line
    bottom-right .... info stack: next map, feature card, online, season, restart
    center .......... optional rotating highlights

All motion uses native scene-node curves (bs.animate / bs.animate_array);
only three slow timers exist (features, online counter, leaderboard).
The HUD lives on the activity itself, so it dies with the activity.
"""

from __future__ import annotations

import functools
import heapq
import logging
import random
import weakref
from typing import Any

import _babase
import bascenev1 as bs
import setting

_LOG = logging.getLogger("LuciousHUD")

try:
    from stats import mystats
except Exception:
    mystats = None
    _LOG.warning("Stats module unavailable; leaderboard skipped.", exc_info=True)

# =============================================================================
# EASY CONFIG - text, colours, positions. \ue0xx = BombSquad icon glyphs.
# =============================================================================
BRAND: dict[str, Any] = {
    "text": "LUCIOUS",
    "ornament": "\ue043",                       # "" hides side icons
    "tagline": "\ue047 COMMUNITY SERVER \ue048",  # "" hides tagline
    "y": -38,          # move down (e.g. -48) if it touches a game clock
    "scale": 1.45,
    "spacing": 34,
    "wave": 6,
    "colors": [
        (1.00, 0.40, 0.75),
        (1.00, 0.85, 0.35),
        (0.45, 0.95, 1.00),
        (0.80, 0.55, 1.00),
    ],
    "color_step": 1.1,
    "bar_color": (1.00, 0.45, 0.75),
}

HUD_TEXT: dict[str, Any] = {
    "feature_messages": [
        ("\ue043 TOP 2  |  ADMIN \ue043", (1.00, 0.82, 0.28)),
        ("\ue043 TOP 5  |  VIP \ue043", (0.66, 0.88, 1.00)),
        ("\ue043 TOP 10  |  EFFECTS + TAG \ue043", (0.82, 0.70, 1.00)),
        ("\ue043 TOP 20  |  TAG \ue043", (0.62, 1.00, 0.82)),
        ("\ue047 JOIN DISCORD \ue048", (0.55, 0.90, 1.00)),
        ("\ue04c OWNER : LUCIOUS AND ROBIN \ue04d", (0.40, 1.00, 1.00)),
    ],
    "card_line": "______________________________",
    "card_owner": "\ue043 OWNER: LUCIOUS \ue043",
    "card_editor": "\ue043 EDITOR: LUCIFER \ue043",
    "bottom_thanks": "\ue043 THANK YOU FOR PLAYING IN OUR SERVER \ue043",
    "next_game": "\ue047 NEXT \ue048 : {map}",
    "online": "\ue047 ONLINE : {count}{max}",
    "season_reset": "\ue04c SEASON ENDS IN: {days} DAYS \ue04d",
    "restart_message": "\ue04c SERVER RESTARTS AFTER THIS SERIES \ue04d",
    "center_highlights": [],
    "use_setting_json_center_highlights": False,
}

THEME: dict[str, Any] = {
    "card_line_colors": [(0.20, 1.00, 0.45), (0.30, 0.85, 1.00)],
    "card_owner_colors": [(1.0, 0.35, 0.65), (1.0, 0.84, 0.0), (1.0, 1.0, 1.0)],
    "card_editor_colors": [(1.0, 0.84, 0.0), (1.0, 1.0, 0.55), (1.0, 0.70, 0.0)],
    "card_accent": (1.0, 0.45, 0.75),
    "thanks_colors": [(0.85, 0.85, 0.85), (1.0, 0.62, 0.80)],
    "next_color": (1.0, 1.0, 1.0),
    "online_color": (0.55, 1.0, 0.65),
    "season_color": (1.0, 0.90, 0.55),
    "restart_color": (1.0, 0.50, 0.70),
}

LAYOUT: dict[str, Any] = {
    "card_x": 25, "card_y": 67,
    "thanks_y": 20,
    "stack_x": -25, "stack_y": 16, "stack_step": 24,
    "highlight_y": 138,
    "feature_seconds": 4.5,
    "online_seconds": 5.0,
    "show_online": True,
}

LEADERBOARD: dict[str, Any] = {
    "rows": 5,
    "title": "\ue04c TOP 5 PLAYERS \ue04d",
    "panel_top": -96,
    "right_margin": 10,
    "width": 300,
    "row_height": 22,
    "name_chars": 12,
    "panel_color": (0.05, 0.05, 0.07),
    "panel_opacity": 0.78,
    "header_color": (0.28, 0.19, 0.04),
    "accent": (1.00, 0.76, 0.18),
    "title_colors": [(1.0, 0.72, 0.12), (1.0, 0.94, 0.52), (1.0, 0.56, 0.18)],
    "column_color": (0.72, 0.74, 0.80),
    "rank_colors": [
        (1.00, 0.80, 0.15),  # gold
        (0.80, 0.88, 1.00),  # silver
        (1.00, 0.55, 0.30),  # bronze
        (0.45, 0.95, 0.85),  # teal
        (0.80, 0.62, 1.00),  # violet
    ],
    "k_color": (0.45, 1.00, 0.45),
    "d_color": (1.00, 0.42, 0.42),
    "kd_color": (1.00, 1.00, 1.00),
}

_FIELDS = ("rank", "name", "k", "d", "kd", "pts")
_HUD_ATTR = "_lucious_hud"


# =============================================================================
# Helpers
# =============================================================================
def _get_settings_snapshot() -> dict:
    try:
        data = setting.get_settings_data()
        return data if isinstance(data, dict) else {}
    except Exception:
        _LOG.warning("Could not read HUD settings.", exc_info=True)
        return {}


def _section(cfg: dict, name: str) -> dict:
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}


def _tom_enabled(cfg: dict) -> bool:
    return _section(cfg, "textonmap").get("enable") is True


def _text_on_map_enabled() -> bool:
    return _tom_enabled(_get_settings_snapshot())


setti = _get_settings_snapshot()  # legacy name kept for local add-ons


def _alive(node) -> bool:
    try:
        return node is not None and node.exists()
    except Exception:
        return False


def _safe_delete(node) -> None:
    if _alive(node):
        node.delete()


def _color_keys(colors, step: float) -> dict:
    seq = list(colors) + [colors[0]]
    return {i * step: tuple(c) for i, c in enumerate(seq)}


def _text(text, pos, *, scale=0.6, color=(1.0, 1.0, 1.0), h_align="center",
          h_attach="center", v_attach="bottom", opacity=1.0, maxwidth=None,
          shadow=0.6):
    attrs = {
        "text": str(text), "position": pos, "scale": scale, "color": color,
        "h_align": h_align, "v_align": "center", "h_attach": h_attach,
        "v_attach": v_attach, "opacity": opacity, "shadow": shadow,
        "flatness": 1.0,
    }
    if maxwidth:
        attrs["maxwidth"] = maxwidth
    return bs.newnode("text", attrs=attrs)


def _int(value) -> int:
    try:
        return 0 if isinstance(value, bool) else max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1000:.1f}k"
    return str(value)


# =============================================================================
# HUD
# =============================================================================
class textonmap:
    """Builds the LUCIOUS HUD inside the current activity context."""

    def __init__(self, settings: dict | None = None):
        self._settings = settings if isinstance(settings, dict) \
            else _get_settings_snapshot()
        self._tom = _section(self._settings, "textonmap")
        self._disabled = self._tom.get("enable") is not True
        self._timers: list = []
        self._lb_rows: list[dict] = []
        self._feature = None
        self._feature_pos = (0.0, 0.0)
        self._feature_index = 0
        self._online = None
        self._online_text = ""
        self._highlights: list[str] = []
        self._hl_settings: dict = {}
        self._highlight_index = 0
        if self._disabled:
            return
        self._tex = bs.gettexture("white")
        for name, build in (
            ("brand", self._build_brand),
            ("owner card", self._build_owner_card),
            ("thanks", self._build_thanks),
            ("info stack", self._build_info_stack),
            ("leaderboard", self._maybe_build_leaderboard),
            ("highlights", self._setup_highlights),
        ):
            try:
                build()
            except Exception:
                _LOG.exception("LuciousHUD: could not build %s.", name)

    # ---------------------------------------------------------------- utils
    def _repeat(self, seconds: float, method) -> None:
        self._timers.append(bs.Timer(seconds, bs.WeakCall(method), repeat=True))

    def _image(self, pos, size, color, opacity, attach):
        return bs.newnode("image", attrs={
            "texture": self._tex, "color": color, "opacity": opacity,
            "attach": attach, "absolute_scale": True,
            "scale": size, "position": pos,
        })

    # ---------------------------------------------------------------- brand
    def _build_brand(self) -> None:
        c = BRAND
        word, sp, sc, y = c["text"], c["spacing"], c["scale"], c["y"]
        start = -((len(word) - 1) * sp) / 2
        keys = _color_keys(c["colors"], c["color_step"])
        wave = c["wave"]

        for i, ch in enumerate(word):
            if ch.isspace():
                continue
            x = start + i * sp
            d = i * 0.09
            node = _text(ch, (x, y), scale=0.0, color=c["colors"][0],
                         v_attach="top", shadow=1.0)
            bs.animate(node, "scale", {0.0: 0.0, d: 0.0,
                                       d + 0.25: sc * 1.25, d + 0.4: sc})
            bs.animate_array(node, "color", 3, keys, loop=True, offset=d)
            bs.animate_array(node, "position", 2, {
                0.0: (x, y), 0.45: (x, y + wave), 0.9: (x, y), 2.4: (x, y),
            }, loop=True, offset=d * 1.5)

        if c["ornament"]:
            edge = start - sp * 1.1
            for x in (edge, -edge):
                orn = _text(c["ornament"], (x, y), scale=sc * 0.6,
                            color=c["bar_color"], v_attach="top")
                bs.animate(orn, "opacity", {0.0: 0.35, 1.2: 1.0, 2.4: 0.35},
                           loop=True)

        bar = self._image((0, y - 26), (len(word) * sp * 0.9, 3),
                          c["bar_color"], 0.0, "topCenter")
        bs.animate(bar, "opacity", {0.0: 0.25, 1.2: 0.85, 2.4: 0.25}, loop=True)

        if c["tagline"]:
            _text(c["tagline"], (0, y - 42), scale=0.5,
                  color=(0.85, 0.85, 0.95), v_attach="top", shadow=0.8)

    # ----------------------------------------------------------- owner card
    def _build_owner_card(self) -> None:
        x, y = LAYOUT["card_x"], LAYOUT["card_y"]
        bar = self._image((x - 8, y + 10), (3, 56), THEME["card_accent"],
                          0.9, "bottomLeft")
        bs.animate(bar, "opacity", {0.0: 0.4, 1.5: 1.0, 3.0: 0.4}, loop=True)

        line_keys = _color_keys(THEME["card_line_colors"], 2.0)
        for ly in (y + 35, y - 15):
            n = _text(HUD_TEXT["card_line"], (x, ly), scale=0.5,
                      h_align="left", h_attach="left")
            bs.animate_array(n, "color", 3, line_keys, loop=True)

        for label, ly, colors in (
            (HUD_TEXT["card_owner"], y + 15, THEME["card_owner_colors"]),
            (HUD_TEXT["card_editor"], y - 5, THEME["card_editor_colors"]),
        ):
            n = _text(label, (x + 5, ly), scale=0.6, h_align="left",
                      h_attach="left", color=colors[0])
            bs.animate_array(n, "color", 3, _color_keys(colors, 1.2), loop=True)

    # --------------------------------------------------------------- thanks
    def _build_thanks(self) -> None:
        n = _text(HUD_TEXT["bottom_thanks"], (0, LAYOUT["thanks_y"]),
                  scale=0.6, color=THEME["thanks_colors"][0])
        bs.animate_array(n, "color", 3,
                         _color_keys(THEME["thanks_colors"], 3.0), loop=True)

    # ----------------------------------------------------------- info stack
    def _build_info_stack(self) -> None:
        x, y, step = LAYOUT["stack_x"], LAYOUT["stack_y"], LAYOUT["stack_step"]

        def row(text, color, scale=0.7):
            return _text(text, (x, y), scale=scale, color=color,
                         h_align="right", h_attach="right")

        row(HUD_TEXT["next_game"].format(map=self._next_map()),
            THEME["next_color"])
        y += step

        if HUD_TEXT["feature_messages"]:
            self._feature_pos = (x, y)
            self._feature = row("", (1, 1, 1), 0.65)
            self._feature.opacity = 0.0
            self._show_next_feature()
            self._repeat(LAYOUT["feature_seconds"], self._cycle_feature)
            y += step

        if LAYOUT["show_online"]:
            self._online = row("", THEME["online_color"], 0.6)
            self._update_online()
            self._repeat(LAYOUT["online_seconds"], self._update_online)
            y += step

        days = getattr(_babase, "season_ends_in_days", None)
        if isinstance(days, int) and not isinstance(days, bool) and days < 99:
            row(HUD_TEXT["season_reset"].format(days=days),
                THEME["season_color"], 0.65)
            y += step

        if hasattr(_babase, "restart_scheduled"):
            node = row(HUD_TEXT["restart_message"], THEME["restart_color"], 0.5)
            bs.animate(node, "opacity", {0.0: 0.5, 0.8: 1.0, 1.6: 0.5}, loop=True)
            activity = bs.get_foreground_host_activity()
            if activity is not None:
                activity.restart_msg = node  # other mods may reference this

    @staticmethod
    def _next_map() -> str:
        try:
            session = bs.get_foreground_host_session()
            desc = session.get_next_game_description() if session else None
            return desc.evaluate() if desc is not None else "-"
        except Exception:
            return "-"

    def _cycle_feature(self) -> None:
        if _alive(self._feature):
            bs.animate(self._feature, "opacity", {0.0: 1.0, 0.3: 0.0})
            bs.timer(0.3, bs.WeakCall(self._show_next_feature))

    def _show_next_feature(self) -> None:
        node = self._feature
        msgs = HUD_TEXT["feature_messages"]
        if not _alive(node) or not msgs:
            return
        msg, color = msgs[self._feature_index % len(msgs)]
        self._feature_index += 1
        node.text, node.color = msg, color
        x, y = self._feature_pos
        bs.animate_array(node, "position", 2, {0.0: (x + 30, y), 0.35: (x, y)})
        bs.animate(node, "opacity", {0.0: 0.0, 0.35: 1.0})

    def _update_online(self) -> None:
        node = self._online
        if not _alive(node):
            return
        try:
            session = bs.get_foreground_host_session()
            count = len(session.sessionplayers) if session else 0
            max_size = bs.get_public_party_max_size()
            suffix = f"/{max_size}" if max_size else ""
        except Exception:
            count, suffix = 0, ""
        text = HUD_TEXT["online"].format(count=count, max=suffix)
        if text != self._online_text:
            self._online_text = text
            node.text = text

    # ---------------------------------------------------------- leaderboard
    def _maybe_build_leaderboard(self) -> None:
        lb = _section(self._settings, "leaderboard")
        if lb.get("enable") is True and mystats is not None:
            self._build_leaderboard(lb)

    def _build_leaderboard(self, lb_settings: dict) -> None:
        c = LEADERBOARD
        w, rh, n = c["width"], c["row_height"], c["rows"]
        right = -c["right_margin"]
        left = right - w
        cx = (left + right) / 2
        top = c["panel_top"]
        header_h = 26
        cols_y = top - header_h - 12
        first_y = cols_y - 22
        bottom = first_y - (n - 1) * rh - 14
        height = top - bottom

        panel = self._image((cx, top - height / 2), (w, height),
                            c["panel_color"], 0.0, "topRight")
        bs.animate(panel, "opacity", {0.0: 0.0, 0.4: c["panel_opacity"]})
        self._image((cx, top - header_h / 2), (w, header_h),
                    c["header_color"], 0.95, "topRight")
        self._image((cx, top - header_h), (w, 2), c["accent"], 0.9, "topRight")

        title = _text(c["title"], (cx, top - header_h / 2), scale=0.72,
                      color=c["accent"], h_attach="right", v_attach="top",
                      shadow=1.0)
        bs.animate_array(title, "color", 3,
                         _color_keys(c["title_colors"], 1.2), loop=True)

        col = {"rank": left + 14, "name": left + 28, "k": left + 158,
               "d": left + 196, "kd": left + 236, "pts": left + 280}
        for key, label in (("rank", "#"), ("name", "PLAYER"), ("k", "K"),
                           ("d", "D"), ("kd", "K/D"), ("pts", "PTS")):
            _text(label, (col[key], cols_y), scale=0.48,
                  color=c["column_color"],
                  h_align="left" if key == "name" else "center",
                  h_attach="right", v_attach="top")

        for i in range(0, n, 2):
            self._image((cx, first_y - i * rh), (w - 8, rh - 2), (1, 1, 1),
                        0.04, "topRight")
        glow = self._image((cx, first_y), (w - 8, rh - 2),
                           c["rank_colors"][0], 0.1, "topRight")
        bs.animate(glow, "opacity", {0.0: 0.05, 1.5: 0.2, 3.0: 0.05}, loop=True)

        def cell(key, y, color, scale=0.58, align="center", maxwidth=None):
            return _text("", (col[key], y), scale=scale, color=color,
                         h_align=align, h_attach="right", v_attach="top",
                         opacity=0.0, maxwidth=maxwidth)

        self._lb_rows = []
        for i in range(n):
            y = first_y - i * rh
            rc = c["rank_colors"][i % len(c["rank_colors"])]
            row = {
                "rank": cell("rank", y, rc, 0.66),
                "name": cell("name", y, rc, align="left", maxwidth=118),
                "k": cell("k", y, c["k_color"]),
                "d": cell("d", y, c["d_color"]),
                "kd": cell("kd", y, c["kd_color"]),
                "pts": cell("pts", y, rc),
                "sig": None,
            }
            delay = 0.3 + i * 0.08
            keys = {0.0: 0.0, delay: 0.0, delay + 0.3: 1.0}
            for key in _FIELDS:
                bs.animate(row[key], "opacity", keys)
            self._lb_rows.append(row)

        self._refresh_board(initial=True)
        interval = lb_settings.get("refreshSeconds", 10)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            interval = 10
        self._repeat(max(2.0, min(float(interval), 3600.0)), self._refresh_board)

    @staticmethod
    def _top_entries(limit: int) -> list[dict]:
        stats = getattr(mystats, "cached_stats", None) or {}
        if not isinstance(stats, dict):
            return []
        ranks = getattr(mystats, "ranks", None)
        if ranks:
            out = []
            for account_id in ranks:
                entry = stats.get(account_id)
                if isinstance(entry, dict):
                    out.append(entry)
                    if len(out) >= limit:
                        break
            return out
        # O(n log k) instead of sorting every player each refresh.
        valid = (e for e in stats.values() if isinstance(e, dict))
        return heapq.nlargest(limit, valid, key=lambda e: _int(e.get("scores")))

    @staticmethod
    def _kd(entry: dict) -> str:
        value = entry.get("kd")
        if isinstance(value, (int, float)) and not isinstance(value, bool) \
                and 0 <= value < 1e9:
            return f"{float(value):.2f}"
        kills, deaths = _int(entry.get("kills")), _int(entry.get("deaths"))
        return f"{(kills / deaths if deaths else float(kills)):.2f}"

    def _row_values(self, index: int, entry: dict | None) -> dict:
        if entry is None:
            return {"rank": str(index + 1), "name": "-", "k": "", "d": "",
                    "kd": "", "pts": ""}
        name = " ".join(str(entry.get("name") or "Unknown").split()) or "Unknown"
        limit = LEADERBOARD["name_chars"]
        if len(name) > limit:
            name = name[: limit - 1] + "."
        return {
            "rank": str(index + 1),
            "name": name,
            "k": _compact(_int(entry.get("kills"))),
            "d": _compact(_int(entry.get("deaths"))),
            "kd": self._kd(entry),
            "pts": _compact(_int(entry.get("scores"))),
        }

    def _refresh_board(self, initial: bool = False) -> None:
        if mystats is None or not self._lb_rows:
            return
        if not _alive(self._lb_rows[0]["name"]):
            return
        try:
            entries = self._top_entries(len(self._lb_rows))
        except Exception:
            _LOG.debug("Leaderboard cache read failed.", exc_info=True)
            return
        for i, row in enumerate(self._lb_rows):
            values = self._row_values(i, entries[i] if i < len(entries) else None)
            sig = tuple(values[k] for k in _FIELDS)
            if sig == row["sig"]:
                continue
            row["sig"] = sig
            if initial:
                self._write_row(row, values)
                continue
            for key in _FIELDS:
                if _alive(row[key]):
                    bs.animate(row[key], "opacity", {0.0: 1.0, 0.15: 0.0})
            bs.timer(0.16, bs.WeakCall(self._finish_row, i, values))

    @staticmethod
    def _write_row(row: dict, values: dict) -> None:
        for key in _FIELDS:
            node = row[key]
            if _alive(node):
                node.text = values[key]

    def _finish_row(self, index: int, values: dict) -> None:
        if index >= len(self._lb_rows):
            return
        row = self._lb_rows[index]
        self._write_row(row, values)
        for key in _FIELDS:
            if _alive(row[key]):
                bs.animate(row[key], "opacity", {0.0: 0.0, 0.25: 1.0})

    # ----------------------------------------------------------- highlights
    def _setup_highlights(self) -> None:
        hs = self._tom.get("center highlights", {})
        self._hl_settings = hs if isinstance(hs, dict) else {}
        source = HUD_TEXT["center_highlights"]
        if HUD_TEXT["use_setting_json_center_highlights"]:
            msgs = self._hl_settings.get("msg", [])
            if isinstance(msgs, (list, tuple)):
                source = msgs
        self._highlights = [m for m in source if isinstance(m, str) and m.strip()]
        if self._highlights:
            self._hl_color = self._resolve_hl_color()
            self._repeat(8.0, self._show_highlight)

    def _resolve_hl_color(self):
        try:
            color = tuple(float(v) for v in self._hl_settings.get("color", (1, 1, 1)))
            return color if len(color) == 3 else (1.0, 1.0, 1.0)
        except (TypeError, ValueError):
            return (1.0, 1.0, 1.0)

    def _show_highlight(self) -> None:
        if self._hl_settings.get("randomColor", False):
            color = (random.random(), random.random(), random.random())
        else:
            color = self._hl_color
        msg = self._highlights[self._highlight_index % len(self._highlights)]
        self._highlight_index += 1
        node = _text(msg, (0, LAYOUT["highlight_y"]), scale=1.0, color=color,
                     opacity=0.0)
        bs.animate(node, "opacity", {0.0: 0.0, 0.4: 1.0, 6.4: 1.0, 7.0: 0.0})
        bs.timer(7.1, functools.partial(_safe_delete, node))


# =============================================================================
# API 9 installer (called by custom_hooks.py -> features.text_on_map.install())
# =============================================================================
_CURRENT_REF: weakref.ref | None = None  # weak: never keeps an activity alive


def get_instance() -> textonmap | None:
    """HUD of the current activity, if any (for other mods)."""
    activity = _CURRENT_REF() if _CURRENT_REF is not None else None
    return getattr(activity, _HUD_ATTR, None) if activity is not None else None


def _ensure_for_activity(activity) -> bool:
    global _CURRENT_REF
    if not isinstance(activity, bs.GameActivity):
        return False
    if getattr(activity, _HUD_ATTR, None) is not None:
        return True
    settings = _get_settings_snapshot()  # read once per game
    if not _tom_enabled(settings):
        return False
    try:
        with activity.context:
            instance = textonmap(settings)
        setattr(activity, _HUD_ATTR, instance)  # lifetime == activity lifetime
        _CURRENT_REF = weakref.ref(activity)
        return True
    except Exception:
        _LOG.exception("Could not create LuciousHUD for GameActivity.")
        return False


def install() -> bool:
    if not _text_on_map_enabled():
        _LOG.info("TextOnMap disabled in setting.json; HUD not installed.")
        return True

    activity_class = bs.Activity
    original_begin = getattr(activity_class, "begin", None)
    if not callable(original_begin):
        _LOG.error("LuciousHUD requires bascenev1.Activity.begin (API 9).")
        return False

    if not getattr(original_begin, "_text_on_map_begin_wrapped", False):
        @functools.wraps(original_begin)
        def wrapped_activity_begin(self, *args, **kwargs):
            result = original_begin(self, *args, **kwargs)
            if isinstance(self, bs.GameActivity):
                try:
                    _ensure_for_activity(self)
                except Exception:
                    _LOG.exception("LuciousHUD begin hook failed.")
            return result

        wrapped_activity_begin._text_on_map_begin_wrapped = True
        activity_class.begin = wrapped_activity_begin

    try:
        activity = bs.get_foreground_host_activity()
        if isinstance(activity, bs.GameActivity):
            has_begun = getattr(activity, "has_begun", None)
            if not callable(has_begun) or has_begun():
                _ensure_for_activity(activity)
    except Exception:
        _LOG.exception("Could not check the current foreground activity.")
    return True
