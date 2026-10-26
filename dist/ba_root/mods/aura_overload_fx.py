# ba_meta require api 9
# ba_meta name Aura Overload FX
# ba_meta description Glowing, smoothly colour-cycling aura (API 9, server-safe).
# ba_meta author KritarthaT

from __future__ import annotations

import logging
import weakref
from typing import Any

import babase
import bascenev1 as bs

OWNER_ONLY = True          # False = every player gets the aura
SECONDS_PER_COLOR = 1.0
_COLORS = [
    (1, 0, 0), (1, 1, 0), (0, 1, 0),
    (0, 1, 1), (0, 0, 1), (1, 0, 1),
]


def _wants_aura(player: Any) -> bool:
    if not OWNER_ONLY:
        return True
    try:
        from playersdata import pdata
        from tools.account_ids import get_v2_account_id

        account_id = get_v2_account_id(player)
        return bool(account_id) and bool(pdata.is_owner_account_id(account_id))
    except Exception:
        logging.exception('AuraFX: owner check failed; skipping aura.')
        return False


def _attach_aura(spaz: Any) -> None:
    node = spaz.node
    if not node:
        return

    light = bs.newnode(
        'light',
        owner=node,  # deleted automatically with the player
        attrs={
            'color': _COLORS[0],
            'radius': 0.3,
            'intensity': 0.8,
            'volume_intensity_scale': 1.0,
        },
    )
    # Follow the player: light.position = player centre + (0, 0.2, 0)
    offset = bs.newnode(
        'math',
        owner=node,
        attrs={'input1': (0.0, 0.2, 0.0), 'operation': 'add'},
    )
    node.connectattr('position_center', offset, 'input2')
    offset.connectattr('output', light, 'position')

    # Smooth looping colour fade (last key repeats first colour).
    count = len(_COLORS)
    keys = {
        i * SECONDS_PER_COLOR: _COLORS[i % count] for i in range(count + 1)
    }
    bs.animate_array(light, 'color', 3, keys, loop=True)

    # Light sparks (kept cheap: they are networked to every client).
    ref = weakref.ref(spaz)

    def _emit() -> None:
        s = ref()
        if s is None or not s.node or not s.node.exists():
            return
        bs.emitfx(
            position=s.node.position,
            velocity=(0, 2, 0),
            count=3,
            scale=0.6,
            spread=0.2,
            chunk_type='spark',
        )

    spaz._aura_spark_timer = bs.Timer(0.1, _emit, repeat=True)


# ba_meta export babase.Plugin
class AuraOverloadFX(babase.Plugin):
    def on_app_running(self) -> None:
        from bascenev1lib.actor.playerspaz import PlayerSpaz

        original = PlayerSpaz.__init__
        if getattr(original, '_aura_fx_wrapped', False):
            return

        def wrapped(self: Any, player: Any, *args: Any, **kwargs: Any) -> None:
            original(self, player, *args, **kwargs)
            try:
                if _wants_aura(player):
                    _attach_aura(self)
            except Exception:
                logging.exception('AuraFX: could not attach aura.')

        wrapped._aura_fx_wrapped = True  # type: ignore[attr-defined]
        PlayerSpaz.__init__ = wrapped  # type: ignore[method-assign]
