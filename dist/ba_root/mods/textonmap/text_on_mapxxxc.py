# Released under the MIT License. See LICENSE for details.
"""Text overlays for BombSquad API 9 (targeted at the 1.8.3 server).

Optimized API 9 HUD. Player-facing strings are centralized in HUD_TEXT; the
leaderboard keeps its established alignment and coordinates while using a
rank-colour palette and subtle native colour animations.
"""

import logging
import math
import random
import time

import _babase
import babase
import bascenev1 as bs
import setting

_LOG = logging.getLogger("TextOnMap")

try:
    from stats import mystats
except Exception:
    mystats = None
    _LOG.warning(
        "Stats module is unavailable; leaderboard overlay will be skipped.",
        exc_info=True,
    )

# ============================================================================
# EASY CONFIGURATION: edit player-facing text in HUD_TEXT and visual styling in
# LEADERBOARD_STYLE below. Positions/alignment for the leaderboard are kept
# exactly as in the supplied file.
# Note: \ue codes are Python Unicode escapes for BombSquad's private-use glyphs.
# ============================================================================
HUD_TEXT = {
    # Rotating feature card. Each entry is: (TEXT, RGB colour).
    "feature_messages": [
        ("\ue043 TOP 2  |  ADMIN \ue043", (1.00, 0.82, 0.28)),
        ("\ue043 TOP 5  |  VIP \ue043", (0.66, 0.88, 1.00)),
        ("\ue043 TOP 10  |  EFFECTS + TAG \ue043", (0.82, 0.70, 1.00)),
        ("\ue043 TOP 20  |  TAG \ue043", (0.62, 1.00, 0.82)),
        ("\ue047 JOIN DISCORD \ue048", (0.55, 0.90, 1.00)),
        ("\ue043 MADE BY LUCIOUS \ue043", (1.00, 0.60, 0.86)),
        ("\ue04c OWNER:LUCIOUS \ue04d", (0.40, 1.00, 1.00)),
    ],
    # Text overlays.
    "watermark_line": "__________________________________",
    "watermark_owner": "\ue043 OWNER: LUCIOUS \ue043",
    "watermark_editor": "\ue043 EDITOR: LUCIFER \ue043",
    "bottom_thanks": "\ue043 THANK YOU FOR PLAYING IN OUR SERVER \ue043",
    "clay_text": " \ue043 L U C I O U S \ue043 ",
    "next_game": "\ue047 NEXT \ue048 : {map}",
    "season_reset": "\ue04c SEASON ENDS IN: {days} DAYS \ue04d",
    "restart_message": "\ue04c SERVER GOING TO RESTART AFTER THIS SERIES \ue04d",
    # Used when the center-highlight messages are not configured elsewhere.
    # These are the centrally editable defaults for that overlay.
    # Empty by default: do not create invisible whitespace-only nodes.
    # Set use_setting_json_center_highlights=True to use setting.json messages.
    "center_highlights": [],
    # False means the list immediately above is the source of truth. Set True
    # only if you prefer setting.json's center-highlights msg list to override it.
    "use_setting_json_center_highlights": False,
    # Leaderboard labels and row formatting.
    "leaderboard_title": "\ue04c TOP 3 PLAYERS \ue04d",
    "leaderboard_headers": ("\ue043 PLAYER", "\ue047 K", "\ue04d D", "\ue048 PTS"),
    "leaderboard_row_name": "{badge} {rank}.{name}",
    "leaderboard_empty_name": "{badge} {rank}.-",
    "leaderboard_empty_value": "",
    "leaderboard_rank_badges": ("\ue04c", "\ue04d", "\ue043"),
    "leaderboard_name_length": 5,
}

LEADERBOARD_STYLE = {
    # Premium gold, cool silver, and warm bronze; positions remain unchanged.
    "rank_colors": (
        (1.00, 0.76, 0.18),
        (0.68, 0.86, 1.00),
        (1.00, 0.53, 0.34),
    ),
    "rank_glow_colors": (
        (1.00, 0.94, 0.58),
        (0.96, 0.99, 1.00),
        (1.00, 0.80, 0.62),
    ),
    "header_color": (0.66, 0.91, 1.00),
    "title_colors": (
        (1.00, 0.72, 0.12),
        (1.00, 0.94, 0.52),
        (1.00, 0.56, 0.18),
    ),
    "animation_cycle": 3.6,
}

