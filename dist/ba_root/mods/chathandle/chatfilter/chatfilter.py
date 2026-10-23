# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""API 9 chat filtering, anti-spam, and command-routing integration.

This preserves the existing spam/profanity behavior and routes messages only
once through chathandle.handlechat after moderation. The native hook remains
installed here; custom_hooks.py does not install a competing chat hook.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import bascenev1 as bs

from tools.account_ids import get_v2_account_id, is_v2_account_id
from chathandle.chatspam import checkspam
from chathandle.chatfilter import profanity

_LOG = logging.getLogger("ChatFilter")
_INSTALLED = False
_GLOBAL_CHAT_MUTED = False
_GLOBAL_MUTE_NOTICE_TIMES: dict[int, float] = {}
_STAFF_MUTE_NOTICE_TIMES: dict[str, float] = {}
_MUTE_NOTICE_INTERVAL_SECONDS = 10.0


def set_global_chat_muted(muted: bool) -> None:
    """Enable or disable session-wide chat mute; slash commands remain routable."""
    global _GLOBAL_CHAT_MUTED
    _GLOBAL_CHAT_MUTED = bool(muted)
    _GLOBAL_MUTE_NOTICE_TIMES.clear()
    _LOG.info("Global player chat %s.", "muted" if _GLOBAL_CHAT_MUTED else "unmuted")


def is_global_chat_muted() -> bool:
    """Return whether session-wide normal chat is currently muted."""
    return _GLOBAL_CHAT_MUTED


def _should_notify(notices: dict[Any, float], key: Any) -> bool:
    """Rate-limit mute reminders so repeated blocked messages do not spam users."""
    now = time.monotonic()
    last = notices.get(key, 0.0)
    if now - last < _MUTE_NOTICE_INTERVAL_SECONDS:
        return False
    notices[key] = now
    # Keep these bookkeeping dictionaries bounded during long-running sessions.
    if len(notices) > 2048:
        cutoff = now - 4 * _MUTE_NOTICE_INTERVAL_SECONDS
        for old_key, timestamp in list(notices.items()):
            if timestamp < cutoff:
                notices.pop(old_key, None)
    return True


def _get_session_player(client_id: int) -> Any | None:
    """Find the active SessionPlayer for a network client ID."""
    try:
        session = bs.get_foreground_host_session()
        if session is None:
            return None
        for session_player in getattr(session, "sessionplayers", ()):
            try:
                if int(session_player.inputdevice.client_id) == int(client_id):
                    return session_player
            except Exception:
                continue
    except Exception:
        _LOG.exception("Could not inspect active session players.")
    return None


def _get_v2_id(client_id: int) -> str | None:
    """Resolve only a V2 ID; never fall back to V1 for role lookup."""
    session_player = _get_session_player(client_id)
    if session_player is not None:
        try:
            account_id = get_v2_account_id(session_player)
            if is_v2_account_id(account_id):
                return account_id
        except Exception:
            _LOG.exception(
                "V2 account-ID helper failed for client %s.", client_id
            )

    # Some engine roster versions expose the account ID directly. Accept it
    # only when it validates as V2; never use a legacy pb- ID for roles.
    try:
        for item in bs.get_game_roster():
            if int(item.get("client_id", -999)) != int(client_id):
                continue
            account_id = item.get("account_id")
            if is_v2_account_id(account_id):
                return account_id
    except Exception:
        _LOG.debug(
            "Could not resolve roster V2 ID for client %s.", client_id
        )
    return None


def _is_warning_exempt(account_id: str | None) -> bool:
    """Only the configured Owner ID bypasses spam warning strikes."""
    if not is_v2_account_id(account_id):
        return False
    try:
        from playersdata import pdata

        owner = pdata.get_roles().get("owner", {})
        return account_id in owner.get("ids", [])
    except Exception:
        _LOG.exception("Could not check owner warning exemption.")
        return False


def _censor_message(message: str) -> str:
    """Censor message text using configured profanity patterns."""
    return profanity.censor(message)


