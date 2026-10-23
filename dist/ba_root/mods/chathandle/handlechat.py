# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""Route already-moderated chat messages and consume slash commands.

The native hook is installed by chathandle.chatfilter.chatfilter. This module
is called by that wrapper only after its original filter and the existing
spam/profanity checks have allowed a message through.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

_LOG = logging.getLogger("Lucifer.HandleChat")
_ERROR_COLOR = (1.0, 0.2, 0.2)

# Cache successful lazy resolutions: this function sits on the hot path for
# every chat message. Failed imports are deliberately not cached so a transient
# import/initialisation problem can recover without restarting the server.
_QUIZ_ANSWER_HANDLER: Callable[..., Any] | None = None
_COMMAND_EXECUTOR: Callable[..., Any] | None = None
_HANDLER_SEND: Callable[..., Any] | None = None


def _get_quiz_answer_handler() -> Callable[..., Any]:
    """Resolve and cache the optional quiz handler on first use."""
    global _QUIZ_ANSWER_HANDLER
    if _QUIZ_ANSWER_HANDLER is None:
        from features import quiz

        handler = getattr(quiz, "handle_answer", None)
        if not callable(handler):
            raise AttributeError("features.quiz.handle_answer is not callable")
        _QUIZ_ANSWER_HANDLER = handler
    return _QUIZ_ANSWER_HANDLER


def _get_command_executor() -> Callable[..., Any]:
    """Resolve and cache the slash-command dispatcher on first use."""
    global _COMMAND_EXECUTOR
    if _COMMAND_EXECUTOR is None:
        from chathandle.chatcommands import command_executor

        executor = getattr(command_executor, "execute", None)
        if not callable(executor):
            raise AttributeError("command_executor.execute is not callable")
        _COMMAND_EXECUTOR = executor
    return _COMMAND_EXECUTOR


def _get_handler_send() -> Callable[..., Any]:
    """Resolve and cache the private error-message sender on demand."""
    global _HANDLER_SEND
    if _HANDLER_SEND is None:
        from chathandle.chatcommands import handlers

        sender = getattr(handlers, "send", None)
        if not callable(sender):
            raise AttributeError("handlers.send is not callable")
        _HANDLER_SEND = sender
    return _HANDLER_SEND


def refresh_chat_dispatch_cache() -> None:
    """Clear cached handlers after an intentional runtime module reload."""
    global _QUIZ_ANSWER_HANDLER, _COMMAND_EXECUTOR, _HANDLER_SEND
    _QUIZ_ANSWER_HANDLER = None
    _COMMAND_EXECUTOR = None
    _HANDLER_SEND = None


def filter_chat_message(msg: str, client_id: int) -> str | None:
    """Return regular chat unchanged; execute and consume slash commands."""
    if not isinstance(msg, str) or not msg:
        return msg

    if not msg.startswith("/"):
        # Ordinary public chat may contain an answer to the automatic trivia
        # quiz. The optional feature must never block normal chat on failure.
        try:
            if _get_quiz_answer_handler()(msg, client_id):
                # Consume a winning answer so it is replaced by the reward notice.
                return None
        except Exception:
            _LOG.exception("Quiz answer handling failed; preserving ordinary chat.")
        return msg

    # Host-console messages do not have a connected V2 account identity. Do not
    # let a host-originated message bypass the role-permission path.
    if isinstance(client_id, bool):
        _LOG.warning("Ignored slash command with a boolean client ID.")
        return None
    try:
        client_id = int(client_id)
    except (TypeError, ValueError, OverflowError):
        _LOG.warning("Ignored slash command with an invalid client ID.")
        return None

    if client_id < 0:
        _LOG.warning("Ignored host-originated slash command without player identity.")
        return None

    try:
        _get_command_executor()(msg, client_id)
    except Exception:
        # Fail closed: never broadcast a raw slash command if dispatch fails.
        _LOG.exception("Command processing failed for client %s.", client_id)
        try:
            _get_handler_send()(
                "Command processing failed; check the server terminal.",
                client_id,
                color=_ERROR_COLOR,
            )
        except Exception:
            _LOG.exception("Could not send command-error notification.")

    # Commands are always consumed; they never enter public chat.
    return None
