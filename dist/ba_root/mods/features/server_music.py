# ba_meta require api 9
"""Loop the custom workspace music during each match."""

from __future__ import annotations

import logging
from typing import Any

import bascenev1 as bs

_LOG = logging.getLogger("LuciferServerMusic")

_INSTALLED = False
_CURRENT_ACTIVITY: Any | None = None
_CURRENT_NODE: Any | None = None


def _stop_music() -> None:
    """Stop and remove the previous custom music node."""
    global _CURRENT_ACTIVITY, _CURRENT_NODE

    activity = _CURRENT_ACTIVITY
    node = _CURRENT_NODE

    _CURRENT_ACTIVITY = None
    _CURRENT_NODE = None

    if node is None:
        return

    try:
        if node.exists():
            if activity is not None and not getattr(
                activity, "expired", True
            ):
                with activity.context:
                    if node.exists():
                        node.delete()
            else:
                node.delete()

        _LOG.info("Stopped custom match music.")

    except Exception:
        _LOG.exception("Could not stop custom match music.")


def _start_music(activity: Any) -> None:
    """Start the custom track as a looping, non-positional sound."""
    global _CURRENT_ACTIVITY, _CURRENT_NODE

    if activity.has_ended() or not activity.has_begun():
        return

    _stop_music()

    try:
        from workspace1 import audio

        with activity.context:
            # Resolve the custom asset.
            sound = audio.my_music.get()

            # Create a looping music node for this game activity.
            node = bs.newnode(
                "sound",
                attrs={
                    "sound": sound,
                    "volume": 0.8,
                    "positional": False,
                    "loop": True,
                },
                name="Lucifer custom match music",
            )

            _CURRENT_ACTIVITY = activity
            _CURRENT_NODE = node

            # Stop the default game soundtrack after our asset loads.
            bs.setmusic(None)

        _LOG.info(
            "Started looping custom music: workspace1.audio.my_music"
        )

    except Exception:
        _LOG.exception(
            "Could not load/play workspace1.audio.my_music. "
            "Check asset-package access and package ID 715."
        )


def install() -> bool:
    """Install music hooks without replacing existing gameplay logic."""
    global _INSTALLED

    if _INSTALLED:
        return True

    activity_class = getattr(bs, "Activity", None)
    game_activity_class = getattr(bs, "GameActivity", None)

    if activity_class is None or game_activity_class is None:
        _LOG.error("API 9 Activity classes are unavailable.")
        return False

    original_begin = getattr(activity_class, "begin", None)

    if not callable(original_begin):
        _LOG.error("Activity.begin() is unavailable.")
        return False

    if not getattr(
        original_begin, "_lucifer_server_music_begin_wrapped", False
    ):

        def wrapped_activity_begin(
            self: Any, *args: Any, **kwargs: Any
        ) -> Any:
            result = original_begin(self, *args, **kwargs)

            try:
                if isinstance(self, game_activity_class):
                    _start_music(self)
                else:
                    _stop_music()

            except Exception:
                _LOG.exception("Music Activity.begin hook failed.")

            return result

        wrapped_activity_begin._lucifer_server_music_begin_wrapped = True
        # Preserve the marker from the existing Solo Bot wrapper.
        wrapped_activity_begin._lucifer_solo_bot_begin_wrapped = bool(
            getattr(original_begin, "_lucifer_solo_bot_begin_wrapped", False)
        )

        activity_class.begin = wrapped_activity_begin

    original_end = getattr(activity_class, "end", None)

    if callable(original_end) and not getattr(
        original_end, "_lucifer_server_music_end_wrapped", False
    ):

        def wrapped_activity_end(
            self: Any, *args: Any, **kwargs: Any
        ) -> Any:
            if self is _CURRENT_ACTIVITY:
                _stop_music()

            return original_end(self, *args, **kwargs)

        wrapped_activity_end._lucifer_server_music_end_wrapped = True

        # Preserve markers used by existing statistics, coins, and Solo Bot.
        for marker in (
            "_lucifer_stats_wrapped",
            "_lucifer_coins_wrapped",
            "_lucifer_solo_bot_end_wrapped",
        ):
            setattr(
                wrapped_activity_end,
                marker,
                bool(getattr(original_end, marker, False)),
            )

        activity_class.end = wrapped_activity_end

    _INSTALLED = True

    _LOG.info("Installed custom match music lifecycle hooks.")
    return True