def _notify(
    client_id: int,
    message: str,
    color: tuple[float, float, float],
) -> None:
    """Send a transient private notification."""
    try:
        bs.broadcastmessage(
            message,
            color=color,
            transient=True,
            clients=[int(client_id)],
        )
    except Exception:
        _LOG.exception("Could not notify client %s.", client_id)


def _kick(client_id: int, identity: str | None, reason: str) -> None:
    """Disconnect a client after the configured profanity warning limit."""
    _LOG.warning(
        "Disconnecting client %s (%s): %s", client_id, identity, reason
    )
    try:
        bs.disconnect_client(int(client_id))
    except Exception:
        _LOG.exception("Could not disconnect client %s.", client_id)


def filter_message(message: str, client_id: int) -> str | None:
    """Return censored text, or None when a message is suppressed/kicked."""
    if not isinstance(message, str):
        message = str(message)

    # Do not process host-console messages as network-player chat.
    try:
        client_id = int(client_id)
    except (TypeError, ValueError):
        return message
    if client_id < 0:
        return message

    account_id = _get_v2_id(client_id)
    exempt = _is_warning_exempt(account_id)

    # Staff/global mutes suppress ordinary public chat but do not block slash
    # commands. That allows authorized staff to issue /unmute or /unmutechat.
    is_command = message.startswith("/")
    if not is_command and _GLOBAL_CHAT_MUTED:
        if _should_notify(_GLOBAL_MUTE_NOTICE_TIMES, client_id):
            _notify(
                client_id,
                "Global chat is currently muted.",
                (1.0, 0.35, 0.25),
            )
        return None

    if not is_command and is_v2_account_id(account_id):
        try:
            from playersdata import pdata

            staff_mute = pdata.get_active_mute(account_id)
        except Exception:
            _LOG.exception(
                "Could not read staff mute record for V2 account %s.", account_id
            )
            staff_mute = None
        if staff_mute is not None:
            if _should_notify(_STAFF_MUTE_NOTICE_TIMES, account_id):
                reason = str(staff_mute.get("reason", "No reason provided"))[:160]
                expires_at = staff_mute.get("expires_at")
                if expires_at:
                    notice = f"You are muted by staff until {expires_at}. Reason: {reason}"
                else:
                    notice = f"You are muted by staff until an administrator unmutes you. Reason: {reason}"
                _notify(client_id, notice, (1.0, 0.35, 0.25))
            return None

    # A spam-muted player cannot send chat during the mute.
    mute_status = checkspam.get_mute_status(account_id, client_id)
    if mute_status["muted"]:
        if mute_status["notify"]:
            remaining = int(mute_status["remaining_seconds"])
            minutes, seconds = divmod(remaining, 60)
            if minutes:
                left = f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
            else:
                left = f"{seconds}s"
            _notify(
                client_id,
                f"You are muted for spam. Time remaining: {left}.",
                (1.0, 0.35, 0.25),
            )
        return None

    # Preserve the existing three-warning-then-kick profanity policy.
    censored = _censor_message(message)
    if censored != message:
        violation = checkspam.record_violation(account_id, client_id)
        strikes = int(violation["strikes"])
        if violation["kick"]:
            _kick(client_id, account_id, "profanity after three warnings")
            return None

        warning_messages = {
            1: "Abuse warning 1/3: Please keep the chat respectful.",
            2: "Abuse warning 2/3: One more warning remains.",
            3: (
                "Abuse warning 3/3: FINAL WARNING. Next abusive message "
                "means an immediate kick."
            ),
        }
        warning = warning_messages.get(
            strikes, "Please keep the chat respectful."
        )
        _notify(client_id, warning, (1.0, 0.2, 0.2))

    # Use the original message for repeat/flood detection, but return its
    # censored version if it is otherwise allowed.
    spam_result = checkspam.check_message(
        account_id,
        message,
        client_id,
        exempt_from_strikes=exempt,
    )
    if spam_result["spam"]:
        reason = spam_result["reason"]
        if reason == "message-flood":
            reason_text = "Sending messages too fast."
        elif reason == "repeated-message":
            reason_text = "Please do not repeat the same message."
        elif reason == "muted":
            reason_text = "You are muted for spam."
        else:
            reason_text = "Spam detected."

        # Owner's spam messages remain suppressed, but are exempt from spam
        # warnings/mutes. Profanity warnings are handled independently above.
        if not exempt:
            strikes = int(spam_result.get("strikes", 0))
            if spam_result.get("muted"):
                duration = int(
                    spam_result.get(
                        "mute_seconds", checkspam.MUTE_DURATION_SECONDS
                    )
                )
                mute_level = int(spam_result.get("mute_level", 1))
                if duration < 60:
                    duration_text = f"{duration} seconds"
                else:
                    minutes, seconds = divmod(duration, 60)
                    if seconds:
                        duration_text = f"{minutes} minute {seconds} seconds"
                    else:
                        duration_text = (
                            "1 minute" if minutes == 1 else f"{minutes} minutes"
                        )
                notice = (
                    f"Spam warning limit reached ({checkspam.MAX_SPAM_WARNINGS}/"
                    f"{checkspam.MAX_SPAM_WARNINGS}). {reason_text} "
                    f"You have been muted for {duration_text} "
                    f"(mute {min(mute_level, 3)}/3)."
                )
            else:
                notice = (
                    f"Spam warning {strikes}/{checkspam.MAX_SPAM_WARNINGS}: "
                    f"{reason_text}"
                )
            _notify(client_id, notice, (1.0, 0.35, 0.25))
        # Spam is suppressed and never kicks.
        return None

    # Record allowed public text at the end of moderation, before the separate
    # command/router hook. This also works if that downstream handler returns
    # None or raises while ordinary text is still allowed through by fallback.
    # Suppressed messages returned earlier above are never recorded.
    if not is_command:
        try:
            from chathandle.chat_history import record_message

            record_message(client_id, censored)
        except Exception:
            _LOG.exception("Could not record an allowed public chat message.")

    return censored


