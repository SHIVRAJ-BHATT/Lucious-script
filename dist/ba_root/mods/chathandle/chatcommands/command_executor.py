# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
# Owner/Editor: Lucifer

"""Dispatch public and permission-protected commands."""

from __future__ import annotations

import logging
import shlex

from chathandle.chatcommands import handlers
from chathandle.chatcommands.commands import normal_commands, management, cheats, fun, shop

_LOG = logging.getLogger("Lucifer.CommandExecutor")
_RED = (1.0, 0.18, 0.18)
_INFO = (0.45, 0.82, 1.0)

# Preserve the original dispatch precedence. If two modules register the same
# command or alias, the first module in this tuple continues to win.
_MODULES = (shop, normal_commands, management, cheats, fun)


def _registered_names(module: object) -> set[str]:
    """Return normalized command names and aliases declared by a module."""
    result: set[str] = set()

    for attribute in ("Commands", "CommandAliases"):
        try:
            names = getattr(module, attribute, ())
        except Exception:
            _LOG.exception(
                "Could not read %s from command module %r.", attribute, module
            )
            continue

        if names is None:
            continue

        # A single string is one command name, not an iterable of characters.
        if isinstance(names, str):
            names = (names,)

        try:
            iterator = iter(names)
        except TypeError:
            _LOG.warning(
                "Ignoring invalid %s registry on command module %r; expected an iterable.",
                attribute,
                module,
            )
            continue
        except Exception:
            _LOG.exception(
                "Could not iterate %s from command module %r.", attribute, module
            )
            continue

        try:
            for name in iterator:
                normalized = str(name).casefold()
                if normalized:
                    result.add(normalized)
        except Exception:
            _LOG.exception(
                "Error while reading %s from command module %r.", attribute, module
            )

    return result


def _build_command_lookup() -> dict[str, object]:
    """Build a command-to-module table while preserving module precedence."""
    lookup: dict[str, object] = {}
    for module in _MODULES:
        for name in _registered_names(module):
            lookup.setdefault(name, module)
    return lookup


_COMMAND_LOOKUP: dict[str, object] = {}


def refresh_command_registry() -> None:
    """Refresh the cached command table after runtime registry changes.

    Command names are normally static, so dispatch uses a prebuilt lookup
    instead of rebuilding sets for every command. Call this function if some
    other code changes a module's Commands or CommandAliases at runtime.
    """
    global _COMMAND_LOOKUP
    _COMMAND_LOOKUP = _build_command_lookup()


# Command modules are imported above, so their initial registries are ready.
refresh_command_registry()


def _execute_impl(msg: str, clientid: int) -> str | None:
    """Run normal command dispatch; outer execute() catches unexpected failures."""
    if not isinstance(msg, str) or not msg.startswith("/"):
        return msg

    try:
        parts = shlex.split(msg[1:])
    except ValueError:
        handlers.send(
            "\ue048  Invalid command syntax.  \ue049",
            clientid,
            color=_RED,
        )
        return None

    if not parts:
        handlers.send(
            "\ue043  Type a command after / to continue.  \ue049",
            clientid,
            color=_INFO,
        )
        return None

    command = parts[0].casefold()
    arguments = parts[1:]
    account_id = handlers.clientid_to_accountid(clientid)

    # Commands in the paid allowlist are free for players who already have
    # role permission. Players without permission may use the same command by
    # paying its configured price. If permission checking itself errors, fail
    # closed: do not execute and do not charge.
    try:
        if shop.is_paid_command(command):
            paid_module = shop.find_paid_command_module(command, _MODULES)
            if paid_module is not None:
                already_permitted = shop.has_command_permission(
                    account_id, command, paid_module
                )
                if not already_permitted:
                    shop.execute_paid_command(
                        command, arguments, clientid, account_id, paid_module
                    )
                    return None
                # A permitted player falls through to the normal protected
                # command path below and is not charged any coins.
    except Exception:
        _LOG.exception("Paid-command permission/routing failed for /%s.", command)
        handlers.send(
            "\ue062  \ue04c Command permission check failed; no coins were charged. \ue04d  \ue062",
            clientid,
            color=_RED,
        )
        return None

    module = _COMMAND_LOOKUP.get(command)

    if module is None:
        handlers.send(
            f"\ue048  Unknown command: /{command}  \ue043  Check the command name.",
            clientid,
            color=_RED,
        )
        return None

    # Public commands work without a role. Economy commands send their own
    # formatted feedback, so avoid adding an extra generic acknowledgement.
    if module is normal_commands:
        try:
            completed = bool(
                normal_commands.ExcelCommand(
                    command, arguments, clientid, account_id
                )
            )
        except Exception:
            _LOG.exception("Public command /%s failed.", command)
            handlers.send(
                "\ue048  \ue04c Command failed. \ue043 Check the server terminal. \ue04d  \ue062",
                clientid,
                color=_RED,
            )
            return None

        if completed:
            handlers.send_command_accepted(account_id, clientid)
        return None

    if module is shop:
        try:
            shop.ExcelCommand(command, arguments, clientid, account_id)
        except Exception:
            _LOG.exception("Economy command /%s failed.", command)
            handlers.send(
                "\ue062  \ue04c Economy command failed. Check the server terminal. \ue04d  \ue062",
                clientid,
                color=_RED,
            )
        return None

    # Protected commands require a valid V2 identity and permission.
    # Alias commands may inherit permission from their canonical command name.
    permitted = handlers.check_permissions(account_id, command)
    canonicalizer = getattr(module, "canonical_command", None)
    if not permitted and callable(canonicalizer):
        canonical = str(canonicalizer(command)).casefold()
        if canonical != command:
            permitted = handlers.check_permissions(account_id, canonical)

    if not permitted:
        handlers.send_denied(clientid)
        return None

    try:
        completed = bool(
            module.ExcelCommand(
                command, arguments, clientid, account_id
            )
        )
        if completed:
            handlers.send_command_accepted(account_id, clientid)
    except Exception:
        _LOG.exception("Protected command /%s failed.", command)
        handlers.send(
            "\ue048  Command failed.  \ue043  Check the server terminal.",
            clientid,
            color=_RED,
        )

    return None


def execute(msg: str, clientid: int) -> str | None:
    """Execute a slash command with a final diagnostic safety boundary.

    Existing command-specific handlers retain their current behavior. This
    outer boundary catches unexpected errors in command parsing, identity
    lookup, registration lookup, permission checks, or feedback delivery that
    occur outside those local handlers. It does not catch BaseException.
    """
    try:
        return _execute_impl(msg, clientid)
    except Exception:
        command_context: str
        if isinstance(msg, str):
            command_context = msg[:160].replace("\n", "\\n")
        else:
            command_context = f"<non-string message type={type(msg).__name__}>"

        _LOG.exception(
            "Unhandled command-dispatch failure for client %r; input=%r.",
            clientid,
            command_context,
        )

        # Feedback is best-effort. A second failure must not hide the original
        # dispatcher exception or produce an unhandled error from this layer.
        try:
            handlers.send(
                "\ue048  Command failed unexpectedly. Check the server terminal.  \ue062",
                clientid,
                color=_RED,
            )
        except Exception:
            _LOG.exception(
                "Could not send command-failure feedback to client %r.",
                clientid,
            )
        return None
