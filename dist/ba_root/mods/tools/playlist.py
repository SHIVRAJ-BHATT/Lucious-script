# ba_meta require api 9
"""Runtime shared-playlist switching for BombSquad 1.8 / API 9.

Playlist changes affect the live host session only. The startup playlist selection
and playlist collections in config.json are restored after every import.
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any

import babase
import bascenev1 as bs

_LOG = logging.getLogger("Lucifer.Playlist")
_LOCK = threading.RLock()
_PENDING = False

_CONFIG_KEYS = (
    "Free-for-All Playlist Selection",
    "Free-for-All Playlists",
    "Team Tournament Playlist Selection",
    "Team Tournament Playlists",
)


def _send(client_id: int | None, message: str, color: tuple[float, float, float] = (0.78, 0.90, 1.0)) -> None:
    if client_id is None:
        _LOG.info("%s", message)
        return
    try:
        from chathandle.chatcommands import handlers
        handlers.send(message, client_id, color=color)
    except Exception:
        _LOG.exception("Could not send private playlist response.")


def _config_snapshot(config: Any) -> dict[str, tuple[bool, Any]]:
    return {
        key: (key in config, copy.deepcopy(config.get(key)))
        for key in _CONFIG_KEYS
    }


def _restore_config(config: Any, snapshot: dict[str, tuple[bool, Any]]) -> None:
    for key, (existed, value) in snapshot.items():
        if existed:
            config[key] = copy.deepcopy(value)
        else:
            config.pop(key, None)
    try:
        config.commit()
    except Exception:
        _LOG.exception("Could not persist restored temporary-playlist configuration.")


def _session_classes() -> tuple[type, type, type]:
    from bascenev1._coopsession import CoopSession
    from bascenev1._dualteamsession import DualTeamSession
    from bascenev1._freeforallsession import FreeForAllSession
    return CoopSession, DualTeamSession, FreeForAllSession


def _with_delay(session_type: type, playlist_data: list[dict[str, Any]] | None) -> None:
    """Wait off-thread, then create the new session on the logic thread."""
    time.sleep(1.0)

    def update() -> None:
        try:
            bs.new_host_session(session_type)
            if playlist_data:
                _update_playlist(playlist_data)
        except Exception:
            _LOG.exception("Could not switch host session to %s.", session_type.__name__)

    try:
        babase.pushcall(update, from_other_thread=True)
    except Exception:
        _LOG.exception("Could not schedule host-session change.")


def _update_next_map(session: Any, game_spec: Any) -> None:
    session._next_game_spec = game_spec
    # API 9 exposes session.context as a ContextRef property, not a method.
    with session.context:
        session._instantiate_next_game()


def _update_playlist(playlist_data: list[dict[str, Any]]) -> None:
    """Replace the current session's playlist and instantiate its next game."""
    from bascenev1 import filter_playlist
    from bascenev1._multiteamsession import ShuffleList

    session = bs.get_foreground_host_session()
    if session is None:
        raise RuntimeError("No active host session is available.")
    filtered = filter_playlist(
        copy.deepcopy(playlist_data),
        sessiontype=type(session),
        add_resolved_type=True,
    )
    resolved = ShuffleList(filtered, shuffle=False)
    session._playlist = resolved
    _update_next_map(session, resolved.pull_next())


def _activate_playlist(playlist_data: list[dict[str, Any]], playlist_type: str) -> None:
    """Switch session mode if needed, otherwise replace its current playlist."""
    coop_type, team_type, ffa_type = _session_classes()
    session = bs.get_foreground_host_session()
    if session is None:
        raise RuntimeError("No active host session is available.")

    if playlist_type == "Free-for-All":
        wanted_type = ffa_type
    elif playlist_type == "Team Tournament":
        wanted_type = team_type
    else:
        raise ValueError(f"Unsupported playlist type: {playlist_type!r}")

    if isinstance(session, wanted_type):
        _update_playlist(playlist_data)
        return

    if isinstance(session, (team_type, ffa_type, coop_type)):
        session.end()
        threading.Thread(
            target=_with_delay,
            args=(wanted_type, copy.deepcopy(playlist_data)),
            daemon=True,
        ).start()
        return

    raise RuntimeError(f"Cannot switch playlist from session {type(session).__name__}.")


def _handle_import_result(
    content: Any,
    *,
    client_id: int | None,
    snapshot: dict[str, tuple[bool, Any]],
    config: Any,
) -> None:
    global _PENDING
    try:
        if not isinstance(content, dict):
            _restore_config(config, snapshot)
            _send(client_id, "Playlist import failed or returned no playlist.", (1.0, 0.18, 0.18))
            return

        playlist_type = content.get("playlistType")
        playlist_name = content.get("playlistName")
        if playlist_type not in ("Free-for-All", "Team Tournament") or not isinstance(playlist_name, str):
            _restore_config(config, snapshot)
            _send(client_id, "Imported playlist has an unsupported type or name.", (1.0, 0.18, 0.18))
            return

        playlists_key = f"{playlist_type} Playlists"
        playlist_map = config.get(playlists_key, {})
        playlist_data = playlist_map.get(playlist_name) if isinstance(playlist_map, dict) else None
        if not isinstance(playlist_data, list) or not playlist_data:
            _restore_config(config, snapshot)
            _send(client_id, "Playlist was imported but its data could not be read from config.", (1.0, 0.18, 0.18))
            return

        # Copy out the fetched playlist before restoring temporary config state.
        runtime_playlist = copy.deepcopy(playlist_data)
        try:
            _activate_playlist(runtime_playlist, playlist_type)
        except Exception:
            _LOG.exception("Could not activate imported playlist %s.", playlist_name)
            _restore_config(config, snapshot)
            _send(client_id, "Playlist import succeeded, but live activation failed; check the server log.", (1.0, 0.18, 0.18))
            return

        # Always restore the pre-import configuration: permanent selection was
        # deliberately removed, so /playlist never changes config.json.
        _restore_config(config, snapshot)
        _send(client_id, f"Playlist '{playlist_name}' is active for this session only; config.json was preserved.")
    finally:
        with _LOCK:
            _PENDING = False


