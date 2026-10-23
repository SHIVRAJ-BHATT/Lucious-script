"""Terminal-first diagnostics for Lucifer's BombSquad API 9 mods.

Install this early from custom_hooks.py. WARNING-and-higher log records,
Python warnings, uncaught Python/thread/task exceptions, and faulthandler
tracebacks are sent directly to the process stderr (normally the tmux terminal).
No diagnostic log files are created by this module.

This module cannot catch every exception deliberately swallowed elsewhere or
all native/OS-level terminations; it does not replace per-module error handling.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import signal
import sys
import threading
import warnings
from types import TracebackType
from typing import Any

_LOG = logging.getLogger("Lucifer.ErrorMonitor")
_INSTALLED = False
_ASYNC_LOOPS: set[int] = set()
_FAULT_STREAM: Any = None

# Handlers are tagged so repeated plugin/module initialization is idempotent.
_HANDLER_TAG = "_lucifer_error_monitor_handler"


def _format_exception(
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    exc_tb: TracebackType | None,
) -> tuple[type[BaseException], BaseException, TracebackType | None] | None:
    if exc_type is None or exc_value is None:
        return None
    return (exc_type, exc_value, exc_tb)


def _log_uncaught(
    label: str,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    exc_tb: TracebackType | None,
) -> None:
    """Log an uncaught exception without allowing logging failures to recurse."""
    try:
        exc_info = _format_exception(exc_type, exc_value, exc_tb)
        if exc_info is None:
            _LOG.error("%s (exception details unavailable).", label)
        else:
            _LOG.critical("%s", label, exc_info=exc_info)
    except BaseException:
        # Last-resort output: avoid raising from an exception hook.
        try:
            sys.stderr.write(f"[Lucifer.ErrorMonitor] {label}\n")
            sys.stderr.flush()
        except BaseException:
            pass


def _install_exception_hooks() -> None:
    """Install main-thread, thread, and unraisable exception hooks."""
    original_sys_hook = sys.excepthook

    if not getattr(original_sys_hook, "_lucifer_error_monitor", False):
        def _sys_excepthook(
            exc_type: type[BaseException],
            exc_value: BaseException,
            exc_tb: TracebackType | None,
        ) -> None:
            _log_uncaught("Uncaught exception on the main Python thread", exc_type, exc_value, exc_tb)
            # Preserve a pre-existing non-default application hook.
            if original_sys_hook is not sys.__excepthook__:
                try:
                    original_sys_hook(exc_type, exc_value, exc_tb)
                except BaseException:
                    pass

        _sys_excepthook._lucifer_error_monitor = True  # type: ignore[attr-defined]
        sys.excepthook = _sys_excepthook

    thread_hook = getattr(threading, "excepthook", None)
    if callable(thread_hook) and not getattr(thread_hook, "_lucifer_error_monitor", False):
        original_thread_hook = thread_hook
        default_thread_hook = getattr(threading, "__excepthook__", None)

        def _thread_excepthook(args: Any) -> None:
            thread_name = getattr(getattr(args, "thread", None), "name", "unknown")
            _log_uncaught(
                f"Uncaught exception in background thread {thread_name!r}",
                getattr(args, "exc_type", None),
                getattr(args, "exc_value", None),
                getattr(args, "exc_traceback", None),
            )
            if original_thread_hook is not default_thread_hook:
                try:
                    original_thread_hook(args)
                except BaseException:
                    pass

        _thread_excepthook._lucifer_error_monitor = True  # type: ignore[attr-defined]
        threading.excepthook = _thread_excepthook  # type: ignore[assignment]

    unraisable_hook = getattr(sys, "unraisablehook", None)
    default_unraisable_hook = getattr(sys, "__unraisablehook__", None)
    if callable(unraisable_hook) and not getattr(unraisable_hook, "_lucifer_error_monitor", False):
        original_unraisable_hook = unraisable_hook

        def _unraisable_excepthook(args: Any) -> None:
            obj = getattr(args, "object", None)
            try:
                object_description = repr(obj)
            except BaseException:
                object_description = "<repr failed>"
            label = (
                "Unraisable exception"
                f" (message={getattr(args, 'err_msg', None)!r}, "
                f"object={object_description})"
            )
            _log_uncaught(
                label,
                getattr(args, "exc_type", None),
                getattr(args, "exc_value", None),
                getattr(args, "exc_traceback", None),
            )
            if original_unraisable_hook is not default_unraisable_hook:
                try:
                    original_unraisable_hook(args)
                except BaseException:
                    pass

        _unraisable_excepthook._lucifer_error_monitor = True  # type: ignore[attr-defined]
        sys.unraisablehook = _unraisable_excepthook


def install_asyncio_handler(loop: asyncio.AbstractEventLoop | None = None) -> bool:
    """Install an exception handler on a known asyncio event loop.

    Call this with the actual loop if the server exposes one. During ordinary
    plugin startup there may be no running asyncio loop on the current thread.
    """
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

    loop_id = id(loop)
    if loop_id in _ASYNC_LOOPS:
        return True

    previous_handler = loop.get_exception_handler()

    def _asyncio_exception_handler(
        current_loop: asyncio.AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        message = str(context.get("message", "Unhandled asyncio exception"))
        exc = context.get("exception")
        if isinstance(exc, BaseException):
            _log_uncaught(
                f"Asyncio event-loop error: {message}",
                type(exc),
                exc,
                exc.__traceback__,
            )
        else:
            details = {
                key: repr(value)
                for key, value in context.items()
                if key not in {"message", "exception"}
            }
            _LOG.error("Asyncio event-loop error: %s; context=%s", message, details)

        # Preserve an application-specific handler, if one was installed.
        if previous_handler is not None:
            try:
                previous_handler(current_loop, context)
            except BaseException:
                _LOG.exception("The previous asyncio exception handler failed.")

    loop.set_exception_handler(_asyncio_exception_handler)
    _ASYNC_LOOPS.add(loop_id)
    return True


def _install_asyncio_handler_if_running() -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        install_asyncio_handler(loop)
    except Exception:
        _LOG.exception("Could not install the asyncio exception handler.")


def _install_logging_handlers() -> None:
    """Ensure WARNING-and-higher Python logs go to the server terminal."""
    root = logging.getLogger()
    root.setLevel(min(root.level or logging.WARNING, logging.WARNING))

    # Reuse an existing adequate stderr/stdout handler when the application
    # already has one, avoiding duplicate console lines.
    has_console = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and handler.level <= logging.WARNING
        for handler in root.handlers
    )
    if not has_console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        setattr(console_handler, _HANDLER_TAG, True)
        root.addHandler(console_handler)


def _install_fault_handler() -> None:
    """Send fatal Python fault tracebacks directly to the server terminal."""
    global _FAULT_STREAM

    if _FAULT_STREAM is not None:
        return

    try:
        # sys.stderr is inherited by the headless subprocess from its launcher,
        # so in the normal tmux setup this writes directly into that session.
        faulthandler.enable(file=sys.stderr, all_threads=True)
        _FAULT_STREAM = sys.stderr
    except (OSError, RuntimeError, ValueError):
        try:
            _LOG.exception("Could not enable terminal faulthandler output.")
        except BaseException:
            pass

def dump_thread_tracebacks() -> bool:
    """Print all available Python thread tracebacks to the server terminal."""
    if _FAULT_STREAM is None:
        _LOG.error("Cannot dump thread tracebacks: terminal faulthandler is unavailable.")
        return False
    try:
        faulthandler.dump_traceback(file=_FAULT_STREAM, all_threads=True)
        _FAULT_STREAM.flush()
        _LOG.warning("Printed a manual all-thread traceback dump to the server terminal.")
        return True
    except Exception:
        _LOG.exception("Could not write a manual thread traceback dump.")
        return False


def install() -> bool:
    """Install terminal diagnostics once. Safe to call repeatedly at startup."""
    global _INSTALLED

    if _INSTALLED:
        return True

    try:
        _install_logging_handlers()
        _install_exception_hooks()
        logging.captureWarnings(True)

        # Make common diagnostic warnings visible once per source location.
        warnings.filterwarnings("default", category=DeprecationWarning)
        warnings.filterwarnings("default", category=RuntimeWarning)
        warnings.filterwarnings("default", category=ResourceWarning)

        _install_fault_handler()
        _install_asyncio_handler_if_running()
        _INSTALLED = True

        print(
            "[INFO] Lucifer.ErrorMonitor: terminal diagnostics installed; "
            "Python warnings/errors and fatal Python fault tracebacks will be "
            "printed to the server terminal (no diagnostic files are created).",
            file=sys.stderr,
            flush=True,
        )
        return True
    except Exception:
        try:
            sys.stderr.write(
                "[Lucifer.ErrorMonitor] Terminal diagnostics initialization failed.\n"
            )
            import traceback
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
        except BaseException:
            pass
        return False
