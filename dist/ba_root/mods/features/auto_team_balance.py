# ba_meta require api 9
# Owner/Editor: Lucifer
"""Automatic post-match balancing for two-team API 9 sessions.

After each completed GameActivity, move enough players from the larger
SessionTeam to the smaller one to make the team sizes equal (or differ by one).
Players are moved between teams; nobody is kicked or sent to spectator.
"""

from __future__ import annotations

import logging
import random
from typing import Any

import babase
import bascenev1 as bs

_LOG = logging.getLogger("LuciferAutoTeamBalance")
_INSTALLED = False
_PROFILE_CACHE_HOOK_INSTALLED = False
_SESSION_HOOK_INSTALLED = False

# SessionPlayer is a native-backed object, so keep the last complete setdata
# arguments externally instead of attaching custom attrs to player instances.
# id(player) is pruned against the active session roster after every match.
_PLAYER_DATA_CACHE: dict[
    int,
    tuple[
        str,
        tuple[float, ...],
        tuple[float, ...],
        str | None,
        str | None,
        str | None,
    ],
] = {}


def _install_profile_cache_hook() -> bool:
    """Cache the complete profile payload after the lobby selects a player.

    API 9's Session._add_chosen_player() receives the chooser and uses its
    cloud-look/name JSON to populate SessionPlayer.setdata(). Capturing here
    avoids modifying the native-backed SessionPlayer class and preserves cloud
    characters/icons exactly when a player is moved between teams.
    """
    global _PROFILE_CACHE_HOOK_INSTALLED

    session_class = getattr(bs, "Session", None)
    if session_class is None:
        _LOG.error("API 9 Session class is unavailable; balancing skipped.")
        return False

    original_add_player = getattr(session_class, "_add_chosen_player", None)
    if not callable(original_add_player):
        _LOG.error("Session._add_chosen_player() is unavailable; balancing skipped.")
        return False

    if getattr(original_add_player, "_lucifer_atb_profile_cache_wrapped", False):
        _PROFILE_CACHE_HOOK_INSTALLED = True
        return True

    def wrapped_add_chosen_player(self: Any, chooser: Any) -> Any:
        player = original_add_player(self, chooser)
        try:
            cloud_look = chooser.get_cloud_look_json()
            _PLAYER_DATA_CACHE[id(player)] = (
                str(chooser.get_character_name()),
                tuple(float(value) for value in chooser.get_color()),
                tuple(float(value) for value in chooser.get_highlight()),
                None if cloud_look is None else cloud_look[0],
                None if cloud_look is None else cloud_look[1],
                chooser.get_name_depiction_json(),
            )
        except Exception:
            # The player still joined normally; this only means the plugin
            # cannot safely reassign this player's team later.
            _LOG.exception(
                "Could not cache profile data for a newly selected player; "
                "that player will be left in place during team balancing."
            )
        return player

    wrapped_add_chosen_player._lucifer_atb_profile_cache_wrapped = True  # type: ignore[attr-defined]
    try:
        session_class._add_chosen_player = wrapped_add_chosen_player
    except Exception:
        _LOG.exception("Could not install the Session profile-cache hook.")
        return False

    _PROFILE_CACHE_HOOK_INSTALLED = True
    _LOG.info("Installed Session profile cache for safe team reassignment.")
    return True


def _update_name_depiction(
    depiction_json: str | None, team_color: tuple[float, ...]
) -> str | None:
    """Update a saved name depiction's team color while preserving its styling."""
    if not depiction_json:
        return depiction_json
    try:
        import bacommon.depiction as bdep
        from efro.dataclassio import dataclass_from_json, dataclass_to_json

        depiction = dataclass_from_json(bdep.NameDepiction, depiction_json)
        depiction.color_override = tuple(babase.safecolor(team_color)[:3])
        depiction.team_coloring = True
        return dataclass_to_json(depiction)
    except Exception:
        # The original JSON remains valid; a stale color override is cosmetic
        # and preferable to losing the player's name depiction entirely.
        _LOG.debug("Could not refresh the moved player's name depiction color.", exc_info=True)
        return depiction_json


