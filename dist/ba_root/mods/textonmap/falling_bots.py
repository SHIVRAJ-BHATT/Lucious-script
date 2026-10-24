# textonmap/falling_bots.py
from __future__ import annotations

import logging
import random

import bascenev1 as bs

_LOG = logging.getLogger("Lucious.FallingBots")

COUNT = 14                 # every client receives these nodes; keep it modest
OPACITY = 0.35
SIZE = (36.0, 72.0)
FALL_SECONDS = (7.0, 14.0)
X_RANGE = (-620.0, 620.0)
Y_TOP, Y_BOTTOM = 430.0, -430.0
FALLBACK_ICONS = (
    "neoSpazIcon", "zoeIcon", "ninjaIcon", "kronkIcon", "melIcon", "jackIcon",
    "santaIcon", "frostyIcon", "bonesIcon", "bearIcon", "penguinIcon",
    "aliIcon", "cyborgIcon", "agentIcon", "pixieIcon", "bunnyIcon", "wizardIcon",
)


def _resolve(value):
    if isinstance(value, str):
        return bs.gettexture(value)
    getter = getattr(value, "get", None)
    return getter() if callable(getter) else value


def _icon_textures() -> list:
    names: list = []
    classic = getattr(bs.app, "classic", None)
    appearances = getattr(classic, "spaz_appearances", None)
    if isinstance(appearances, dict):
        for appearance in appearances.values():
            icon = getattr(appearance, "icon_texture", None)
            if icon:
                names.append(icon)
    if not names:
        names = list(FALLBACK_ICONS)
    out = []
    for name in names:
        try:
            out.append(_resolve(name))
        except Exception:
            continue
    return out


def build(activity):
    """Runs inside the activity context."""
    textures = _icon_textures()
    if not textures:
        _LOG.warning("No character icon textures found; falling bots skipped.")
        return None
    for _ in range(COUNT):
        size = random.uniform(*SIZE)
        x = random.uniform(*X_RANGE)
        duration = random.uniform(*FALL_SECONDS)
        delay = random.uniform(0.0, duration)
        drift = random.uniform(-70.0, 70.0)
        spin = random.choice((-1, 1)) * random.uniform(180.0, 540.0)
        node = bs.newnode("image", attrs={
            "texture": random.choice(textures),
            "attach": "center",
            "absolute_scale": True,
            "scale": (size, size),
            "opacity": OPACITY,
            "position": (x, Y_TOP),
        })
        bs.animate_array(node, "position", 2, {
            0.0: (x, Y_TOP), duration: (x + drift, Y_BOTTOM),
        }, loop=True, offset=delay)
        bs.animate(node, "rotate", {0.0: 0.0, duration: spin},
                   loop=True, offset=delay)
    return True
