# ba_meta require api 9
# Owner/Editor: Lucifer
"""Optimized API 9 hooks for tags, chat, stats, rewards, bans, and ATD menu/screens."""

from __future__ import annotations

import logging
import time
from typing import Any

import babase
import bascenev1 as bs

_LOG = logging.getLogger("RoleTags")
_SPAZ_INSTALLED = False
_CHARACTER_CHOOSER_INSTALLED = False
_STATS_HOOK_INSTALLED = False
_BAN_MONITOR_STARTED = False
_PENDING_BAN_DISCONNECTS: dict[int, tuple[str, float]] = {}
_BAN_MONITOR_LAST_ERROR_LOG = 0.0
_LAST_RECENT_ROSTER_SIGNATURE: tuple[tuple[str, ...], ...] | None = None
_BAN_CHECK_CACHE: dict[str, tuple[float, bool]] = {}
_BAN_MONITOR_INTERVAL = 1.0
_BAN_CACHE_TTL = 2.0
_SOLO_BOT_HOOKS_INSTALLED = False
_SETTINGS_CACHE: dict[str, Any] | None = None
_DISCORD_BOT_HOOK_STARTED = False
_MAP_TINT_HOOK_INSTALLED = False
_FLYING_MINE_INSTALLED = False


def _read_mod_settings() -> dict[str, Any]:
    """Read setting.json once per process; optional mods can cheaply check flags."""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE
    try:
        import json
        from pathlib import Path

        path = Path(__file__).with_name("setting.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("setting.json root must be a JSON object")
        _SETTINGS_CACHE = data
    except Exception:
        _LOG.exception("Could not read setting.json; optional features use safe defaults.")
        _SETTINGS_CACHE = {}
    return _SETTINGS_CACHE


def _setting_enabled(section_or_key: str, key: str | None = None, default: bool = True) -> bool:
    """Return a boolean setting from a top-level key or nested section."""
    settings = _read_mod_settings()
    if key is None:
        value = settings.get(section_or_key, default)
    else:
        section = settings.get(section_or_key, {})
        value = section.get(key, default) if isinstance(section, dict) else default
    return value if isinstance(value, bool) else default


def _install_account_info_capture() -> None:
    """Capture public account tag and creation time from accepted V2 auth."""
    try:
        classic = getattr(babase.app, "classic", None)
        if classic is None:
            _LOG.warning("Classic subsystem is unavailable; account info capture skipped.")
            return
        original_handler = getattr(classic, "v2_auth_handler", None)
        if not callable(original_handler):
            _LOG.warning("V2 auth handler is unavailable; account info capture skipped.")
            return
        if getattr(original_handler, "_lucifer_account_info_capture", False):
            return

        async def captured_v2_auth_handler(request: Any) -> Any:
            # Preserve the server's existing accept/reject policy and response.
            response = await original_handler(request)
            if getattr(response, "allow", False):
                try:
                    from playersdata import account_info

                    account_info.record_account_info(
                        getattr(request, "account_id", None),
                        getattr(request, "account_tag", None),
                        getattr(request, "account_create_time", None),
                    )
                except Exception:
                    _LOG.exception("Could not record V2 account creation details.")
            return response

        captured_v2_auth_handler._lucifer_account_info_capture = True  # type: ignore[attr-defined]
        classic.v2_auth_handler = captured_v2_auth_handler
        _LOG.info("Installed V2 account tag/creation-time capture.")
    except Exception:
        _LOG.exception("Could not install V2 account info capture.")


def _install_stats_hook() -> None:
    """Load persistent player stats and record one update per completed game."""
    global _STATS_HOOK_INSTALLED

    try:
        from stats import mystats

        # Initialize explicitly when supported; importing alone may not load the data.
        initializer = getattr(mystats, "initialise", None)
        if callable(initializer):
            initializer()
    except Exception as exc:
        _LOG.exception("[Lucifer Stats] Could not load/initialise stats.mystats; stats hook not installed.")
        return

    try:
        # Hook Activity.end instead of only GameActivity.end so games that
        # inherit the normal Activity.end implementation are covered uniformly.
        activity_class = bs.Activity
        original_end = activity_class.end
        if getattr(original_end, "_lucifer_stats_wrapped", False):
            _STATS_HOOK_INSTALLED = True
            _LOG.info("[Lucifer Stats] Activity end hook was already installed.")
            return

        def wrapped_activity_end(self: Any, *args: Any, **kwargs: Any) -> Any:
            """Capture game stats before a GameActivity begins expiring."""
            results = kwargs.get("results", args[0] if args else None)
            is_game = isinstance(self, bs.GameActivity)
            is_restart = (
                isinstance(results, dict)
                and str(results.get("outcome", "")).casefold() in {"restart", "quit", "abort"}
            )
            if (
                is_game
                and results is not None
                and not is_restart
                and not getattr(self, "_lucifer_stats_recorded", False)
            ):
                try:
                    # Mark before queuing the update so repeated/forced end() calls
                    # cannot count this activity more than once.
                    self._lucifer_stats_recorded = True
                    score_set = self.stats
                    get_records = getattr(score_set, "get_records", None)
                    if not callable(get_records):
                        _LOG.warning(
                            "[Lucifer Stats] Game ended, but its stats object has no get_records()."
                        )
                    else:
                        record_count = len(get_records())
                        if record_count:
                            mystats.update(score_set)
                            _LOG.info(
                                "[Lucifer Stats] Submitted completed-game stats for %d player record(s).",
                                record_count,
                            )
                        else:
                            _LOG.info(
                                "[Lucifer Stats] Game ended without player records; nothing to save."
                            )
                except Exception:
                    _LOG.exception(
                        "[Lucifer Stats] Could not collect completed-game stats."
                    )
            return original_end(self, *args, **kwargs)

        wrapped_activity_end._lucifer_stats_wrapped = True  # type: ignore[attr-defined]
        activity_class.end = wrapped_activity_end  # type: ignore[method-assign]
        _STATS_HOOK_INSTALLED = True
        _LOG.info("[Lucifer Stats] Installed Activity end hook for completed-game statistics.")
    except Exception as exc:
        _LOG.exception("[Lucifer Stats] Could not install the Activity end hook.")



def _install_coin_reward_hook() -> None:
    """Award persistent coins to the winning FFA player/team using API 9 results."""
    try:
        activity_class = bs.Activity
        original_end = activity_class.end
        if getattr(original_end, "_lucifer_coins_wrapped", False):
            return

        def wrapped_activity_end(self: Any, *args: Any, **kwargs: Any) -> Any:
            results = kwargs.get("results", args[0] if args else None)
            if (
                isinstance(self, bs.GameActivity)
                and results is not None
                and not getattr(self, "_lucifer_coins_reward_checked", False)
            ):
                # An intentional abort/restart/quit is never a completed win.
                if isinstance(results, dict):
                    outcome = str(results.get("outcome", "")).casefold()
                    if outcome in {"restart", "quit", "abort"}:
                        self._lucifer_coins_reward_checked = True
                    else:
                        # Non-GameResults dicts do not define a safe winner.
                        self._lucifer_coins_reward_checked = True
                else:
                    self._lucifer_coins_reward_checked = True
                    try:
                        winner_groups = list(getattr(results, "winnergroups", ()))
                        # API 9 GameResults uses fewer than two score-groups for a draw.
                        if len(winner_groups) >= 2 and winner_groups[0].teams:
                            winning_session_teams = set(winner_groups[0].teams)
                            session = getattr(self, "session", None)
                            team_mode = bool(getattr(session, "use_teams", False))
                            import setting
                            from shop import coins
                            from tools.account_ids import get_v2_account_id, is_v2_account_id
                            from chathandle.chatcommands import handlers

                            economy = setting.get_section("economy")
                            if economy.get("enable", True):
                                setting_key = "teamWinReward" if team_mode else "ffaWinReward"
                                reward = economy.get(setting_key, 50)
                                if isinstance(reward, bool) or not isinstance(reward, int) or reward < 0:
                                    reward = 0
                                if reward > 0:
                                    awarded: set[str] = set()
                                    for player in list(getattr(self, "players", ())):
                                        try:
                                            team = getattr(player, "team", None)
                                            session_team = getattr(team, "sessionteam", None)
                                            if session_team not in winning_session_teams:
                                                continue
                                            session_player = getattr(player, "sessionplayer", None)
                                            account_id = get_v2_account_id(session_player)
                                            if not is_v2_account_id(account_id) or account_id in awarded:
                                                continue
                                            name = "Player"
                                            getter = getattr(player, "getname", None)
                                            if callable(getter):
                                                try:
                                                    candidate = getter(full=False, icon=False)
                                                except TypeError:
                                                    candidate = getter()
                                                if isinstance(candidate, str) and candidate.strip():
                                                    name = " ".join(candidate.split())[:64]
                                            balance = coins.add_coins(account_id, reward, name)
                                            awarded.add(account_id)
                                            try:
                                                client_id = int(session_player.inputdevice.client_id)
                                                handlers.send(
                                                    f"\ue04c  MATCH REWARD  \ue04d\n"
                                                    f"\ue047  +{reward:,} COINS\n"
                                                    f"\ue048  BALANCE: {balance:,}  \ue043",
                                                    client_id,
                                                    color=(1.0, 0.78, 0.25),
                                                )
                                            except Exception:
                                                _LOG.debug("Could not send match reward message.", exc_info=True)
                                        except Exception:
                                            _LOG.exception("Could not award match coins to a winning player.")
                                    if awarded:
                                        _LOG.info(
                                            "Paid %s coin winner reward to %d verified V2 account(s).",
                                            reward,
                                            len(awarded),
                                        )
                    except Exception:
                        _LOG.exception("Could not resolve or award completed-game coin rewards.")
            return original_end(self, *args, **kwargs)

        wrapped_activity_end._lucifer_coins_wrapped = True  # type: ignore[attr-defined]
        # The wrapper contains the pre-existing stats wrapper when it is present;
        # marking it avoids stacking another stats wrapper if install() is called again.
        wrapped_activity_end._lucifer_stats_wrapped = bool(  # type: ignore[attr-defined]
            getattr(original_end, "_lucifer_stats_wrapped", False)
        )
        activity_class.end = wrapped_activity_end  # type: ignore[method-assign]
        _LOG.info("Installed API 9 winner reward hook for FFA and team games.")
    except Exception:
        _LOG.exception("Could not install the coin winner-reward hook.")

def _roster_signature(roster: Any) -> tuple[tuple[str, ...], ...]:
    """Build a stable signature so /recents is not processed on every poll."""
    rows: list[tuple[str, ...]] = []
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        rows.append((
            str(entry.get("client_id", "")),
            str(entry.get("account_id", "")),
            str(entry.get("display_string", "")),
            str(entry.get("spec_string", "")),
            str(entry.get("name", "")),
        ))
    return tuple(sorted(rows))


def _check_banned_roster() -> None:
    """Disconnect connected players with active V2 bans using a short-lived result cache."""
    global _BAN_MONITOR_LAST_ERROR_LOG, _BAN_MONITOR_STARTED
    global _LAST_RECENT_ROSTER_SIGNATURE

    try:
        from playersdata import pdata
        from tools.account_ids import is_v2_account_id

        roster = bs.get_game_roster()
        signature = _roster_signature(roster)

        # A roster change is the only event that can affect /recents membership.
        if signature != _LAST_RECENT_ROSTER_SIGNATURE:
            try:
                from playersdata import recent_players
                recent_players.record_roster(roster)
                _LAST_RECENT_ROSTER_SIGNATURE = signature
            except Exception:
                _LOG.exception("Could not update recent-player history.")

        connected: dict[int, str] = {}
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            try:
                client_id = int(entry.get("client_id"))
            except (TypeError, ValueError, OverflowError):
                continue
            if client_id < 0:
                continue
            raw_account_id = entry.get("account_id")
            account_id = raw_account_id if is_v2_account_id(raw_account_id) else None
            if account_id is not None:
                connected[client_id] = account_id

        # Clear pending entries if a client leaves or its client ID is reused.
        for client_id, pending in tuple(_PENDING_BAN_DISCONNECTS.items()):
            if connected.get(client_id) != pending[0]:
                _PENDING_BAN_DISCONNECTS.pop(client_id, None)

        now = time.monotonic()
        connected_accounts = set(connected.values())
        for account_id in tuple(_BAN_CHECK_CACHE):
            if account_id not in connected_accounts:
                _BAN_CHECK_CACHE.pop(account_id, None)

        for client_id, account_id in connected.items():
            try:
                # Keep the owner protected even if a ban record was edited manually.
                if pdata.is_owner_account_id(account_id):
                    _BAN_CHECK_CACHE.pop(account_id, None)
                    continue
                cached = _BAN_CHECK_CACHE.get(account_id)
                if cached is not None and now - cached[0] < _BAN_CACHE_TTL:
                    is_banned = cached[1]
                else:
                    is_banned = bool(pdata.get_active_ban(account_id))
                    _BAN_CHECK_CACHE[account_id] = (now, is_banned)
            except Exception:
                now = time.monotonic()
                if now - _BAN_MONITOR_LAST_ERROR_LOG >= 30.0:
                    _BAN_MONITOR_LAST_ERROR_LOG = now
                    _LOG.exception("Ban check failed. Verify playersdata/banned.json and roles.json.")
                continue

            if not is_banned:
                continue
            pending = _PENDING_BAN_DISCONNECTS.get(client_id)
            if pending is not None and pending[0] == account_id and now - pending[1] < 5.0:
                continue
            try:
                bs.disconnect_client(client_id)
                _PENDING_BAN_DISCONNECTS[client_id] = (account_id, now)
                _LOG.warning(
                    "Disconnected client %s because V2 account %s has an active ban.",
                    client_id, account_id,
                )
            except Exception:
                _LOG.exception(
                    "Could not disconnect banned client %s (V2 account %s); will retry.",
                    client_id, account_id,
                )
    except Exception:
        now = time.monotonic()
        if now - _BAN_MONITOR_LAST_ERROR_LOG >= 30.0:
            _BAN_MONITOR_LAST_ERROR_LOG = now
            _LOG.exception("Ban-monitor roster scan failed; join enforcement will retry.")
    finally:
        # App timers are one-shot and return None, so schedule the next scan explicitly.
        if _BAN_MONITOR_STARTED:
            try:
                babase.apptimer(_BAN_MONITOR_INTERVAL, _check_banned_roster)
            except Exception:
                _BAN_MONITOR_STARTED = False
                _LOG.exception("Could not schedule the next ban-monitor roster scan.")


def _install_ban_monitor() -> None:
    """Start periodic roster verification so saved bans persist across restarts."""
    global _BAN_MONITOR_STARTED
    if _BAN_MONITOR_STARTED:
        return
    _BAN_MONITOR_STARTED = True
    _check_banned_roster()
    if _BAN_MONITOR_STARTED:
        _LOG.info(
            "Persistent V2 ban monitor started (%.1f-second scans; %.1f-second ban cache).",
            _BAN_MONITOR_INTERVAL, _BAN_CACHE_TTL,
        )
    else:
        _LOG.error("Persistent V2 ban monitor could not be scheduled.")


def _install_character_chooser() -> None:
    """Enable profile/character selection in the lobby on API 9."""
    global _CHARACTER_CHOOSER_INSTALLED
    if _CHARACTER_CHOOSER_INSTALLED:
        return
    try:
        # This plugin lives in mods/plugins/, not mods/features/.
        from plugins import character_chooser

        if character_chooser.install():
            _CHARACTER_CHOOSER_INSTALLED = True
            _LOG.info("Installed lobby profile/character chooser.")
        else:
            _LOG.error("Lobby profile/character chooser was not installed.")
    except Exception:
        # Do not allow this optional lobby patch to disable other server hooks.
        _LOG.exception("Could not install the lobby profile/character chooser.")



def _apply_map_tint(activity: Any) -> None:
    """Apply the configured global tint after each game activity begins."""
    game_activity_class = getattr(bs, "GameActivity", None)
    if game_activity_class is None or not isinstance(activity, game_activity_class):
        return

    config = _read_mod_settings().get("mapTint", {})
    if not isinstance(config, dict) or config.get("enable", False) is not True:
        return

    color = config.get("color", [1.0, 1.0, 1.0])
    if (
        not isinstance(color, (list, tuple))
        or len(color) != 3
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= value <= 1.0
            for value in color
        )
    ):
        _LOG.error("Invalid mapTint.color in setting.json; map tint was not applied.")
        return

    try:
        # globalsnode.tint is the API 9 global screen/world tint value.
        # Apply after Activity.begin() has run so per-game map setup is complete.
        with activity.context:
            activity.globalsnode.tint = tuple(float(value) for value in color)
        _LOG.info("Applied map tint RGB=%s.", tuple(float(value) for value in color))
    except Exception:
        # Keep an optional cosmetic feature from breaking a game start.
        _LOG.exception("Could not apply configured map tint.")