def _live_session_player_ids(session: Any) -> set[int]:
    """Return live player identities and prune cached profiles for departed players."""
    live_ids: set[int] = set()
    for player in tuple(getattr(session, "sessionplayers", ())):
        try:
            if player:
                live_ids.add(id(player))
        except Exception:
            continue

    for player_id in tuple(_PLAYER_DATA_CACHE):
        if player_id not in live_ids:
            _PLAYER_DATA_CACHE.pop(player_id, None)
    return live_ids


def _rebalance_two_teams(session: Any) -> int:
    """Move players from the larger of exactly two teams into the smaller team."""
    if not getattr(session, "use_teams", False):
        return 0

    teams = list(getattr(session, "sessionteams", ()))
    if len(teams) != 2:
        return 0

    live_ids = _live_session_player_ids(session)

    team_players: list[list[Any]] = []
    for team in teams:
        members = [player for player in tuple(team.players) if id(player) in live_ids]
        team_players.append(members)

    counts = [len(members) for members in team_players]
    difference = abs(counts[0] - counts[1])
    if difference <= 1:
        _LOG.info(
            "Team balance check: %s=%d, %s=%d; already balanced.",
            teams[0].name,
            counts[0],
            teams[1].name,
            counts[1],
        )
        return 0

    larger_index = 0 if counts[0] > counts[1] else 1
    smaller_index = 1 - larger_index
    source_team = teams[larger_index]
    target_team = teams[smaller_index]
    source_members = team_players[larger_index]

    # A player is movable only if we have their exact setdata payload. This
    # ensures custom cloud characters/icons and name depictions survive moves.
    movable = [
        player for player in source_members if id(player) in _PLAYER_DATA_CACHE
    ]
    desired_moves = difference // 2
    if not movable:
        _LOG.warning(
            "Teams are imbalanced (%d vs %d), but no source-team player has a "
            "cached profile payload yet; no one was moved.",
            counts[larger_index],
            counts[smaller_index],
        )
        return 0

    selected = random.sample(movable, min(desired_moves, len(movable)))
    use_team_colors = bool(getattr(session, "use_team_colors", True))
    moved = 0

    for player in selected:
        data = _PLAYER_DATA_CACHE.get(id(player))
        if data is None:
            continue
        (
            character,
            old_color,
            highlight,
            cloud_spaz_json,
            cloud_icon_json,
            name_json,
        ) = data
        new_color = (
            tuple(float(value) for value in target_team.color)
            if use_team_colors
            else old_color
        )
        new_name_json = (
            _update_name_depiction(name_json, tuple(target_team.color))
            if use_team_colors
            else name_json
        )

        # The session's own team membership and the native SessionPlayer data
        # must agree. Roll the Python lists back if setdata fails.
        try:
            source_team.players.remove(player)
            target_team.players.append(player)
            try:
                player.setdata(
                    team=target_team,
                    character=character,
                    color=new_color,
                    highlight=highlight,
                    cloud_spaz_json=cloud_spaz_json,
                    cloud_icon_json=cloud_icon_json,
                    name_depiction_json=new_name_json,
                )
            except Exception:
                if player in target_team.players:
                    target_team.players.remove(player)
                if player not in source_team.players:
                    source_team.players.append(player)
                raise
        except Exception:
            _LOG.exception(
                "Could not move a player from team %s to %s; "
            "continuing with other candidates.",
                source_team.name,
                target_team.name,
            )
            continue

        _PLAYER_DATA_CACHE[id(player)] = (
            character,
            tuple(float(value) for value in new_color),
            highlight,
            cloud_spaz_json,
            cloud_icon_json,
            new_name_json,
        )
        moved += 1
        try:
            player_name = player.getname(full=False, icon=False)
        except Exception:
            player_name = "a player"
        _LOG.info(
            "Moved %s from %s to %s for team balance.",
            player_name,
            source_team.name,
            target_team.name,
        )

    _LOG.info(
        "Team balance complete: moved %d player(s); %s=%d, %s=%d.",
        moved,
        source_team.name,
        len(source_team.players),
        target_team.name,
        len(target_team.players),
    )
    return moved