def _get_settings_snapshot() -> dict:
    """Return settings safely without assuming the JSON shape is valid."""
    try:
        data = setting.get_settings_data()
        return data if isinstance(data, dict) else {}
    except Exception:
        _LOG.warning("Could not read TextOnMap settings.", exc_info=True)
        return {}


def _text_on_map_enabled() -> bool:
    """Return True only when the central TextOnMap setting is explicitly enabled."""
    config = _get_settings_snapshot()
    section = config.get("textonmap")
    return isinstance(section, dict) and section.get("enable") is True


# Retain the old module-level name for compatibility with any local additions.
setti = _get_settings_snapshot()


class textonmap:
    """Creates and updates the existing HUD text overlays."""

    def __init__(self):
        self._settings_snapshot = _get_settings_snapshot()
        data = self._settings_snapshot.get("textonmap", {})
        if not isinstance(data, dict):
            data = {}
        self._disabled = data.get("enable") is not True
        self.index = 0
        highlight_settings = data.get('center highlights', {})
        if not isinstance(highlight_settings, dict):
            highlight_settings = {}
        self.highlight_settings = highlight_settings
        configured_highlights = highlight_settings.get("msg", [])
        if (
            HUD_TEXT["use_setting_json_center_highlights"]
            and isinstance(configured_highlights, (list, tuple))
        ):
            highlight_source = configured_highlights
        else:
            highlight_source = HUD_TEXT["center_highlights"]
        # Do not periodically create text nodes for blank/invalid messages.
        self.highlights = [
            item for item in highlight_source
            if isinstance(item, str) and item.strip()
        ]
        self._gradient_started = time.monotonic()

        # Copy the top-level configuration so runtime updates cannot mutate it.
        self._feature_msgs = list(HUD_TEXT["feature_messages"])
        self._feature_index = 0
        self._lb_rows = []

        if self._disabled:
            return

        # Resolve the next map defensively; this does not affect other overlays.
        next_map = ""
        try:
            session = bs.get_foreground_host_session()
            if session is not None:
                description = session.get_next_game_description()
                if description is not None:
                    next_map = description.evaluate()
        except Exception:
            pass

        # The original script reads/updates the configured top-watermark text
        # but never renders it. Do not add it here, as that would change the HUD.

        # Initialize all of the same components in the same order.
        self.left_watermark()
        self.bottom_text()
        self.nextGame(next_map)
        self.restart_msg()
        try:
            self.clay_text()
        except Exception:
            _LOG.exception("Could not initialize the decorative clay text.")
        leaderboard_settings = self._settings_snapshot.get("leaderboard", {})
        if not isinstance(leaderboard_settings, dict):
            leaderboard_settings = {}
        self._leaderboard_enabled = (
            leaderboard_settings.get("enable", False) is True
            and mystats is not None
        )
        self.init_feature_cycle()

        try:
            season_days = getattr(_babase, 'season_ends_in_days', None)
            if season_days is not None and season_days < 99:
                self.season_reset(season_days)
        except Exception:
            pass

        if self._leaderboard_enabled:
            try:
                self.leaderBoard()
            except Exception:
                _LOG.exception("Could not initialize the TextOnMap leaderboard.")

        # The built-in HUD uses native scene-node animations. Do not schedule
        # a recurring Python animation callback; _animate() remains available
        # for optional external compatibility but is not polled by this HUD.

        # Only create a highlight timer when there are non-blank messages.
        if self.highlights:
            self.timer = bs.timer(
                8.0, babase.CallStrict(self.highlights_), repeat=True
            )

    # ================= FEATURE TEXT =================
    def init_feature_cycle(self):
        self._feature_text = bs.newnode('text', attrs={
            'text': '',
            'flatness': 1.0,
            'h_align': 'right',
            'v_attach': 'bottom',
            'h_attach': 'right',
            'scale': 0.65,
            'position': (-25, 40),
            'opacity': 0.0,
        })
        self._feature_timer = bs.timer(
            4.0, self._update_feature_cycle, repeat=True
        )
        self._update_feature_cycle()

    @staticmethod
    def _node_exists(node):
        """Return False for missing/deleted nodes instead of raising in timers."""
        if node is None:
            return False
        try:
            return node.exists()
        except Exception:
            return False

    def _update_feature_cycle(self):
        if not self._node_exists(getattr(self, '_feature_text', None)):
            return
        try:
            bs.animate(self._feature_text, 'opacity', {0.0: 1.0, 0.5: 0.0})
            bs.timer(0.5, self._change_feature_text)
        except Exception:
            pass

    def _change_feature_text(self):
        if not self._node_exists(getattr(self, '_feature_text', None)):
            return
        if not self._feature_msgs:
            return
        try:
            msg, color = self._feature_msgs[self._feature_index]
            self._feature_text.text = msg
            self._feature_text.color = color
            bs.animate(self._feature_text, 'opacity', {0.0: 0.0, 0.5: 1.0})
            self._feature_index = (self._feature_index + 1) % len(
                self._feature_msgs
            )
        except Exception:
            pass

    # ================= MAIN ANIMATION =================
    def _animate(self):
        """Compatibility helper for optional externally-added gradient nodes.

        This method is deliberately not scheduled as a high-frequency timer.
        External code may still call it if it manages a legacy ``_top_message``
        attribute.
        """
        top_message = getattr(self, "_top_message", None)
        if top_message is not None:
            self.smooth_gradient(
                top_message,
                (0.5, 0.5, 1.0),
                (1.0, 1.0, 1.0),
                speed=1.0,
            )

    def smooth_gradient(self, nodes, c1, c2, speed=1.0):
        """Update one or more node colours from elapsed real time.

        This helper remains available for compatibility with local extensions;
        the built-in HUD no longer invokes it on every frame.
        """
        if nodes is None:
            return
        if not isinstance(nodes, (list, tuple)):
            nodes = (nodes,)
        anim_time = (time.monotonic() - self._gradient_started) * speed
        for i, node in enumerate(nodes):
            if not self._node_exists(node):
                continue
            wave = (math.sin(anim_time + i * 0.5) + 1.0) / 2.0
            r = c1[0] + (c2[0] - c1[0]) * wave
            g = c1[1] + (c2[1] - c1[1]) * wave
            b = c1[2] + (c2[2] - c1[2]) * wave
            try:
                node.color = (r, g, b)
            except Exception:
                _LOG.debug("Could not update optional gradient node colour.", exc_info=True)

    # ================= CLAY TEXT =================
    def clay_text(self):
        # Keep one node per character and the original keyframes/layout.
        text = HUD_TEXT["clay_text"]
        scale_val = 0.9
        spacing = 20
        base_y = -38
        self.nodes = []
        start_x = -(len(text) * spacing) / 2
        for i, char in enumerate(text):
            if char == ' ':
                continue
            try:
                x = start_x + i * spacing
                node = bs.newnode('text', attrs={
                    'text': char,
                    'flatness': 1.0,
                    'shadow': 0.5,
                    'scale': scale_val,
                    'h_align': 'center',
                    'v_align': 'center',
                    'h_attach': 'center',
                    'v_attach': 'top',
                    'position': (x, base_y),
                    'color': (1.0, 0.45, 0.75),
                })
                delay = i * 0.12
                bs.animate_array(node, 'color', 3, {
                    0.0: (1.0, 0.45, 0.75),
                    1.5: (1.0, 1.0, 1.0),
                    3.0: (1.0, 0.85, 0.45),
                    4.5: (1.0, 0.45, 0.75),
                }, loop=True, offset=delay)
                bs.animate_array(node, 'position', 2, {
                    0.0: (x, base_y),
                    0.5: (x, base_y + 8),
                    1.0: (x, base_y),
                    2.0: (x, base_y),
                }, loop=True, offset=delay)
                self.nodes.append(node)
            except Exception:
                _LOG.debug(
                    "Could not create decorative clay-text character %r.",
                    char,
                    exc_info=True,
                )

    # ================= LEFT WATERMARK =================
    @staticmethod
    def _icon(text):
        return text.replace('\\c', u'\ue043')

    def _animated_line(self, text, x, y, colors, spacing=10, scale=0.6):
        """Retain the original character-by-character shimmer and wave."""
        nodes = []
        color_keys = {}
        step = 1.2
        for k, color in enumerate(colors + [colors[0]]):
            color_keys[k * step] = color

        # A node per character is intentional: combining these would change
        # the original per-character animation visible to players.
        for i, ch in enumerate(text):
            px = x + i * spacing
            node = bs.newnode('text', attrs={
                'text': ch,
                'flatness': 1.0,
                'h_align': 'left',
                'v_attach': 'bottom',
                'h_attach': 'left',
                'scale': scale,
                'position': (px, y),
                'color': colors[0],
            })
            delay = i * 0.1
            bs.animate_array(
                node, 'color', 3, color_keys, loop=True, offset=delay
            )
            bs.animate_array(node, 'position', 2, {
                0.0: (px, y),
                0.4: (px, y + 2.5),
                0.8: (px, y),
                1.6: (px, y),
            }, loop=True, offset=delay)
            nodes.append(node)
        return nodes

    def left_watermark(self):
        start_x = 25
        base_y = 67

        bs.newnode('text', attrs={
            'text': HUD_TEXT["watermark_line"],
            'h_align': 'left',
            'v_attach': 'bottom',
            'h_attach': 'left',
            'scale': 0.5,
            'position': (start_x, base_y + 35),
            'color': (0.2, 1.0, 0.2),
        })

        bs.newnode('text', attrs={
            'text': HUD_TEXT["watermark_line"],
            'h_align': 'left',
            'v_attach': 'bottom',
            'h_attach': 'left',
            'scale': 0.5,
            'position': (start_x, base_y - 15),
            'color': (0.2, 1.0, 0.2),
        })

        # OWNER and EDITOR rows retain the source positions, colours, and text.
        self._owner_nodes = self._animated_line(
            HUD_TEXT["watermark_owner"],
            start_x + 5,
            base_y + 15,
            [(1.0, 0.35, 0.65), (1.0, 0.84, 0.0), (1.0, 1.0, 1.0)],
        )
        self._editor_nodes = self._animated_line(
            HUD_TEXT["watermark_editor"],
            start_x + 5,
            base_y - 5,
            [(1.0, 0.84, 0.0), (1.0, 1.0, 0.55), (1.0, 0.7, 0.0)],
        )

    # ================= BOTTOM TEXT =================
    def bottom_text(self):
        text = HUD_TEXT["bottom_thanks"]
        spacing = 12
        base_y = 20
        self._bottom_text = []
        start_x = -(len(text) * spacing) / 2
        color_keys = {
            0.0: (0.85, 0.85, 0.85),
            3.0: (0.65, 0.35, 0.28),
            6.0: (0.85, 0.85, 0.85),
        }
        for i, ch in enumerate(text):
            if ch == ' ':
                continue
            x = start_x + i * spacing
            node = bs.newnode('text', attrs={
                'text': ch,
                'flatness': 1.0,
                'h_align': 'center',
                'v_attach': 'bottom',
                'scale': 0.6,
                'position': (x, base_y),
                'color': (0.85, 0.85, 0.85),
            })
            delay = i * 0.08
            bs.animate_array(
                node, 'color', 3, color_keys, loop=True, offset=delay
            )
            bs.animate_array(node, 'position', 2, {
                0.0: (x, base_y),
                0.4: (x, base_y + 8),
                0.8: (x, base_y),
                1.6: (x, base_y),
            }, loop=True, offset=delay)
            self._bottom_text.append(node)

    # ================= API 7-STYLE TOP-RIGHT LEADERBOARD (API 9) =================
    # Visual design follows the supplied older leaderboard: dark panel, crown
    # title, NAME / K / D / K/D columns, and gold/silver/bronze ranks.
    # Data comes from the API 9 stats cache; no API 7 calls or polling loops are used.
    _LB_PANEL_COLOR = (0.07, 0.07, 0.08)
    _LB_PANEL_OPACITY = 0.80
    _LB_RANK_COLORS = (
        (1.00, 0.84, 0.10),  # gold
        (0.85, 0.87, 0.92),  # silver
        (0.90, 0.55, 0.20),  # bronze
    )
    _LB_K_COLOR = (0.45, 1.00, 0.45)
    _LB_D_COLOR = (1.00, 0.40, 0.40)
    _LB_WHITE = (1.00, 1.00, 1.00)

    @staticmethod
    def _lb_int(value):
        """Normalize a stats value into a safe, non-negative integer."""
        try:
            if isinstance(value, bool):
                return 0
            return max(0, int(value or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _lb_kd(cls, entry):
        """Read cached K/D or calculate it with the stats module's convention."""
        value = entry.get("kd")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                value = float(value)
                if math.isfinite(value) and value >= 0.0:
                    return f"{value:.2f}"
            except (TypeError, ValueError, OverflowError):
                pass
        kills = cls._lb_int(entry.get("kills", 0))
        deaths = cls._lb_int(entry.get("deaths", 0))
        kd = kills / deaths if deaths > 0 else float(kills)
        return f"{kd:.2f}"

    @staticmethod
    def _lb_text(text, x, y, color, h_align="center", scale=0.6, maxwidth=None):
        """Create one lightweight label anchored to the top-right HUD corner."""
        attrs = {
            "text": str(text),
            "flatness": 1.0,
            "shadow": 0.5,
            "h_align": h_align,
            "v_align": "center",
            "h_attach": "right",
            "v_attach": "top",
            "scale": scale,
            "color": color,
            "position": (x, y),
        }
        if maxwidth is not None:
            attrs["maxwidth"] = maxwidth
        return bs.newnode("text", attrs=attrs)

    def leaderBoard(self):
        """Build the panel once; native scene nodes animate without Python polling."""
        cx = -145
        top = -112
        width, height = 262, 112

        # Store created nodes so a deleted activity node is easy to detect.
        self._lb_panel = bs.newnode(
            "image",
            attrs={
                "texture": bs.gettexture("white"),
                "color": self._LB_PANEL_COLOR,
                "opacity": self._LB_PANEL_OPACITY,
                "attach": "topRight",
                "absolute_scale": True,
                "scale": (width, height),
                "position": (cx, top - height / 2),
            },
        )

        self._lb_title = self._lb_text(
            "\U0001F451LEADERBOARD\U0001F451",
            cx,
            top + 12,
            (1.0, 0.7, 0.1),
            scale=0.85,
        )

        grey = (0.75, 0.75, 0.78)
        hy = top - 14
        self._lb_text("NAME", -250, hy, grey, h_align="left", scale=0.5)
        self._lb_text("K", -112, hy, grey, scale=0.5)
        self._lb_text("D", -72, hy, grey, scale=0.5)
        self._lb_text("K/D", -34, hy, grey, scale=0.5)

        self._lb_rows = []
        for index, color in enumerate(self._LB_RANK_COLORS):
            y = top - 38 - index * 24
            self._lb_rows.append({
                "rank": self._lb_text(str(index + 1), -262, y, color, scale=0.65),
                "name": self._lb_text("-", -246, y, color, h_align="left", scale=0.6, maxwidth=105),
                "k": self._lb_text("-", -112, y, self._LB_K_COLOR, scale=0.6),
                "d": self._lb_text("-", -72, y, self._LB_D_COLOR, scale=0.6),
                "kd": self._lb_text("-", -34, y, self._LB_WHITE, scale=0.6),
            })

        self._refresh_board()
        leaderboard_settings = self._settings_snapshot.get("leaderboard", {})
        interval = leaderboard_settings.get("refreshSeconds", 10)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            interval = 10
        interval = max(1.0, min(float(interval), 3600.0))
        # One low-frequency refresh; positions/colours are static scene data.
        self.board_timer = bs.timer(
            interval, self._refresh_board, repeat=True
        )

    def _refresh_board(self):
        """Refresh visible leaderboard rows only when a value has changed."""
        if mystats is None:
            return
        rows = getattr(self, "_lb_rows", None)
        if not rows:
            return
        if not all(self._node_exists(row.get("name")) for row in rows):
            return

        try:
            stats = getattr(mystats, "cached_stats", {}) or {}
            ranks = list(getattr(mystats, "ranks", ()) or ())
            if not isinstance(stats, dict):
                stats = {}
            # Normally ranks is populated by mystats.initialise(). If it is
            # temporarily empty, sort the existing cache by score without I/O.
            if not ranks and stats:
                ranks = sorted(
                    stats,
                    key=lambda account_id: self._lb_int(
                        (stats.get(account_id) or {}).get("scores", 0)
                    ),
                    reverse=True,
                )
            top_ids = ranks[:3]
        except Exception:
            _LOG.debug("Could not read leaderboard stats cache.", exc_info=True)
            return

        for index, row in enumerate(rows):
            entry = stats.get(top_ids[index]) if index < len(top_ids) else None
            if not isinstance(entry, dict):
                values = {
                    "rank": str(index + 1),
                    "name": "-",
                    "k": "-",
                    "d": "-",
                    "kd": "-",
                }
            else:
                name = " ".join(str(entry.get("name") or "Unknown").split())
                if not name:
                    name = "Unknown"
                values = {
                    "rank": str(index + 1),
                    "name": name,
                    "k": str(self._lb_int(entry.get("kills", 0))),
                    "d": str(self._lb_int(entry.get("deaths", 0))),
                    "kd": self._lb_kd(entry),
                }

            # Avoid redundant node writes on every timer tick.
            for key, value in values.items():
                node = row.get(key)
                if self._node_exists(node):
                    try:
                        if node.text != value:
                            node.text = value
                    except Exception:
                        _LOG.debug("Could not update leaderboard %s field.", key, exc_info=True)

    def nextGame(self, text):
        bs.newnode('text', attrs={
            'text': HUD_TEXT["next_game"].format(map=str(text)),
            'flatness': 1.0,
            'h_align': 'right',
            'v_attach': 'bottom',
            'h_attach': 'right',
            'scale': 0.7,
            'position': (-25, 16),
            'color': (1.0, 1.0, 1.0),
        })

    def highlights_(self):
        if not self.highlights:
            return
        try:
            highlight_settings = self.highlight_settings
            if highlight_settings.get('randomColor', False):
                color = (random.random(), random.random(), random.random())
            else:
                try:
                    color = tuple(highlight_settings.get('color', (1.0, 1.0, 1.0)))
                    if len(color) != 3:
                        raise ValueError('highlight color must contain three values')
                except (TypeError, ValueError):
                    color = (1.0, 1.0, 1.0)
            node = bs.newnode('text', attrs={
                'text': self.highlights[self.index],
                'flatness': 1.0,
                'h_align': 'center',
                'v_attach': 'bottom',
                'scale': 1,
                'position': (0, 138),
                'color': color,
            })
            bs.timer(7, node.delete)
            self.index = (self.index + 1) % len(self.highlights)
        except Exception:
            _LOG.debug("Could not display the configured center highlight.", exc_info=True)

    def season_reset(self, text):
        bs.newnode('text', attrs={
            'text': HUD_TEXT["season_reset"].format(days=text),
            'flatness': 5.0,
            'h_align': 'right',
            'v_attach': 'bottom',
            'h_attach': 'right',
            'scale': 0.7,
            'position': (-25, 65),
            'color': (1.0, 1.0, 1.0),
        })

    def restart_msg(self):
        if not hasattr(_babase, 'restart_scheduled'):
            return
        try:
            activity = bs.get_foreground_host_activity()
            if activity is None:
                return
            activity.restart_msg = bs.newnode('text', attrs={
                'text': HUD_TEXT["restart_message"],
                'flatness': 1.0,
                'h_align': 'right',
                'v_attach': 'bottom',
                'h_attach': 'right',
                'scale': 0.5,
                'position': (-25, 90),
                'color': (1, 0.5, 0.7),
            })
        except Exception:
            _LOG.debug("Could not create the restart announcement.", exc_info=True)


# ================= API 9 INSTALLER =================
# custom_hooks.py calls features.text_on_map.install(). Keep one overlay
# instance per GameActivity so text nodes/timers are recreated on the next
# game after the previous activity's context expires.
_INSTANCE = None
_CURRENT_ACTIVITY = None


def _install_activity_end_cleanup():
    """Release global references when the owning GameActivity ends.

    HUD nodes and timers are created in the activity context and are managed
    by Ballistica. This hook only releases our Python-side references; it does
    not delete or alter any HUD nodes directly.
    """
    activity_class = getattr(bs, 'Activity', None)
    if activity_class is None:
        _LOG.error('TextOnMap could not find bascenev1.Activity.')
        return False

    original_end = getattr(activity_class, 'end', None)
    if not callable(original_end):
        _LOG.error('TextOnMap requires bascenev1.Activity.end (API 9).')
        return False
    if getattr(original_end, '_text_on_map_end_wrapped', False):
        return True

    def wrapped_activity_end(self, *args, **kwargs):
        global _INSTANCE, _CURRENT_ACTIVITY
        is_current_activity = self is _CURRENT_ACTIVITY
        try:
            return original_end(self, *args, **kwargs)
        finally:
            if is_current_activity:
                _INSTANCE = None
                _CURRENT_ACTIVITY = None

    wrapped_activity_end._text_on_map_end_wrapped = True
    # Preserve marker attributes used by other wrappers in custom_hooks.py.
    # This prevents their idempotency checks from stacking those hooks again.
    for marker in (
        '_lucifer_stats_wrapped',
        '_lucifer_coins_wrapped',
        '_lucifer_solo_bot_end_wrapped',
    ):
        setattr(
            wrapped_activity_end,
            marker,
            bool(getattr(original_end, marker, False)),
        )
    activity_class.end = wrapped_activity_end
    return True


def _ensure_for_activity(activity):
    """Create the existing HUD once inside the target game activity context."""
    global _INSTANCE, _CURRENT_ACTIVITY

    if not _text_on_map_enabled():
        return False
    if not isinstance(activity, bs.GameActivity):
        return False
    if activity is _CURRENT_ACTIVITY and _INSTANCE is not None:
        return True

    try:
        with activity.context:
            instance = textonmap()
        _CURRENT_ACTIVITY = activity
        _INSTANCE = instance
        return True
    except Exception:
        _LOG.exception('Could not create TextOnMap HUD for GameActivity.')
        return False


def install():
    """Install the HUD integration expected by mods/custom_hooks.py.

    If textonmap.enable is false (or its setting is invalid), return True
    without installing hooks or creating HUD nodes. Enable/disable changes take
    effect after restarting the server, consistent with plugin installation.
    """
    if not _text_on_map_enabled():
        _LOG.info("TextOnMap is disabled by setting.json; HUD hooks not installed.")
        return True

    if not _install_activity_end_cleanup():
        return False

    activity_class = getattr(bs, 'Activity', None)
    original_begin = getattr(activity_class, 'begin', None)
    if activity_class is None or not callable(original_begin):
        _LOG.error('TextOnMap requires bascenev1.Activity.begin (API 9).')
        return False

    if not getattr(original_begin, '_text_on_map_begin_wrapped', False):
        def wrapped_activity_begin(self, *args, **kwargs):
            result = original_begin(self, *args, **kwargs)
            try:
                if isinstance(self, bs.GameActivity):
                    _ensure_for_activity(self)
            except Exception:
                _LOG.exception('TextOnMap activity-begin callback failed.')
            return result

        wrapped_activity_begin._text_on_map_begin_wrapped = True
        activity_class.begin = wrapped_activity_begin

    # Also support installation while a game is already active.
    try:
        activity = bs.get_foreground_host_activity()
        if isinstance(activity, bs.GameActivity):
            has_begun = getattr(activity, 'has_begun', None)
            if not callable(has_begun) or has_begun():
                _ensure_for_activity(activity)
    except Exception:
        _LOG.exception('Could not check the current foreground activity.')

    return True
