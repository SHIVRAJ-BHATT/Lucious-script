# ba_meta require api 9
import bascenev1 as ba
from bascenev1lib.actor import spaz


def _try_launch_ball_forward(player_spaz):
        activity = ba.getactivity()

        if type(activity).__name__ != 'QueimadaGame':
                return

        if not hasattr(activity, '_balls') or not activity._balls:
                return

        ppos = player_spaz.node.position
        bola_proxima = None
        menor_dist = 999

        for ball in activity._balls:
                if ball is None or not ball.node or not ball.node.exists():
                        continue
                bpos = ball.node.position
                dx = ppos[0] - bpos[0]
                dy = ppos[1] - bpos[1]
                dz = ppos[2] - bpos[2]
                dist = (dx**2 + dy**2 + dz**2) ** 0.5
                if dist < menor_dist:
                        menor_dist = dist
                        bola_proxima = ball

        if bola_proxima is None or menor_dist > 1.6:
                return

        bpos = bola_proxima.node.position

        fx = player_spaz.node.move_left_right
        fz = -player_spaz.node.move_up_down

        if abs(fx) < 0.1 and abs(fz) < 0.1:
                fx = 0.0
                fz = -1.0

        bola_proxima.node.handlemessage(
                "impulse",
                bpos[0], bpos[1], bpos[2],
                fx, 0, fz,
                1200, 0, 0, 0,
                fx, 0, fz
        )

        ba.emitfx(
                position=bpos,
                velocity=(fx * 5, 0, fz * 5),
                count=10,
                scale=0.8,
                spread=0.3,
                chunk_type='spark'
        )

def myOnBombPress(old_func):
        def wrapper(self):
                if not self.node.exists():
                        return

                activity = ba.getactivity()
                if type(activity).__name__ != 'QueimadaGame':
                        old_func(self)
                        return

                now = ba.apptime()
                last = getattr(self, '_last_ball_launch_time', -999)
                if now - last < 0.5:
                        return

                self._last_ball_launch_time = now
                _try_launch_ball_forward(self)

        return wrapper


# ba_meta export babase.Plugin
class QueimadaBallPlugin(ba.Plugin):
        def __init__(self):
                spaz.Spaz.on_bomb_press = myOnBombPress(spaz.Spaz.on_bomb_press)