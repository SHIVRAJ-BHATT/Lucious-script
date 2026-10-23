# Ported by your friend: Freaku
# Join BCS: https://discord.gg/ucyaesh
# My GitHub: https://github.com/Freaku17/BombSquad-Mods-byFreaku

# ba_meta require api 9
from __future__ import annotations

import math
import random

import babase
import bauiv1 as bui
import bascenev1 as bs
from bascenev1lib.gameutils import SharedObjects
from bascenev1lib.actor.bomb import Bomb
from bascenev1lib.actor.popuptext import PopupText


class Floater(bs.Actor):
    def __init__(self, bounds):
        super().__init__()
        shared = SharedObjects.get()
        self.controlled = False
        self.source_player = None
        self.floaterMaterial = bs.Material()
        self.floaterMaterial.add_actions(
            conditions=('they_have_material', shared.player_material),
            actions=(
                ('modify_node_collision', 'collide', True),
                ('modify_part_collision', 'physical', True),
            ),
        )
        self.floaterMaterial.add_actions(
            conditions=(
                ('they_have_material', shared.object_material),
                'or',
                ('they_have_material', shared.footing_material),
                'or',
                ('they_have_material', self.floaterMaterial),
            ),
            actions=('modify_part_collision', 'physical', False),
        )

        self.pos = bounds
        self.px = 'random.uniform(self.pos[0], self.pos[3])'
        self.py = 'random.uniform(self.pos[1], self.pos[4])'
        self.pz = 'random.uniform(self.pos[2], self.pos[5])'

        self.node = bs.newnode(
            'prop',
            delegate=self,
            owner=None,
            attrs={
                'position': (eval(self.px), eval(self.py), eval(self.pz)),
                'mesh': bs.getmesh('landMine'),
                'light_mesh': bs.getmesh('landMine'),
                'body': 'landMine',
                'body_scale': 3,
                'mesh_scale': 3.1,
                'shadow_size': 0.25,
                'density': 999999999999999,
                'gravity_scale': 0.0,
                'color_texture': bs.gettexture('powerupCurse'),
                'reflection': 'soft',
                'reflection_scale': [0.25],
                'materials': [shared.footing_material, self.floaterMaterial],
            },
        )
        self.node2 = bs.newnode(
            'prop',
            owner=self.node,
            attrs={
                'position': (0, 0, 0),
                'body': 'sphere',
                'mesh': None,
                'color_texture': None,
                'body_scale': 1.0,
                'reflection': 'powerup',
                'density': 999999,
                'reflection_scale': [1.0],
                'mesh_scale': 1.0,
                'gravity_scale': 0,
                'shadow_size': 0.1,
                'is_area_of_interest': True,
                'materials': [shared.object_material, self.floaterMaterial],
            },
        )
        self.node.connectattr('position', self.node2, 'position')

    def pop(self):
        PopupText(
            text='\ue043\ue048Made by Lucifer\ue048\ue043',
            scale=1.3,
            position=(
                self.node.position[0],
                self.node.position[1] - 1,
                self.node.position[2],
            ),
            color=(0, 1, 1),
        ).autoretain()

    def checkCanControl(self):
        if not self.node.exists():
            return False
        if self.source_player is None or not self.source_player.is_alive():
            self.dis()
            return False
        return True

    def con(self):
        self.controlled = True
        self.checkPlayerDie()

    def up(self):
        if not self.checkCanControl():
            return
        v = self.node.velocity
        self.node.velocity = (v[0], 5, v[2])

    def upR(self):
        if not self.checkCanControl():
            return
        v = self.node.velocity
        self.node.velocity = (v[0], 0, v[2])

    def down(self):
        if not self.checkCanControl():
            return
        v = self.node.velocity
        self.node.velocity = (v[0], -5, v[2])

    def downR(self):
        if not self.checkCanControl():
            return
        v = self.node.velocity
        self.node.velocity = (v[0], 0, v[2])

    def leftright(self, value):
        if not self.checkCanControl():
            return
        v = self.node.velocity
        self.node.velocity = (5 * value, v[1], v[2])

    def updown(self, value):
        if not self.checkCanControl():
            return
        v = self.node.velocity
        self.node.velocity = (v[0], v[1], -5 * value)

    def dis(self):
        if self.node.exists():
            self.controlled = False
            self.node.velocity = (0, 0, 0)
            self.move()

    def checkPlayerDie(self):
        if not self.controlled or self.source_player is None:
            return
        if self.source_player.is_alive():
            bs.timer(1, self.checkPlayerDie)
        else:
            self.dis()

    @staticmethod
    def distance(x1, y1, z1, x2, y2, z2):
        return math.sqrt(
            (x2 - x1) ** 2 + (y2 - y1) ** 2 + (z2 - z1) ** 2
        )

    def drop(self):
        try:
            np = self.node.position
        except Exception:
            np = (0, 0, 0)
        self.b = Bomb(
            bomb_type=random.choice(
                ['normal', 'ice', 'sticky', 'impact', 'land_mine', 'tnt']
            ),
            source_player=self.source_player,
            position=(np[0], np[1] - 1, np[2]),
            velocity=(0, -1, 0),
        ).autoretain()
        if self.b.bomb_type in ['impact', 'land_mine']:
            self.b.arm()

    def move(self):
        if not self.node.exists() or self.controlled:
            return

        # Choose a fresh random destination within the map bounds.
        px = eval(self.px)
        py = eval(self.py)
        pz = eval(self.pz)
        pn = self.node.position
        dist = self.distance(pn[0], pn[1], pn[2], px, py, pz)

        # Avoid division by zero if the destination matches the current position.
        if dist <= 0.05:
            self.node.velocity = (0, 0, 0)
            bs.timer(0.1, bs.WeakCallStrict(self.move))
            return

        self.node.velocity = (
            (px - pn[0]) / dist,
            (py - pn[1]) / dist,
            (pz - pn[2]) / dist,
        )

        # Keep the delay non-negative; dist - 1 can be negative near the target.
        bs.timer(max(0.1, dist - 1.0), bs.WeakCallStrict(self.move))

    def handlemessage(self, msg):
        if isinstance(msg, bs.DieMessage):
            self.node.delete()
            self.node2.delete()
            self.controlled = False
        elif isinstance(msg, bs.OutOfBoundsMessage):
            self.handlemessage(bs.DieMessage())
        else:
            super().handlemessage(msg)