def _install_session_end_hook() -> bool:
    """Balance after the score-screen path, before the next game begins.

    The match-end callback only marks a balance as pending. The team move is
    delayed until Session.begin_next_activity() is about to start the next
    GameActivity, so the just-finished match's score-screen still sees the
    teams that actually played that match.
    """
    global _SESSION_HOOK_INSTALLED

    session_class = getattr(bs, "Session", None)
    game_activity_class = getattr(bs, "GameActivity", None)
    if session_class is None or game_activity_class is None:
        _LOG.error("API 9 Session/GameActivity class is unavailable; balancing skipped.")
        return False

    original_complete_end = getattr(session_class, "_complete_end_activity", None)
    original_begin_next = getattr(session_class, "begin_next_activity", None)
    if not callable(original_complete_end) or not callable(original_begin_next):
        _LOG.error(
            "Session lifecycle methods are unavailable; automatic balancing skipped."
        )
        return False

    if not getattr(original_complete_end, "_lucifer_atb_end_wrapped", False):
        def wrapped_complete_end_activity(
            self: Any, activity: Any, results: Any
        ) -> Any:
            if isinstance(activity, game_activity_class) and results is not None:
                outcome = (
                    str(results.get("outcome", "")).casefold()
                    if isinstance(results, dict)
                    else ""
                )
                if outcome not in {"restart", "quit", "abort"} and not getattr(
                    activity, "_lucifer_auto_team_balance_done", False
                ):
                    activity._lucifer_auto_team_balance_done = True
                    # The session may show a score screen first; defer actual
                    # changes until the next GameActivity is about to begin.
                    self._lucifer_atb_pending_team_balance = True
            return original_complete_end(self, activity, results)

        wrapped_complete_end_activity._lucifer_atb_end_wrapped = True  # type: ignore[attr-defined]
        try:
            session_class._complete_end_activity = wrapped_complete_end_activity
        except Exception:
            _LOG.exception("Could not install the Session match-end hook.")
            return False

    if not getattr(original_begin_next, "_lucifer_atb_begin_wrapped", False):
        def wrapped_begin_next_activity(self: Any) -> Any:
            next_activity = getattr(self, "_next_activity", None)
            if (
                getattr(self, "_lucifer_atb_pending_team_balance", False)
                and isinstance(next_activity, game_activity_class)
            ):
                # Clear first so even an exception cannot cause repeated moves.
                self._lucifer_atb_pending_team_balance = False
                try:
                    with self.context:
                        _rebalance_two_teams(self)
                except Exception:
                    _LOG.exception(
                        "Automatic team balancing failed before the next game began."
                    )
            return original_begin_next(self)

        wrapped_begin_next_activity._lucifer_atb_begin_wrapped = True  # type: ignore[attr-defined]
        try:
            session_class.begin_next_activity = wrapped_begin_next_activity
        except Exception:
            _LOG.exception("Could not install the Session next-activity hook.")
            return False

    _SESSION_HOOK_INSTALLED = True
    _LOG.info(
        "Installed automatic team balancing between matches, before the next game begins."
    )
    return True


def install() -> bool:
    """Install safe profile caching and post-match team-balancing hooks."""
    global _INSTALLED
    if _INSTALLED:
        return True

    profile_ok = _install_profile_cache_hook()
    session_ok = _install_session_end_hook()
    _INSTALLED = profile_ok and session_ok
    if _INSTALLED:
        _LOG.info("Auto Team Balance is enabled.")
    else:
        _LOG.error(
            "Auto Team Balance was not fully installed "
            "(profile_cache=%s, session_hook=%s).",
            profile_ok,
            session_ok,
        )
    return _INSTALLED
