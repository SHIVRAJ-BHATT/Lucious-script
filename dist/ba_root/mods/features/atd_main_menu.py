# ba_meta require api 9

from __future__ import annotations

import random

import babase
import bascenev1 as bs


class ATDMainMenuActivity(bs.Activity[bs.Player, bs.Team]):

    def __init__(self, settings: dict):
        super().__init__(settings)
        self._atd_party_timer: bs.Timer | None = None

    def on_transition_in(self) -> None:
        super().on_transition_in()

        gnode = self.globalsnode
        gnode.camera_mode = 'follow'

        self.atd_logo = bs.NodeActor(
            bs.newnode(
                'text',
                attrs={
                    'position': (0, -90),
                    'big': True,
                    'color': (1, 1, 1),
                    'v_align': 'center',
                    'h_align': 'center',
                    'shadow': 0.2,
                    'scale': 1.3,
                    'text': 'ATD SC 5.0',
                },
            )
        )

        bs.animate_array(
            self.atd_logo.node,
            'color',
            3,
            {
                0: (0, 0, 2),
                0.5: (0, 2, 0),
                1.0: (2, 0, 0),
                1.5: (2, 2, 0),
                2.0: (2, 0, 2),
                2.5: (0, 1, 6),
                3.0: (1, 2, 0),
            },
            loop=True,
        )

        bs.animate(
            self.atd_logo.node,
            'opacity',
            {
                0: 0.0,
                0.5: 1.0,
                1.0: 1.0,
                1.5: 0,
            },
            loop=True,
        )

        self._atd_party_timer = bs.Timer(
            0.1,
            bs.WeakCallStrict(self._party_c),
            repeat=True,
        )

        try:
            bs.setmusic(bs.MusicType.MENU)
        except Exception:
            pass

        try:
            if bs.app.classic is not None:
                bs.app.classic.invoke_main_menu_ui()
        except Exception:
            pass

    def _party_c(self) -> None:
        if self.expired:
            return

        pos = (
            -15.0 + random.random() * 30.0,
            float(random.randrange(5, 15)),
            -15.0 + random.random() * 30.0,
        )

        color = (
            random.random() * 2.0,
            random.random() * 2.0,
            random.random() * 2.0,
        )

        try:
            from bascenev1lib.actor.popuptext import PopupText
            PopupText(
                '\ue062Ashu\ue062',
                color=color,
                scale=6.0,
                position=pos,
            ).autoretain()
        except Exception:
            pass

        try:
            explosion = bs.newnode(
                'explosion',
                attrs={
                    'position': pos,
                    'radius': 5.0,
                },
            )
            explosion.color = color
            bs.timer(1.0, explosion.delete)
        except Exception:
            pass

        try:
            from bascenev1lib.actor.bomb import BombFactory
            BombFactory.get().random_explode_sound().play(position=pos)
        except Exception:
            pass

        try:
            bs.camerashake(intensity=1.0)
        except Exception:
            pass

        try:
            bs.emitfx(
                position=pos,
                emit_type='distortion',
                spread=2.0,
            )
        except Exception:
            pass

        try:
            bs.emitfx(
                position=pos,
                velocity=(0.0, 0.0, 0.0),
                count=40,
                scale=1.6,
                spread=1.2,
                chunk_type='spark',
            )
        except Exception:
            pass


def enable() -> None:
    try:
        from bascenev1lib import mainmenu
        mainmenu.MainMenuActivity = ATDMainMenuActivity
    except Exception:
        pass
