# ba_meta require api 9
from __future__ import annotations
from typing import TYPE_CHECKING

import bascenev1 as bs
import babase
from bascenev1lib.gameutils import SharedObjects

if TYPE_CHECKING:
    pass


class TheCyborgPadMapData:
    points = {}
    boxes = {}
    boxes['area_of_interest_bounds'] = (
        (0.3544110667, 4.493562578, -2.518391331)
        + (0.0, 0.0, 0.0)
        + (16.64754831, 8.06138989, 18.5029888)
    )
    points['ffa_spawn1'] = (-3.812275836, 4.380655495, -8.962074979) + (
        2.371946621,
        1.0,
        0.8737798622,
    )
    points['ffa_spawn2'] = (4.472503025, 4.406820459, -9.007239732) + (
        2.708525168,
        1.0,
        0.8737798622,
    )
    points['ffa_spawn3'] = (6.972673935, 4.380775486, -7.424407061) + (
        0.4850648533,
        1.0,
        1.597018665,
    )
    points['ffa_spawn4'] = (-6.36978974, 4.380775486, -7.424407061) + (
        0.4850648533,
        1.0,
        1.597018665,
    )
    points['flag1'] = (-7.026110145, 4.308759233, -6.302807727)
    points['flag2'] = (7.632557137, 4.366002373, -6.287969342)
    points['flag_default'] = (0.4611826686, 4.382076338, 3.680881802)
    boxes['map_bounds'] = (
        (0.2608783669, 4.899663734, -3.543675157)
        + (0.0, 0.0, 0.0)
        + (29.23565494, 14.19991443, 29.92689344)
    )
    points['powerup_spawn1'] = (-4.166594349, 5.281834349, -6.427493781)
    points['powerup_spawn2'] = (4.426873526, 5.342460464, -6.329745237)
    points['powerup_spawn3'] = (-4.201686731, 5.123385835, 0.4400721376)
    points['powerup_spawn4'] = (4.758924722, 5.123385835, 0.3494054559)
    points['shadow_lower_bottom'] = (-0.2912522507, 2.020798381, 5.341226521)
    points['shadow_lower_top'] = (-0.2912522507, 3.206066063, 5.341226521)
    points['shadow_upper_bottom'] = (-0.2912522507, 6.062361813, 5.341226521)
    points['shadow_upper_top'] = (-0.2912522507, 9.827201965, 5.341226521)
    points['spawn1'] = (-3.902942148, 4.380655495, -8.962074979) + (
        1.66339533,
        1.0,
        0.8737798622,
    )
    points['spawn2'] = (4.775040345, 4.406820459, -9.007239732) + (
        1.66339533,
        1.0,
        0.8737798622,
    )
    points['tnt1'] = (0.4599593402, 4.044276501, -6.573537395)


class TheCyborgPadMap(bs.Map):

    defs = TheCyborgPadMapData()
    name = 'The Cyborg Pad'

    @classmethod
    def get_play_types(cls) -> list[str]:
        return ['melee', 'keep_away']

    @classmethod
    def get_preview_texture_name(cls) -> list[str]:
        return 'shield'

    @classmethod
    def on_preload(cls) -> any:
        data: dict[str, any] = {
            'mesh': bs.getmesh('thePadLevel'),
            'mesh_bottom': bs.getmesh('thePadLevelBottom'),
            'tex': bs.gettexture('thePadLevelColor'),
            'collision_mesh': bs.getcollisionmesh('thePadLevelCollide'),
            'mesh2': bs.getmesh('trees'),
            'tex2': bs.gettexture('treesColor'),
            'bgtex': bs.gettexture('shrapnel1Color'),
            'bgmesh': bs.getmesh('thePadBG'),
        }
        return data

    def __init__(self) -> None:
        super().__init__()
        shared = SharedObjects.get()

        self.node = bs.newnode(
            'terrain',
            delegate=self,
            attrs={
                'mesh': self.preloaddata['mesh'],
                'color_texture': self.preloaddata['tex'],
                'collision_mesh': self.preloaddata['collision_mesh'],
                'materials': [shared.footing_material],
                'reflection': 'soft',
                'reflection_scale': (3.5, 3, 3.5),
            }
        )
        self.bottom = bs.newnode(
            'terrain',
            attrs={
                'mesh': self.preloaddata['mesh_bottom'],
                'lighting': False,
                'color_texture': bs.gettexture('shield'),
                'reflection': 'soft',
                'reflection_scale': [10.0],
            },
        )
        self.background = bs.newnode(
            'terrain',
            attrs={
                'mesh': self.preloaddata['bgmesh'],
                'lighting': False,
                'background': True,
                'color_texture': self.preloaddata['bgtex'],
            },
        )

        gnode = bs.getactivity().globalsnode
        tint = (0.60, 0.60, 0.60)
        gnode.tint = tint
        gnode.ambient_color = (1.0, 0.96, 0.90)
        gnode.vignette_outer = (0.62, 0.64, 0.69)
        gnode.vignette_inner = (0.97, 0.95, 0.93)

        gnode.shadow_ortho = False

        credits = bs.newnode('text', attrs={
            'text': "Map by: HWProgram",
            'color': (1, 0, 0),
            'opacity': 0.9,
            'position': (-1.9, 5, -11),
            'scale': 0.0200,
            'in_world': True,
            'shadow': 0.5
        })
        bs.animate_array(credits, 'position', 3, {0: (3, 5, -11), 1: (3, 5, -11), 3: (-1.6, 5, -11), 5: (-2.4, 5, -11), 6: (-4, 5, -11), 7: (-8, 5, -11)}, loop=False)
        bs.animate(credits, 'opacity', {0: 0, 1: 0, 1.5: 0.9, 5.5: 0.9, 6: 0.5, 7: 0}, loop=False)
        bs.timer(8, credits.delete)

    def is_point_near_edge(self,
                           point: bs.Vec3,
                           running: bool = False) -> bool:
        xpos = point.x
        zpos = point.z
        x_adj = xpos * 0.125
        z_adj = (zpos + 3.7) * 0.2
        if running:
            x_adj *= 1.4
            z_adj *= 1.4
        return x_adj * x_adj + z_adj * z_adj > 1.0


# ba_meta export babase.Plugin
class HWProgram(babase.Plugin):
    def on_app_running(self) -> None:
        bs.register_map(TheCyborgPadMap)