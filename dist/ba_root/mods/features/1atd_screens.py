# ba_meta require api 9

from __future__ import annotations

import importlib
import logging
import random
import weakref
from typing import Any

import bascenev1 as bs


_PATCHED_ATTR = '_atd_score_patch_applied'
_SCORE_ACTIVITY_PATCHED_ATTR = '_atd_score_activity_patch_applied'
_SCORE_DISPLAY_PATCHED_ATTR = '_atd_score_display_patch_applied'
_SCORE_DISPLAY_DONE_ATTR = '_atd_score_display_done'
_COOP_SCORE_PATCHED_ATTR = '_atd_coop_score_patch_applied'
_FFA_VICTORY_PATCHED_ATTR = '_atd_ffa_victory_patch_applied'
_FFA_SKIP_BASE_SCORES_ATTR = '_atd_ffa_skip_base_scores'
_BACKGROUND_PATCHED_ATTR = '_atd_background_patch_applied'
_CLEANUP_PATCHED_ATTR = '_atd_screen_cleanup_patch_applied'
_EFFECTS_ATTR = '_atd_screen_effects_started'
_CLEANUP_DONE_ATTR = '_atd_screen_cleanup_done'
_BACKGROUND_SUPPRESS_DEPTH = 0

_SCREEN_CLASS_NAMES = (
    'MultiTeamScoreScreenActivity',
    'TeamScoreScreenActivity',
    'TeamVictoryScoreScreenActivity',
    'TeamSeriesVictoryScoreScreenActivity',
    'FreeForAllScoreScreenActivity',
    'FreeForAllVictoryScoreScreenActivity',
    'DrawScoreScreenActivity',
    'CoopScoreScreen',
)
_SCREEN_MODULE_NAMES = (
    'bascenev1lib.activity.multiteamscore',
    'bascenev1lib.activity.dualteamscore',
    'bascenev1lib.activity.multiteamvictory',
    'bascenev1lib.activity.freeforallscore',
    'bascenev1lib.activity.freeforallscorescreen',
    'bascenev1lib.activity.freeforallvictory',
    'bascenev1lib.activity.drawscore',
    'bascenev1lib.activity.coopscore',
)


def _delete_node(node: Any) -> None:
    try:
        if node and node.exists():
            node.delete()
    except Exception:
        logging.exception('ATD screens: failed to delete node')


def _remember_node(activity: Any, node: Any) -> None:
    if node is None:
        return
    nodes = getattr(activity, '_atd_screen_nodes', None)
    if nodes is None:
        nodes = []
        activity._atd_screen_nodes = nodes
    nodes.append(node)


def _remember_actor(activity: Any, actor: Any) -> None:
    if actor is None:
        return
    actors = getattr(activity, '_atd_screen_actors', None)
    if actors is None:
        actors = []
        activity._atd_screen_actors = actors
    actors.append(actor)


def _remember_timer(activity: Any, timer: Any) -> None:
    if timer is None:
        return
    timers = getattr(activity, '_atd_screen_timers', None)
    if timers is None:
        timers = []
        activity._atd_screen_timers = timers
    timers.append(timer)


def _cleanup_effects(activity: Any) -> None:
    if getattr(activity, _CLEANUP_DONE_ATTR, False):
        return
    setattr(activity, _CLEANUP_DONE_ATTR, True)
    # FIX: صفّر الـ flag حتى لا يحجب show_player_scores في أي استدعاء مستقبلي
    setattr(activity, _SCORE_DISPLAY_DONE_ATTR, False)

    timer = getattr(activity, '_atd_party_timer', None)
    if timer is not None:
        activity._atd_party_timer = None
    activity._atd_screen_timers = []

    for node in list(getattr(activity, '_atd_screen_nodes', [])):
        _delete_node(node)
    activity._atd_screen_nodes = []

    for actor in list(getattr(activity, '_atd_screen_actors', [])):
        _delete_node(getattr(actor, 'node', None))
    activity._atd_screen_actors = []
    setattr(activity, _EFFECTS_ATTR, False)


