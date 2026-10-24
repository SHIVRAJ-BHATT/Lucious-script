# Released under the MIT License. See LICENSE for details.
"""LUCIOUS HUD - default variant (see hud_common.py for the engine).

Edit OVERRIDES to change anything. Only list the keys you want to change;
everything else comes from hud_common.DEFAULTS. Note that list values
(feature_messages, colour lists) must be written out in full when overridden.

Example:
    OVERRIDES = {
        "BRAND": {"y": -48, "scale": 1.2},
        "LAYOUT": {"show_online": False},
        "LEADERBOARD": {"panel_opacity": 0.6},
    }
"""

from __future__ import annotations

from textonmap.hud_common import (LuciousHUD, build_config,
                                  get_settings_snapshot, get_instance, install)

OVERRIDES: dict = {}

CONFIG = build_config(OVERRIDES)

setti = get_settings_snapshot()  # legacy name kept for local add-ons


class textonmap(LuciousHUD):
    """Entry point looked up by textonmap.manager."""

    CONFIG = CONFIG


__all__ = ["textonmap", "CONFIG", "OVERRIDES", "install", "get_instance"]