def _install_map_tint() -> None:
    """Apply setting.json mapTint to every GameActivity without editing maps."""
    global _MAP_TINT_HOOK_INSTALLED
    if _MAP_TINT_HOOK_INSTALLED:
        return

    config = _read_mod_settings().get("mapTint", {})
    if not isinstance(config, dict) or config.get("enable", False) is not True:
        _LOG.info("Map tint skipped because mapTint.enable is false or missing.")
        return

    activity_class = getattr(bs, "Activity", None)
    original_transition_in = (
        getattr(activity_class, "transition_in", None) if activity_class else None
    )
    if not callable(original_transition_in):
        _LOG.error(
            "Map tint hook skipped: bascenev1.Activity.transition_in() is unavailable."
        )
        return
    if getattr(original_transition_in, "_lucifer_map_tint_wrapped", False):
        _MAP_TINT_HOOK_INSTALLED = True
        return

    def wrapped_activity_transition_in(
        self: Any, *args: Any, **kwargs: Any
    ) -> Any:
        # The original transition creates the activity globals node. Apply tint
        # afterward so the node definitely exists for this map/activity.
        result = original_transition_in(self, *args, **kwargs)
        _apply_map_tint(self)
        return result

    wrapped_activity_transition_in._lucifer_map_tint_wrapped = True  # type: ignore[attr-defined]
    activity_class.transition_in = wrapped_activity_transition_in  # type: ignore[method-assign]
    _MAP_TINT_HOOK_INSTALLED = True
    _LOG.info("Installed persistent map tint hook for all GameActivity instances.")

