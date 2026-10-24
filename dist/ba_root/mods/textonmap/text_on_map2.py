# Released under the MIT License. See LICENSE for details.
"""LUCIOUS HUD - variant 2 (see hud_common.py for the engine).

Starts with the original wording (owner/editor both LUCIOUS). Add any other
key from hud_common.DEFAULTS to OVERRIDES to make this variant look different.
"""

from __future__ import annotations

from textonmap.hud_common import (LuciousHUD, build_config,
                                  get_settings_snapshot, get_instance, install)

OVERRIDES: dict = {
    "HUD_TEXT": {
        "feature_messages": [
            ("\ue043 TOP 2  |  ADMIN \ue043", (1.00, 0.82, 0.28)),
            ("\ue043 TOP 5  |  VIP \ue043", (0.66, 0.88, 1.00)),
            ("\ue043 TOP 10  |  EFFECTS + TAG \ue043", (0.82, 0.70, 1.00)),
            ("\ue043 TOP 20  |  TAG \ue043", (0.62, 1.00, 0.82)),
            ("\ue047 JOIN DISCORD \ue048", (0.55, 0.90, 1.00)),
            ("\ue04c OWNER : LUCIOUS \ue04d", (0.40, 1.00, 1.00)),
        ],
        "card_owner": "\ue043 OWNER: LUCIOUS \ue043",
        "card_editor": "\ue043 EDITOR: LUCIOUS \ue043",
    },
}

CONFIG = build_config(OVERRIDES)

setti = get_settings_snapshot()  # legacy name kept for local add-ons


class textonmap(LuciousHUD):
    """Entry point looked up by textonmap.manager."""

    CONFIG = CONFIG


__all__ = ["textonmap", "CONFIG", "OVERRIDES", "install", "get_instance"]
