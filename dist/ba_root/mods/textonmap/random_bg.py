# textonmap/random_bg.py
from __future__ import annotations

import logging
import random

_LOG = logging.getLogger("Lucious.RandomBG")
_last: str | None = None

# name, tint, ambient_color, vignette_outer, vignette_inner
THEMES = (
    ("sunset",   (1.25, 1.00, 0.80), (1.20, 1.00, 0.85), (0.65, 0.40, 0.35), (1.00, 0.95, 0.90)),
    ("ocean",    (0.85, 1.05, 1.30), (0.85, 1.00, 1.20), (0.30, 0.50, 0.65), (0.95, 0.98, 1.00)),
    ("toxic",    (0.90, 1.25, 0.85), (0.90, 1.15, 0.90), (0.30, 0.60, 0.35), (0.97, 1.00, 0.97)),
    ("neon",     (1.15, 0.85, 1.30), (1.10, 0.90, 1.20), (0.55, 0.30, 0.65), (1.00, 0.96, 1.00)),
    ("golden",   (1.30, 1.15, 0.80), (1.20, 1.10, 0.90), (0.60, 0.50, 0.30), (1.00, 0.98, 0.92)),
    ("ice",      (0.90, 1.15, 1.35), (0.95, 1.10, 1.25), (0.35, 0.55, 0.70), (0.97, 0.99, 1.00)),
    ("crimson",  (1.30, 0.85, 0.85), (1.20, 0.90, 0.90), (0.65, 0.25, 0.25), (1.00, 0.95, 0.95)),
    ("candy",    (1.25, 0.95, 1.15), (1.15, 0.95, 1.10), (0.65, 0.35, 0.50), (1.00, 0.96, 0.98)),
    ("midnight", (0.75, 0.80, 1.10), (0.80, 0.85, 1.05), (0.20, 0.25, 0.45), (0.90, 0.92, 1.00)),
)


def apply(activity) -> str:
    """Runs inside the activity context."""
    global _last
    name, tint, ambient, outer, inner = random.choice(
        [t for t in THEMES if t[0] != _last]
    )
    _last = name
    g = activity.globalsnode
    g.tint = tint
    g.ambient_color = ambient
    g.vignette_outer = outer
    g.vignette_inner = inner
    _LOG.info("Random background theme: %s", name)
    return name