def _install_text_on_map() -> None:
    """Install the switchable TextOnMap system from mods/textonmap/."""
    try:
        from textonmap import manager

        if manager.install():
            _LOG.info("Installed TextOnMap manager.")
        else:
            _LOG.error("TextOnMap manager was not installed.")
    except Exception:
        _LOG.exception("Could not install TextOnMap manager.")


def _install_colorful_explosions() -> None:
    """Install the optional Holi-color floor splashes after bomb explosions."""
    if not _setting_enabled("colorful_explosions", "enable", True):
        _LOG.info("Colorful explosions skipped because colorful_explosions.enable is false.")
        return
    try:
        # This plugin lives in mods/plugins/, not mods/features/.
        from plugins import colorful_explosion

        if colorful_explosion.install():
            _LOG.info("Installed colorful Holi explosion patches.")
        else:
            _LOG.warning("Colorful explosion patches were not installed.")
    except Exception:
        # Visual extras must never prevent gameplay, chat, or player hooks.
        _LOG.exception("Could not install colorful explosion patches.")


def _install_snowfall() -> None:
    """Install optional snowfall controlled by mods/setting.json."""
    if not _setting_enabled("snowfall", "enable", False):
        _LOG.info("Snowfall skipped because snowfall.enable is false.")
        return
    try:
        from features import snowfall

        if snowfall.install():
            _LOG.info("Installed snowfall feature (enabled=%s).", snowfall.is_enabled())
        else:
            _LOG.warning("Snowfall lifecycle hooks were not installed.")
    except Exception:
        # The visual effect must never interfere with gameplay or existing hooks.
        _LOG.exception("Could not install the snowfall feature.")




