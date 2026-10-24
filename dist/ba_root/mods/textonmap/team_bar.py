# textonmap/team_bar.py
from __future__ import annotations

import logging

import bascenev1 as bs

_LOG = logging.getLogger("Lucious.TeamBar")

Y = -112          # just under the COMMUNITY SERVER tagline (brand y=-38, tagline y=-80)
WIDTH = 400
HEIGHT = 30
NAME_OFFSET = 112


class TeamBar:
    def __init__(self, teams) -> None:
        self._teams = teams
        self._last = ""
        tex = bs.gettexture("white")

        def image(pos, size, color, opacity):
            node = bs.newnode("image", attrs={
                "texture": tex, "color": color, "opacity": 0.0,
                "attach": "topCenter", "absolute_scale": True,
                "scale": size, "position": pos,
            })
            bs.animate(node, "opacity", {0.0: 0.0, 0.5: opacity})
            return node

        def text(value, x, scale, color, maxwidth=None):
            attrs = {
                "text": value, "position": (x, Y), "scale": scale,
                "color": color, "h_align": "center", "v_align": "center",
                "h_attach": "center", "v_attach": "top",
                "flatness": 1.0, "shadow": 0.8, "opacity": 0.0,
            }
            if maxwidth:
                attrs["maxwidth"] = maxwidth
            node = bs.newnode("text", attrs=attrs)
            bs.animate(node, "opacity", {0.0: 0.0, 0.5: 1.0})
            return node

        left, right = teams
        c_left = bs.safecolor(left.color, target_intensity=1.0)
        c_right = bs.safecolor(right.color, target_intensity=1.0)

        image((0, Y), (WIDTH, HEIGHT), (0.04, 0.04, 0.06), 0.72)
        image((-NAME_OFFSET, Y), (150, HEIGHT - 6), c_left, 0.30)
        image((NAME_OFFSET, Y), (150, HEIGHT - 6), c_right, 0.30)
        image((-NAME_OFFSET, Y - HEIGHT / 2 + 1), (150, 2), c_left, 0.95)
        image((NAME_OFFSET, Y - HEIGHT / 2 + 1), (150, 2), c_right, 0.95)

        text(left.name, -NAME_OFFSET, 0.62, c_left, maxwidth=140)
        text(right.name, NAME_OFFSET, 0.62, c_right, maxwidth=140)
        self._score = text("0 : 0", 0, 0.85, (1.0, 1.0, 1.0))

        self._timer = bs.Timer(1.0, bs.WeakCall(self._refresh), repeat=True)
        self._refresh()

    @staticmethod
    def _wins(team) -> int:
        getter = getattr(getattr(team, "customdata", None), "get", None)
        value = getter("score", 0) if callable(getter) else 0
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _refresh(self) -> None:
        a, b = (self._wins(t) for t in self._teams)
        value = f"{a} : {b}"
        if value == self._last:
            return
        self._last = value
        node = self._score
        try:
            if node.exists():
                node.text = value
                bs.animate(node, "scale", {0.0: 1.3, 0.3: 0.85})
        except Exception:
            pass


def build(activity):
    """Runs inside the activity context. Two-team sessions only."""
    session = activity.session
    teams = list(getattr(session, "sessionteams", ()))
    if len(teams) != 2:
        return None
    return TeamBar(teams)