def _party_c(activity: Any) -> None:
    if getattr(activity, 'expired', True):
        _cleanup_effects(activity)
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
        with activity.context:
            from bascenev1lib.actor.popuptext import PopupText

            popup = PopupText(
                '\ue062Ashu\ue062',
                color=color,
                scale=6.0,
                position=pos,
            )
            _remember_actor(activity, popup)
            popup.autoretain()
    except Exception:
        logging.exception('ATD screens: failed to create popup effect')

    try:
        with activity.context:
            explosion = bs.newnode(
                'explosion',
                attrs={
                    'position': pos,
                    'radius': 5.0,
                },
            )
            _remember_node(activity, explosion)
            explosion.color = color
            _remember_timer(
                activity,
                bs.timer(1.0, bs.CallPartial(_delete_node, explosion)),
            )
    except Exception:
        logging.exception('ATD screens: failed to create explosion effect')

    try:
        with activity.context:
            from bascenev1lib.actor.bomb import BombFactory

            BombFactory.get().random_explode_sound().play(position=pos)
    except Exception:
        logging.exception('ATD screens: failed to play explosion sound')

    try:
        with activity.context:
            bs.camerashake(intensity=1.0)
    except Exception:
        logging.exception('ATD screens: failed to shake camera')

    try:
        with activity.context:
            bs.emitfx(
                position=pos,
                emit_type='distortion',
                spread=2.0,
            )
            bs.emitfx(
                position=pos,
                velocity=(0.0, 0.0, 0.0),
                count=40,
                scale=1.6,
                spread=1.2,
                chunk_type='spark',
            )
    except Exception:
        logging.exception('ATD screens: failed to emit screen effect')


def _start_atd_effects(activity: Any) -> None:
    if getattr(activity, 'expired', True):
        return
    if getattr(activity, _EFFECTS_ATTR, False):
        return

    setattr(activity, _EFFECTS_ATTR, True)
    setattr(activity, _CLEANUP_DONE_ATTR, False)
    activity._atd_screen_nodes = []
    activity._atd_screen_actors = []
    _party_c(activity)
    activity_ref = weakref.ref(activity)

    def _party_tick() -> None:
        current = activity_ref()
        if current is not None:
            _party_c(current)

    activity._atd_party_timer = bs.Timer(
        0.25,
        _party_tick,
        repeat=True,
    )