def _install_auto_team_balance() -> None:
    """Install the automatic post-match two-team balancing feature."""
    if not _setting_enabled("autoTeamBalance", "enable", True):
        _LOG.info("Auto Team Balance skipped because autoTeamBalance.enable is false.")
        return
    try:
        from features import auto_team_balance

        if auto_team_balance.install():
            _LOG.info("Installed automatic post-match team balancing.")
        else:
            _LOG.error("Auto Team Balance hooks were not fully installed.")
    except Exception:
        # An optional gameplay adjustment must never disable core server hooks.
        _LOG.exception("Could not install Auto Team Balance.")


def _install_powerup_manager() -> None:
    """Install powerup quantity controls and display labels."""
    try:
        from powerups import install as install_powerup_manager

        manager = install_powerup_manager()
        _LOG.info(
            "[Lucifer Powerups] Manager installed (enabled=%s).",
            getattr(manager, "enabled", True),
        )
    except Exception:
        # A powerup error must not disable other server hooks.
        _LOG.exception(
            "[Lucifer Powerups] Could not install the Powerup Manager."
        )


def _schedule_solo_bot_check(activity: Any, delay: float = 0.8) -> None:
    """Ask the optional Solo Bot feature to reconcile the current game."""
    try:
        game_activity_class = getattr(bs, "GameActivity", None)
        if game_activity_class is None or not isinstance(activity, game_activity_class):
            return
        from features import solo_bot

        schedule = getattr(solo_bot, "schedule_reconcile", None)
        if callable(schedule):
            schedule(activity, delay=delay)
    except ModuleNotFoundError as exc:
        if exc.name == "features.solo_bot" or exc.name == "features":
            _LOG.warning(
                "Solo Bot is not installed; add features/solo_bot.py to enable it."
            )
        else:
            _LOG.exception("Solo Bot could not import a required dependency.")
    except Exception:
        # This feature is optional and must not interrupt normal activity flow.
        _LOG.exception("Could not schedule the Lucifer Solo Bot check.")