def assignFloInputs(clientID: int):
    activity = bs.get_foreground_host_activity()
    if activity is None:
        return

    with activity.context:
        if not hasattr(activity, 'flo') or not activity.flo.node.exists():
            try:
                activity.flo = Floater(
                    activity.map.get_def_bound_box('map_bounds')
                )
            except Exception:
                return  # Probably in the main menu or score screen.

        floater = activity.flo
        if floater.controlled:
            bs.broadcastmessage(
                'Floater is already being controlled',
                color=(1, 0, 0),
                transient=True,
                clients=[clientID],
            )
            return

        bs.broadcastmessage(
            'You Gained Control Over The Floater!\n'
            'Press Bomb to Throw Bombs and Punch to leave!',
            clients=[clientID],
            transient=True,
            color=(0, 1, 1),
        )

        for player in activity.players:
            if player.sessionplayer.inputdevice.client_id == clientID:
                def dis(player, floater):
                    try:
                        if player.actor and player.actor.node.exists():
                            player.actor.node.invincible = False
                            player.resetinput()
                            player.actor.connect_controls_to_player()
                    finally:
                        floater.dis()

                ps = player.actor.node.position
                player.actor.node.invincible = True
                floater.node.position = (ps[0], ps[1] + 1.0, ps[2])
                bs.timer(1, floater.pop)
                player.actor.node.hold_node = bs.Node(None)
                player.actor.node.hold_node = floater.node2
                player.actor.connect_controls_to_player()
                player.actor.disconnect_controls_from_player()
                player.resetinput()
                floater.source_player = player
                floater.con()
                player.assigninput(babase.InputType.PICK_UP_PRESS, floater.up)
                player.assigninput(babase.InputType.PICK_UP_RELEASE, floater.upR)
                player.assigninput(babase.InputType.JUMP_PRESS, floater.down)
                player.assigninput(babase.InputType.JUMP_RELEASE, floater.downR)
                player.assigninput(babase.InputType.BOMB_PRESS, floater.drop)
                # No callback-time arguments are needed, so use CallStrict.
                player.assigninput(
                    babase.InputType.PUNCH_PRESS,
                    babase.CallStrict(dis, player, floater),
                )
                player.assigninput(babase.InputType.UP_DOWN, floater.updown)
                player.assigninput(
                    babase.InputType.LEFT_RIGHT, floater.leftright
                )


old_fcm = bs.chatmessage


def new_chat_message(*args, **kwargs):
    old_fcm(*args, **kwargs)
    if args and args[0] == '/floater':
        try:
            assignFloInputs(-1)
        except Exception:
            pass


bs.chatmessage = new_chat_message


# ba_meta export babase.Plugin
class byFreaku(babase.Plugin):
    def on_app_running(self):
        pass