def _background_without_nodes(
    self: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    del args, kwargs
    bs.Actor.__init__(self)
    self._dying = True
    self.fade_time = 0.0
    session = bs.getsession()
    self._session = weakref.ref(session) if session is not None else lambda: None
    self.node = None
    self.logo = None


def _background_init(
    self: Any,
    *args: Any,
    _original: Any,
    **kwargs: Any,
) -> None:
    if _BACKGROUND_SUPPRESS_DEPTH == 0:
        _original(self, *args, **kwargs)
        return

    _background_without_nodes(self, *args, **kwargs)


def _run_without_background(
    original: Any,
    self: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    global _BACKGROUND_SUPPRESS_DEPTH

    _BACKGROUND_SUPPRESS_DEPTH += 1
    try:
        return original(self, *args, **kwargs)
    finally:
        _BACKGROUND_SUPPRESS_DEPTH -= 1


def _patch_background_actor() -> None:
    try:
        module = importlib.import_module('bascenev1lib.actor.background')
        background_cls = module.Background
    except (ImportError, AttributeError):
        return

    if getattr(background_cls, _BACKGROUND_PATCHED_ATTR, False):
        return

    original_init = background_cls.__init__

    def patched_init(
        self: Any,
        *args: Any,
        _original=original_init,
        **kwargs: Any,
    ) -> None:
        _background_init(
            self,
            *args,
            _original=_original,
            **kwargs,
        )

    background_cls.__init__ = patched_init
    setattr(background_cls, _BACKGROUND_PATCHED_ATTR, True)


def _patch_lifecycle_method(
    cls: type[Any],
    method_name: str,
    marker: str,
) -> None:
    if getattr(cls, marker, False):
        return

    original = cls.__dict__.get(method_name)
    if original is None:
        return

    def patched(
        self: Any,
        *args: Any,
        _original=original,
        **kwargs: Any,
    ) -> Any:
        return _run_without_background(
            _original,
            self,
            args,
            kwargs,
        )

    setattr(cls, method_name, patched)
    setattr(cls, marker, True)


def _patch_cleanup_methods(cls: type[Any]) -> None:
    for method_name in ('on_expire', 'end'):
        marker = _CLEANUP_PATCHED_ATTR + '_' + cls.__name__ + '_' + method_name
        if getattr(cls, marker, False):
            continue
        original = cls.__dict__.get(method_name)
        if original is None:
            continue

        def patched(
            self: Any,
            *args: Any,
            _original=original,
            **kwargs: Any,
        ) -> Any:
            _cleanup_effects(self)
            return _original(self, *args, **kwargs)

        setattr(cls, method_name, patched)
        setattr(cls, marker, True)


def _collect_screen_classes() -> list[type[Any]]:
    classes: list[type[Any]] = []

    def add_class(cls: Any) -> None:
        if isinstance(cls, type) and cls not in classes:
            classes.append(cls)

    def add_subclasses(cls: type[Any]) -> None:
        try:
            subclasses = cls.__subclasses__()
        except Exception:
            logging.exception('ATD screens: failed to inspect screen subclasses')
            return
        for subclass in subclasses:
            if subclass.__name__ in _SCREEN_CLASS_NAMES:
                add_class(subclass)
            add_subclasses(subclass)

    base_score_cls = getattr(bs, 'ScoreScreenActivity', None)
    if base_score_cls is not None:
        add_class(base_score_cls)
        add_subclasses(base_score_cls)

    for module_name in _SCREEN_MODULE_NAMES:
        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue
        for class_name in _SCREEN_CLASS_NAMES:
            add_class(getattr(module, class_name, None))

    return classes


def _patch_score_activity_with(classes: list[type[Any]]) -> None:
    for cls in classes:
        marker_prefix = _SCORE_ACTIVITY_PATCHED_ATTR + '_' + cls.__name__
        for method_name in ('__init__', 'on_transition_in', 'on_begin'):
            _patch_lifecycle_method(
                cls,
                method_name,
                marker_prefix + '_' + method_name,
            )


def _patch_coop_score() -> None:
    try:
        module = importlib.import_module('bascenev1lib.activity.coopscore')
        cls = module.CoopScoreScreen
    except Exception:
        return

    if getattr(cls, _COOP_SCORE_PATCHED_ATTR, False):
        return

    original = getattr(cls, 'on_transition_in', None)
    if original is None:
        return

    def patched(
        self: Any,
        *args: Any,
        _original=original,
        **kwargs: Any,
    ) -> Any:
        return _run_without_background(
            _original,
            self,
            args,
            kwargs,
        )

    setattr(cls, 'on_transition_in', patched)
    setattr(cls, _COOP_SCORE_PATCHED_ATTR, True)


def _patch_ffa_victory_with(classes: list[type[Any]]) -> None:
    cls = next(
        (
            screen_cls
            for screen_cls in classes
            if screen_cls.__name__ == 'FreeForAllVictoryScoreScreenActivity'
        ),
        None,
    )
    if cls is None or getattr(cls, _FFA_VICTORY_PATCHED_ATTR, False):
        return

    original_on_begin = cls.__dict__.get('on_begin')
    # FIX: استخدام __dict__.get بدل getattr حتى لا يلتقط نسخة معدّلة من كلاس أب
    original_show_player_scores = cls.__dict__.get('show_player_scores')
    if original_on_begin is None or original_show_player_scores is None:
        return

    def patched_show_player_scores(
        self: Any,
        *args: Any,
        _original=original_show_player_scores,
        **kwargs: Any,
    ) -> Any:
        if getattr(self, _FFA_SKIP_BASE_SCORES_ATTR, False):
            return None
        return _original(self, *args, **kwargs)

    def patched_on_begin(
        self: Any,
        *args: Any,
        _original=original_on_begin,
        **kwargs: Any,
    ) -> Any:
        setattr(self, _FFA_SKIP_BASE_SCORES_ATTR, True)
        try:
            return _original(self, *args, **kwargs)
        finally:
            setattr(self, _FFA_SKIP_BASE_SCORES_ATTR, False)

    cls.show_player_scores = patched_show_player_scores
    cls.on_begin = patched_on_begin
    setattr(cls, _FFA_VICTORY_PATCHED_ATTR, True)


def _patch_score_display_with(classes: list[type[Any]]) -> None:
    for cls in classes:
        if getattr(cls, _SCORE_DISPLAY_PATCHED_ATTR, False):
            continue

        original = cls.__dict__.get('show_player_scores')
        if original is None:
            continue

        def patched(
            self: Any,
            *args: Any,
            _original=original,
            **kwargs: Any,
        ) -> Any:
            if getattr(self, _SCORE_DISPLAY_DONE_ATTR, False):
                return None
            setattr(self, _SCORE_DISPLAY_DONE_ATTR, True)
            return _original(self, *args, **kwargs)

        setattr(cls, 'show_player_scores', patched)
        setattr(cls, _SCORE_DISPLAY_PATCHED_ATTR, True)


def _patch_class(cls: type[Any]) -> bool:
    if cls.__dict__.get(_PATCHED_ATTR, False):
        return False

    original_on_begin = cls.__dict__.get('on_begin')
    if original_on_begin is None:
        return False

    def patched_on_begin(
        self: Any,
        _original=original_on_begin,
    ) -> None:
        _original(self)
        _start_atd_effects(self)

    setattr(cls, 'on_begin', patched_on_begin)
    setattr(cls, _PATCHED_ATTR, True)
    return True


_TWO_TEAM_PATCHED_ATTR = '_atd_two_team_layout_applied'
_TWO_TEAM_NAME_Y = 260
_TWO_TEAM_SCORE_Y = 190
_TWO_TEAM_X = 250


def _two_team_slot(pos_v: float, y: float) -> tuple[float, float]:
    return (-_TWO_TEAM_X, y) if pos_v == 65 else (_TWO_TEAM_X, y)


def _patch_two_team_layout() -> None:
    try:
        module = importlib.import_module('bascenev1lib.activity.dualteamscore')
        from bascenev1 import _classicassets
        from bascenev1lib.actor.zoomtext import ZoomText
    except Exception:
        logging.exception('ATD screens: two team layout is unavailable')
        return

    cls = getattr(module, 'TeamVictoryScoreScreenActivity', None)
    if cls is None or cls.__dict__.get(_TWO_TEAM_PATCHED_ATTR, False):
        return

    def on_begin(self: Any) -> None:
        bs.set_analytics_screen('Teams Score Screen')
        super(cls, self).on_begin()

        height = 130
        active_team_count = len(self.teams)
        vval = (height * active_team_count) / 2 - height / 2
        i = 0
        shift_time = 2.5

        session = self.session
        assert isinstance(session, bs.MultiTeamSession)
        if active_team_count != 2:
            ZoomText(
                _classicassets.strings.multi_team.best_of_series(
                    count=session.get_series_length()
                ),
                position=(0, 175),
                shiftposition=(-250, 175),
                shiftdelay=2.5,
                flash=False,
                trail=False,
                h_align='center',
                scale=0.25,
                color=(0.5, 0.5, 0.5, 1.0),
                jitter=3.0,
            ).autoretain()
        for team in self.session.sessionteams:
            bs.timer(
                i * 0.15 + 0.15,
                bs.WeakCallStrict(
                    self._show_team_name,
                    vval - i * height,
                    team,
                    i * 0.2,
                    shift_time - (i * 0.150 + 0.150),
                ),
            )
            bs.timer(i * 0.150 + 0.5, self._score_display_sound_small.play)
            scored = team is self._winner
            delay = 0.2
            if scored:
                delay = 1.2
                bs.timer(
                    i * 0.150 + 0.2,
                    bs.WeakCallStrict(
                        self._show_team_old_score,
                        vval - i * height,
                        team,
                        shift_time - (i * 0.15 + 0.2),
                    ),
                )
                bs.timer(i * 0.15 + 1.5, self._score_display_sound.play)

            bs.timer(
                i * 0.150 + delay,
                bs.WeakCallStrict(
                    self._show_team_score,
                    vval - i * height,
                    team,
                    scored,
                    i * 0.2 + 0.1,
                    shift_time - (i * 0.15 + delay),
                ),
            )
            i += 1
        self.show_player_scores()

    def _show_team_name(
        self: Any,
        pos_v: float,
        team: Any,
        kill_delay: float,
        shiftdelay: float,
    ) -> None:
        del kill_delay
        if len(self.teams) != 2:
            ZoomText(
                _classicassets.strings.multi_team.team_label(name=team.name),
                position=(100, pos_v),
                shiftposition=(-150, pos_v),
                shiftdelay=shiftdelay,
                flash=False,
                trail=False,
                h_align='right',
                maxwidth=300,
                color=team.color,
                jitter=1.0,
            ).autoretain()
            return
        slot = _two_team_slot(pos_v, _TWO_TEAM_NAME_Y)
        ZoomText(
            team.name,
            position=slot,
            shiftposition=slot,
            shiftdelay=shiftdelay,
            flash=False,
            trail=False,
            h_align='center',
            maxwidth=300,
            scale=0.45,
            color=team.color,
            jitter=1.0,
        ).autoretain()

    def _show_team_old_score(
        self: Any, pos_v: float, sessionteam: Any, shiftdelay: float
    ) -> None:
        if len(self.teams) != 2:
            ZoomText(
                str(sessionteam.customdata['score'] - 1),
                position=(150, pos_v),
                maxwidth=100,
                color=(0.6, 0.6, 0.7),
                shiftposition=(-100, pos_v),
                shiftdelay=shiftdelay,
                flash=False,
                trail=False,
                lifespan=1.0,
                h_align='left',
                jitter=1.0,
            ).autoretain()
            return
        slot = _two_team_slot(pos_v, _TWO_TEAM_SCORE_Y)
        ZoomText(
            str(sessionteam.customdata['score'] - 1),
            position=slot,
            maxwidth=100,
            color=(0.6, 0.6, 0.7),
            shiftposition=slot,
            shiftdelay=shiftdelay,
            flash=False,
            trail=False,
            lifespan=1.0,
            scale=0.56,
            h_align='center',
            jitter=1.0,
        ).autoretain()

    def _show_team_score(
        self: Any,
        pos_v: float,
        sessionteam: Any,
        scored: bool,
        kill_delay: float,
        shiftdelay: float,
    ) -> None:
        del kill_delay
        color = (1.0, 0.9, 0.5) if scored else (0.6, 0.6, 0.7)
        if len(self.teams) != 2:
            ZoomText(
                str(sessionteam.customdata['score']),
                position=(150, pos_v),
                maxwidth=100,
                color=color,
                shiftposition=(-100, pos_v),
                shiftdelay=shiftdelay,
                flash=scored,
                trail=scored,
                h_align='left',
                jitter=1.0,
                trailcolor=(1, 0.8, 0.0, 0),
            ).autoretain()
            return
        slot = _two_team_slot(pos_v, _TWO_TEAM_SCORE_Y)
        ZoomText(
            str(sessionteam.customdata['score']),
            position=slot,
            maxwidth=100,
            color=color,
            shiftposition=slot,
            shiftdelay=shiftdelay,
            flash=scored,
            trail=scored,
            scale=0.56,
            h_align='center',
            jitter=1.0,
            trailcolor=(1, 0.8, 0.0, 0),
        ).autoretain()

    cls.on_begin = on_begin
    cls._show_team_name = _show_team_name
    cls._show_team_old_score = _show_team_old_score
    cls._show_team_score = _show_team_score
    setattr(cls, _TWO_TEAM_PATCHED_ATTR, True)


def _patch_victory_screens() -> None:
    _patch_two_team_layout()
    all_classes = _collect_screen_classes()
    patched_classes = set(all_classes)

    _patch_background_actor()

    _patch_ffa_victory_with(all_classes)
    _patch_score_display_with(all_classes)

    _patch_score_activity_with(all_classes)
    _patch_coop_score()
    for cls in patched_classes:
        _patch_cleanup_methods(cls)
        _patch_class(cls)



def enable() -> None:
    try:
        _patch_victory_screens()
    except Exception:
        logging.exception('ATD screens: failed to patch score screens')