def _route_after_moderation(message: str, client_id: int) -> str | None:
    """Pass moderated messages through the single chat handler."""
    try:
        from chathandle import handlechat

        return handlechat.filter_chat_message(message, client_id)
    except Exception:
        _LOG.exception("Could not route message through chathandle.handlechat.")
        # Fail closed for slash commands so they never appear as public chat.
        if isinstance(message, str) and message.startswith("/"):
            try:
                target = int(client_id)
                if target >= 0:
                    _notify(
                        target,
                        "Command system unavailable; check the server log.",
                        (1.0, 0.2, 0.2),
                    )
            except (TypeError, ValueError):
                pass
            return None
        return message


def install() -> bool:
    """Wrap the API 9 native chat hook and refresh the engine hook table."""
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import bascenev1._hooks as hooks

        original_filter = hooks.filter_chat_message
        if getattr(original_filter, "_lucifer_chat_filter_wrapped", False):
            _INSTALLED = True
            return True

        def wrapped_filter(message: str, client_id: int) -> str | None:
            # Preserve filters installed before this module.
            try:
                original_message = original_filter(message, client_id)
            except Exception:
                _LOG.exception("An earlier native chat filter failed.")
                # Keep ordinary chat available, but do not execute commands if
                # the earlier filter itself failed.
                if isinstance(message, str) and message.startswith("/"):
                    return None
                return message

            if original_message is None:
                return None

            try:
                filtered_message = filter_message(original_message, client_id)
            except Exception:
                _LOG.exception("Lucifer chat moderation failed.")
                # Fail closed for commands when moderation fails.
                if isinstance(original_message, str) and original_message.startswith("/"):
                    return None
                return original_message

            if filtered_message is None:
                return None
            return _route_after_moderation(filtered_message, client_id)

        wrapped_filter._lucifer_chat_filter_wrapped = True  # type: ignore[attr-defined]
        hooks.filter_chat_message = wrapped_filter
        bs.reload_hooks()
        _INSTALLED = True
        _LOG.info("Installed API 9 chat filter, anti-spam, and command routing.")
        _LOG.info("Profanity censoring is loaded from chathandle.chatfilter.profanity.")
        return True
    except Exception:
        _LOG.exception("Could not install API 9 chat filter.")
        return False