def _cleanup_solo_bot(activity: Any) -> None:
    """Clean up a bot owned by an activity, if the feature is installed."""
    try:
        from features import solo_bot

        cleanup = getattr(solo_bot, "cleanup_activity", None)
        if callable(cleanup):
            cleanup(activity)
    except ModuleNotFoundError as exc:
        if exc.name not in {"features.solo_bot", "features"}:
            _LOG.exception("Solo Bot cleanup could not import a dependency.")
    except Exception:
        # Never let optional cleanup block end/transition of a game activity.
        _LOG.exception("Could not clean up the Lucifer Solo Bot.")


def _install_solo_bot_hooks() -> None:
    """Wire Solo Bot lifecycle hooks only when the feature is enabled."""
    global _SOLO_BOT_HOOKS_INSTALLED
    if _SOLO_BOT_HOOKS_INSTALLED:
        return
    if not _setting_enabled("soloBot", "enable", False):
        _LOG.info("Solo Bot lifecycle hooks skipped because soloBot.enable is false.")
        return

    try:
        from features import solo_bot

        if not callable(getattr(solo_bot, "schedule_reconcile", None)) or not callable(
            getattr(solo_bot, "cleanup_activity", None)
        ):
            _LOG.error(
                "features.solo_bot is present but does not expose the required functions."
            )
            return
    except ModuleNotFoundError as exc:
        if exc.name == "features.solo_bot" or exc.name == "features":
            _LOG.warning(
                "Solo Bot hooks skipped: install the module at features/solo_bot.py."
            )
        else:
            _LOG.exception("Solo Bot hooks could not import a required dependency.")
        return
    except Exception:
        _LOG.exception("Could not load features.solo_bot; its hooks were not installed.")
        return

    activity_class = getattr(bs, "Activity", None)
    game_activity_class = getattr(bs, "GameActivity", None)
    if activity_class is None or game_activity_class is None:
        _LOG.error("API 9 Activity/GameActivity classes are unavailable; Solo Bot skipped.")
        return

    installed_any = False

    # Activity.begin() is the common lifecycle entry point: it adds the initial
    # teams/players and calls on_begin(). Schedule only after that process ends.
    original_begin = getattr(activity_class, "begin", None)
    if callable(original_begin):
        if not getattr(original_begin, "_lucifer_solo_bot_begin_wrapped", False):
            def wrapped_activity_begin(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = original_begin(self, *args, **kwargs)
                _schedule_solo_bot_check(self, delay=0.8)
                return result

            wrapped_activity_begin._lucifer_solo_bot_begin_wrapped = True  # type: ignore[attr-defined]
            activity_class.begin = wrapped_activity_begin  # type: ignore[method-assign]
            installed_any = True
        else:
            installed_any = True
    else:
        _LOG.warning("Activity.begin() is unavailable; initial Solo Bot check may be skipped.")

    # These internal Activity methods catch joins/leaves even when a game class
    # overrides on_player_join/on_player_leave without calling super().
    original_add_player = getattr(activity_class, "add_player", None)
    if callable(original_add_player):
        if not getattr(original_add_player, "_lucifer_solo_bot_add_wrapped", False):
            def wrapped_add_player(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = original_add_player(self, *args, **kwargs)
                try:
                    if isinstance(self, game_activity_class) and self.has_begun():
                        _schedule_solo_bot_check(self, delay=0.5)
                except Exception:
                    _LOG.exception("Could not reconcile Solo Bot after a player joined.")
                return result

            wrapped_add_player._lucifer_solo_bot_add_wrapped = True  # type: ignore[attr-defined]
            activity_class.add_player = wrapped_add_player  # type: ignore[method-assign]
            installed_any = True
        else:
            installed_any = True
    else:
        _LOG.warning("Activity.add_player() is unavailable; join-triggered Solo Bot checks skipped.")

    original_remove_player = getattr(activity_class, "remove_player", None)
    if callable(original_remove_player):
        if not getattr(original_remove_player, "_lucifer_solo_bot_remove_wrapped", False):
            def wrapped_remove_player(self: Any, *args: Any, **kwargs: Any) -> Any:
                result = original_remove_player(self, *args, **kwargs)
                try:
                    if isinstance(self, game_activity_class) and not self.has_ended():
                        _schedule_solo_bot_check(self, delay=0.4)
                except Exception:
                    _LOG.exception("Could not reconcile Solo Bot after a player left.")
                return result

            wrapped_remove_player._lucifer_solo_bot_remove_wrapped = True  # type: ignore[attr-defined]
            activity_class.remove_player = wrapped_remove_player  # type: ignore[method-assign]
            installed_any = True
        else:
            installed_any = True
    else:
        _LOG.warning("Activity.remove_player() is unavailable; leave-triggered Solo Bot checks skipped.")

    # Clean up before both explicit end() and transitions away from a game.
    original_end = getattr(activity_class, "end", None)
    if callable(original_end):
        if not getattr(original_end, "_lucifer_solo_bot_end_wrapped", False):
            def wrapped_activity_end(self: Any, *args: Any, **kwargs: Any) -> Any:
                _cleanup_solo_bot(self)
                return original_end(self, *args, **kwargs)

            wrapped_activity_end._lucifer_solo_bot_end_wrapped = True  # type: ignore[attr-defined]
            # Preserve markers used by the already-installed stats/coin hooks;
            # otherwise a second install() call could stack them again.
            for marker in ("_lucifer_stats_wrapped", "_lucifer_coins_wrapped"):
                setattr(wrapped_activity_end, marker, bool(getattr(original_end, marker, False)))
            activity_class.end = wrapped_activity_end  # type: ignore[method-assign]
            installed_any = True
        else:
            installed_any = True

    original_transition_out = getattr(activity_class, "transition_out", None)
    if callable(original_transition_out):
        if not getattr(original_transition_out, "_lucifer_solo_bot_transition_wrapped", False):
            def wrapped_transition_out(self: Any, *args: Any, **kwargs: Any) -> Any:
                _cleanup_solo_bot(self)
                return original_transition_out(self, *args, **kwargs)

            wrapped_transition_out._lucifer_solo_bot_transition_wrapped = True  # type: ignore[attr-defined]
            activity_class.transition_out = wrapped_transition_out  # type: ignore[method-assign]
            installed_any = True
        else:
            installed_any = True

    _SOLO_BOT_HOOKS_INSTALLED = installed_any
    if installed_any:
        _LOG.info(
            "Installed Solo Bot lifecycle hooks: solo games spawn Luci; multiplayer games remove it."
        )
    else:
        _LOG.error("No Solo Bot lifecycle hooks could be installed.")

def _install_quiz() -> None:
    """Start the optional automatic chat quiz and coin rewards."""
    if not _setting_enabled("quiz", "enable", True):
        _LOG.info("Automatic trivia quiz skipped because quiz.enable is false.")
        return
    try:
        from features import quiz

        if quiz.install():
            _LOG.info("Installed automatic trivia quiz with coin rewards.")
        else:
            _LOG.info("Automatic trivia quiz is disabled by configuration.")
    except Exception:
        # Quiz errors must not prevent the server's other plugins from starting.
        _LOG.exception("Could not initialize the automatic trivia quiz.")





def _install_atd_screens() -> None:
    """Install the ATD score-screen effects and layout patches."""
    try:
        from features import atd_screens

        enable = getattr(atd_screens, "enable", None)
        if not callable(enable):
            raise RuntimeError("features.atd_screens has no callable enable() function.")

        enable()
        _LOG.info("Initialized ATD score-screen hooks.")
    except Exception:
        # Score-screen cosmetics must not prevent the server's other hooks from starting.
        _LOG.exception("Could not initialize ATD score-screen hooks.")


def _install_atd_main_menu() -> None:
    """Install the custom ATD main-menu activity from features/atd_main_menu.py."""
    try:
        from features import atd_main_menu

        enable = getattr(atd_main_menu, "enable", None)
        if not callable(enable):
            raise RuntimeError("features.atd_main_menu has no callable enable() function.")

        enable()
        _LOG.info("Initialized ATD main-menu hook.")
    except Exception:
        # Menu cosmetics must not prevent the server's other hooks from starting.
        _LOG.exception("Could not initialize ATD main-menu hook.")


def _install_discord_bot() -> None:
    """Start the optional Discord bot from mods/plugins/discord_bot.py."""
    global _DISCORD_BOT_HOOK_STARTED
    if _DISCORD_BOT_HOOK_STARTED:
        return

    config = _read_mod_settings().get("discordbot", {})
    if not isinstance(config, dict):
        _LOG.error("setting.json discordbot section must be a JSON object.")
        return
    discord_enabled = config.get("enable", False) is True
    web_enabled = config.get("webEnable", discord_enabled) is True
    if not (discord_enabled or web_enabled):
        _LOG.info("Discord bot skipped because discordbot.enable and webEnable are false.")
        return

    try:
        from plugins import discord_bot

        start = getattr(discord_bot, "start_from_custom_hooks", None)
        if not callable(start):
            raise AttributeError(
                "plugins.discord_bot does not define start_from_custom_hooks()."
            )
        started = start()
        if started:
            _DISCORD_BOT_HOOK_STARTED = True
            _LOG.info("Started Discord bot integration from custom_hooks.py.")
        else:
            _LOG.info("Discord integration did not start; check discordbot settings.")
    except Exception:
        # A Discord integration error must not disable existing game hooks.
        _LOG.exception("Could not start plugins.discord_bot.")



def _install_flying_mine() -> None:
    """Install the Flying Mine plugin only when enabled in setting.json."""
    global _FLYING_MINE_INSTALLED
    if _FLYING_MINE_INSTALLED:
        return

    # Add "flyingMine": {"enable": true} to mods/setting.json to enable it.
    # A missing or invalid setting defaults to disabled to avoid surprise spawns.
    if not _setting_enabled("flyingMine", "enable", False):
        _LOG.info("Flying Mine skipped because flyingMine.enable is false or missing.")
        return

    try:
        from plugins import flying_mine

        enable = getattr(flying_mine, "enable", None)
        if not callable(enable):
            raise AttributeError(
                "plugins.flying_mine does not define a callable enable() function."
            )

        if enable() is False:
            _LOG.warning("Flying Mine plugin returned False during installation.")
            return

        _FLYING_MINE_INSTALLED = True
        _LOG.info("Installed Flying Mine plugin (flyingMine.enable=true).")
    except Exception:
        # Keep an optional gameplay plugin failure isolated from the other hooks.
        _LOG.exception("Could not install plugins.flying_mine.")


def install() -> None:
    """Install tags, stats, chat, bans, and Solo Bot lifecycle hooks."""
    global _SPAZ_INSTALLED

    _install_account_info_capture()
    _install_stats_hook()
    _install_coin_reward_hook()
    _install_auto_team_balance()

    # Keep the currently working floating role/custom-tag integration.
    if not _SPAZ_INSTALLED:
        try:
            from bascenev1lib.actor.playerspaz import PlayerSpaz

            original_init = PlayerSpaz.__init__
            if not getattr(original_init, "_roles_tags_wrapped", False):

                def wrapped_init(self: Any, player: Any, *args: Any, **kwargs: Any) -> None:
                    original_init(self, player, *args, **kwargs)
                    try:
                        from spazmod import modifyspaz

                        modifyspaz.main(self, player)
                    except Exception:
                        # A tag failure must never break normal character spawning.
                        _LOG.exception("Role-tag hook failed after PlayerSpaz initialization.")

                wrapped_init._roles_tags_wrapped = True  # type: ignore[attr-defined]
                PlayerSpaz.__init__ = wrapped_init  # type: ignore[method-assign]

            _SPAZ_INSTALLED = True
            _LOG.info("Installed API 9 PlayerSpaz hook for floating tags.")
        except Exception:
            _LOG.exception("Could not install PlayerSpaz role/custom-tag hook.")

    # chatfilter.install() owns the single native chat-hook wrapper.
    try:
        from chathandle.chatfilter import chatfilter

        if not chatfilter.install():
            _LOG.error(
                "Chat filter/command hook did not install; floating tags remain enabled."
            )
        else:
            _LOG.info("Installed chat moderation and slash-command routing.")
    except Exception:
        _LOG.exception("Could not initialize chathandle chat filtering/commands.")

    _install_ban_monitor()
    # Keep the character chooser line commented while its joining behaviour is being tested.
    _install_character_chooser()
    _install_solo_bot_hooks()
    _install_map_tint()
    _install_quiz()

    # Optional visual features. Failures must not disable the existing hooks.
    _install_text_on_map()
    _install_colorful_explosions()
    _install_snowfall()
    _install_powerup_manager()
    _install_flying_mine()
    _install_atd_main_menu()
    _install_atd_screens()

    # Install cosmetic punch hooks only when enabled in setting.json.
    if _setting_enabled("hiteffects", default=True):
        try:
            from spazmod import hit_effect
            if not hit_effect.install():
                _LOG.error("Strong-punch metal hit effect was not installed.")
        except Exception:
            _LOG.exception("Could not install strong-punch metal hit effect.")
    else:
        _LOG.info("Strong-punch hit effect skipped because hiteffects is false.")

    if _setting_enabled("hitmessages", default=True):
        try:
            from spazmod import hit_messages
            if not hit_messages.install():
                _LOG.error("Powerful-punch hit messages were not installed.")
        except Exception:
            _LOG.exception("Could not install powerful-punch hit messages.")
    else:
        _LOG.info("Powerful-punch hit messages skipped because hitmessages is false.")

    # Keep the Discord bot optional and independent from core gameplay hooks.
    _install_discord_bot()

# ba_meta export babase.Plugin
class CustomHooksPlugin(babase.Plugin):
    """Plugin entry point for role/custom tags, chat commands, stats, and bans."""

    def on_app_running(self) -> None:
        install()
        _LOG.info("Custom hooks startup completed.")

    def on_app_shutdown(self) -> None:
        if not _DISCORD_BOT_HOOK_STARTED:
            return
        try:
            from plugins import discord_bot

            shutdown = getattr(discord_bot, "shutdown_from_custom_hooks", None)
            if callable(shutdown):
                shutdown()
        except ModuleNotFoundError as exc:
            if exc.name not in {"plugins", "plugins.discord_bot"}:
                _LOG.exception("Could not import Discord bot shutdown hook.")
        except Exception:
            _LOG.exception("Could not shut down Discord bot integration cleanly.")
