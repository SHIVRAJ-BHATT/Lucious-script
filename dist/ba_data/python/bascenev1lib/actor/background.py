# Released under the MIT License. See LICENSE for details.
#
"""Defines Actor(s)."""

import weakref
import logging
from typing import TYPE_CHECKING, override

import bascenev1 as bs
from bascenev1 import _classicassets

if TYPE_CHECKING:
    from typing import Any


class Background(bs.Actor):
    """Simple Fading Background Actor without the logo overlay."""

    def __init__(
        self,
        fade_time: float = 0.5,
        start_faded: bool = False,
        show_logo: bool = False,
    ):
        super().__init__()

        self._dying = False
        self.fade_time = fade_time

        # Keep the background in the session scene so it can
        # overlap multiple activities during transitions.
        session = bs.getsession()
        self._session = weakref.ref(session)

        with session.context:
            self.node = bs.newnode(
                'image',
                delegate=self,
                attrs={
                    'fill_screen': True,
                    'texture': _classicassets.textures.powerup_curse.get(),
                    'tilt_translate': -0.3,
                    'has_alpha_channel': False,
                    'color': (1, 1, 1),
                },
            )

            # Fade in the background.
            if not start_faded:
                bs.animate(
                    self.node,
                    'opacity',
                    {0.0: 0.0, self.fade_time: 1.0},
                    loop=False,
                )

            # The logo overlay has intentionally been removed.
            # The show_logo parameter remains for compatibility
            # with existing scripts that use this actor.

    @override
    def __del__(self) -> None:
        # Remove the session-scene node when the actor is destroyed.
        self._die()
        super().__del__()

    def _die(self, immediate: bool = False) -> None:
        session = self._session()

        if session is None and self.node:
            logging.exception(
                'got None session on Background _die'
                ' (and node still exists!)'
            )

        elif session is not None:
            with session.context:
                if not self._dying and self.node:
                    self._dying = True

                    if immediate:
                        self.node.delete()
                    else:
                        bs.animate(
                            self.node,
                            'opacity',
                            {0.0: 1.0, self.fade_time: 0.0},
                            loop=False,
                        )

                        bs.timer(
                            self.fade_time + 0.1,
                            self.node.delete,
                        )

    @override
    def handlemessage(self, msg: Any) -> Any:
        assert not self.expired

        if isinstance(msg, bs.DieMessage):
            self._die(msg.immediate)
        else:
            super().handlemessage(msg)