def _import_playlist_code(code: str, client_id: int | None) -> bool:
    global _PENDING
    with _LOCK:
        if _PENDING:
            _send(client_id, "A playlist request is already in progress; wait for it to finish.", (1.0, 0.65, 0.2))
            return False
        _PENDING = True

    try:
        import bauiv1 as bui
        plus = getattr(bui.app, "plus", None)
        if plus is None:
            raise RuntimeError("Playlist import requires the legacy Plus/V1 transaction subsystem.")
        config = babase.app.config
        snapshot = _config_snapshot(config)

        def callback(content: Any) -> None:
            _handle_import_result(
                content,
                client_id=client_id,
                snapshot=snapshot,
                config=config,
            )

        plus.add_v1_account_transaction(
            {"type": "IMPORT_PLAYLIST", "code": code, "overwrite": True},
            callback=callback,
        )
        plus.run_v1_account_transactions()
        _send(client_id, f"Playlist code {code} requested; waiting for the playlist service.")
        return True
    except Exception as exc:
        with _LOCK:
            _PENDING = False
        _LOG.exception("Playlist-code import request failed.")
        _send(client_id, f"Could not request playlist code {code}: {exc}", (1.0, 0.18, 0.18))
        return False


def _activate_named_playlist(name: str, client_id: int | None) -> bool:
    config = babase.app.config
    matches: list[tuple[str, str, list[dict[str, Any]]]] = []
    for playlist_type in ("Free-for-All", "Team Tournament"):
        playlist_map = config.get(f"{playlist_type} Playlists", {})
        if not isinstance(playlist_map, dict):
            continue
        for playlist_name, data in playlist_map.items():
            if isinstance(playlist_name, str) and playlist_name.casefold() == name.casefold() and isinstance(data, list):
                matches.append((playlist_type, playlist_name, copy.deepcopy(data)))
    if len(matches) != 1:
        _send(client_id, "Playlist name not found or ambiguous. Use a shared-playlist code, or an exact configured playlist name.", (1.0, 0.18, 0.18))
        return False

    playlist_type, actual_name, playlist_data = matches[0]
    try:
        _activate_playlist(playlist_data, playlist_type)
        _send(client_id, f"Playlist '{actual_name}' changed live for this session only; config.json selection was not changed.")
        return True
    except Exception:
        _LOG.exception("Could not activate configured playlist %s.", name)
        _send(client_id, "Could not activate that playlist. Check the server log.", (1.0, 0.18, 0.18))
        return False


def _activate_configured_selection(playlist_type: str, client_id: int | None) -> bool:
    """Activate the playlist currently selected in config for FFA or teams."""
    config = babase.app.config
    selection_key = f"{playlist_type} Playlist Selection"
    playlists_key = f"{playlist_type} Playlists"
    name = config.get(selection_key)
    playlist_map = config.get(playlists_key, {})
    if not isinstance(name, str) or not isinstance(playlist_map, dict):
        _send(client_id, f"No configured {playlist_type} playlist selection was found.", (1.0, 0.18, 0.18))
        return False
    playlist_data = playlist_map.get(name)
    if not isinstance(playlist_data, list) or not playlist_data:
        _send(client_id, f"Configured playlist '{name}' was not found or is empty.", (1.0, 0.18, 0.18))
        return False
    try:
        _activate_playlist(copy.deepcopy(playlist_data), playlist_type)
        _send(client_id, f"Switched to configured {playlist_type} playlist '{name}' for this session only.")
        return True
    except Exception:
        _LOG.exception("Could not activate the configured %s playlist.", playlist_type)
        _send(client_id, "Could not activate the configured playlist; check the server log.", (1.0, 0.18, 0.18))
        return False


def change_playlist(arguments: list[str], client_id: int | None = None) -> bool:
    """Activate a playlist for this session only; config.json is never changed."""
    if len(arguments) != 1:
        _send(client_id, "Usage: /playlist <shared-code-or-playlist-name> (session only)", (1.0, 0.18, 0.18))
        return False

    value = arguments[0].strip()
    if not value:
        _send(client_id, "Playlist code or name cannot be empty.", (1.0, 0.18, 0.18))
        return False

    mode_alias = value.casefold().replace("_", "-").strip()
    if mode_alias in ("ffa", "free-for-all"):
        return _activate_configured_selection("Free-for-All", client_id)
    if mode_alias in ("teams", "team", "team-tournament"):
        return _activate_configured_selection("Team Tournament", client_id)

    if value.casefold() == "coop":
        try:
            coop_type, _team_type, _ffa_type = _session_classes()
            session = bs.get_foreground_host_session()
            if session is None:
                raise RuntimeError("No active host session is available.")
            if isinstance(session, coop_type):
                _send(client_id, "The server is already in co-op mode.")
                return True
            session.end()
            threading.Thread(target=_with_delay, args=(coop_type, None), daemon=True).start()
            _send(client_id, "Switching the server to co-op mode for this session.")
            return True
        except Exception:
            _LOG.exception("Could not switch to co-op mode.")
            _send(client_id, "Could not switch to co-op mode; check the server log.", (1.0, 0.18, 0.18))
            return False

    if value.isdecimal():
        return _import_playlist_code(value, client_id)
    return _activate_named_playlist(value, client_id)
