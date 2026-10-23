# Released under the MIT License. See LICENSE for details.
"""In-game automation helpers for the opt-in control channel.

.. warning::

   **Unstable, unsupported API.** May change or be removed without
   notice. No backward-compatibility guarantees across versions.
   Use at your own risk.

Automation is an optional dev tool that lets external tools (scripts,
test harnesses, Claude Code, etc.) drive a running game in-process.
These helpers run wherever driver-supplied Python is exec'd: over the
automation channel to the game's basn node (see
:mod:`baplus._automationsession` and ``tools/pcommand
automation_drive``), or through the cloud console.

Gating:

* **Compile time** — the whole subsystem is gated on the
  ``BA_ENABLE_AUTOMATION`` build define (CMake:
  ``-DENABLE_AUTOMATION=ON``). When off, no native hooks are compiled
  in, and the helpers below emit a ``[automation] <tag> fail
  not_compiled_in`` line if called.
* **Runtime** — in builds that compiled it in, the native
  capabilities are stood up on developer builds. Offering the game
  up for *remote* driving additionally requires the channel's own
  runtime opt-in (``BA_AUTOMATION_CHANNEL``; ``tools/pcommand
  test_game_run --automation-channel`` sets it).

This module holds the UI-agnostic helpers. Anything that reaches
into the live widget tree (press/scroll by id or label, widget
inspection, waits) lives in :mod:`bauiv1._automation` so that
base-only spinoffs don't pull in a ``bauiv1`` dependency.

Results of every helper are reported via a single standardized log
line of the form ``[automation] <tag> <status> <payload>`` on the
``ba.app`` logger — external watchers grep that prefix rather than
parse free-form output. ``[automation]`` is the stable marker; the
choice of logger is incidental.
"""

import logging
from typing import TYPE_CHECKING

import _babase

if TYPE_CHECKING:
    from typing import Any, Callable

automationlog = logging.getLogger('ba.app')

# The automation_* native hooks on _babase are only compiled in when
# BA_ENABLE_AUTOMATION is set. Route dev-hook calls through this
# Any-typed alias so mypy stays happy in builds where the flag is off
# and the stubs reflect that. At runtime we check for the attribute
# explicitly before calling, so public builds emit a structured
# ``not_compiled_in`` failure instead of raising AttributeError.
_badev: Any = _babase


#: While a driver's exec runs, the result lines it emits are collected
#: here too, so they go back to the driver with the exec's own result
#: instead of only reaching the device log (which a driver only sees
#: with ``--log``). None when no exec is running.
_g_exec_results: list[tuple[str, str, str]] | None = None


def begin_exec_result_capture() -> None:
    """Start collecting emitted result lines for a driver's exec.

    :meta private:
    """
    global _g_exec_results  # pylint: disable=global-statement
    _g_exec_results = []


def end_exec_result_capture() -> list[tuple[str, str, str]]:
    """Stop collecting and return the (tag, status, payload) results.

    :meta private:
    """
    global _g_exec_results  # pylint: disable=global-statement
    results = _g_exec_results or []
    _g_exec_results = None
    return results


def _emit(
    tag: str, status: str, payload: str = '', *, redact_log: bool = False
) -> None:
    """Print the standard ``[automation] <tag> <status> <payload>`` line.

    Always logs at INFO so external watchers don't need to opt in to a
    specific log level. Use ``status`` values like ``ok``, ``fail``,
    ``not_implemented``; ``payload`` is a free-form trailing string
    callers can include identifiers, timings, error messages, etc. in.
    Lines emitted while a driver's exec runs also go back to that
    driver as results. With ``redact_log``, the payload goes to the
    driver only and the log line says it was redacted (for secrets).
    """
    if _g_exec_results is not None:
        _g_exec_results.append((tag, status, payload))
    if redact_log:
        automationlog.info('[automation] %s %s (redacted)', tag, status)
    elif payload:
        automationlog.info('[automation] %s %s %s', tag, status, payload)
    else:
        automationlog.info('[automation] %s %s', tag, status)


def available() -> bool:
    """Whether automation was compiled into this build.

    The native hooks are simply absent otherwise, so this is a
    legitimate question to ask in any build -- unlike a build-flavor
    attribute, which may not exist to be read at all.

    Exists for callers outside babase (the automation channel lives
    in baplus, which may not reach the private ``_babase`` module) so
    they need not reimplement the check.
    """
    return hasattr(_babase, 'automation_capture_screenshot')


def ping(tag: str = 'ping') -> None:
    """Round-trip sanity check: emits ``[automation] <tag> ok pong``.

    Useful as a "is the channel alive?" probe at the start of a test
    script; if you see the matching line in the log within a tick of
    sending it, the automation dispatch path is all healthy.
    """
    _emit(tag, 'ok', 'pong')


def fps(tag: str = 'fps') -> None:
    """Report the current render frame rate.

    Emits ``[automation] <tag> ok <fps>``, where ``<fps>`` is the
    number of frames rendered over the most recent one-second stats
    window -- the same value the in-game 'Show FPS' display shows,
    tracked whether or not that display is enabled. Always ``0`` in
    headless builds, which render no frames. Note the window updates
    once per second, so a just-launched app can briefly report ``0``.
    """
    _emit(tag, 'ok', str(_babase.get_last_fps()))


def shutdown(tag: str = 'shutdown') -> None:
    """Cleanly quit the running game.

    Wraps ``_babase.quit()`` so external scripts have a single
    consistent way to end an automated session. Emits the marker
    *before* triggering shutdown so the watcher can still see it.
    """
    _emit(tag, 'ok')
    _babase.quit()


def screenshot(path: str, tag: str = 'screenshot') -> None:
    """Save the next-rendered framebuffer as an image file.

    Fire-and-forget — the actual capture happens in the graphics
    context between frames; a ``[automation] <tag> ok|fail <details>``
    line lands in the log (``ba.app``) when it completes.

    The path's extension picks the format — **prefer ``.jpg``**: it
    gets lossy JPEG, which for photographic game frames is a fraction
    of PNG's size (what makes captures cheap to store and move over
    the wire). Any other extension gets lossless PNG, which should
    only be used where pixel-perfect data is actually needed
    (exact-color assertions, render-output comparisons, etc.).

    This writes on the *device*. A remote driver that wants the bytes
    should use ``automation_drive --screenshot`` instead, which
    captures to a temp file and ships the image back.

    Path resolution:

    * **Absolute path** (``/tmp/x.jpg``, ``/Users/.../shot.jpg``) —
      used as-is.
    * **Relative path or bare filename** (``home.jpg``,
      ``menus/main.jpg``) — resolved under ``screenshots/`` beneath
      the process cwd; subdirs are created as needed.

    Native-resolution capture: on retina displays the image will be
    at physical pixel dimensions (e.g. 2880x1800), not logical
    window size. Resize externally if you need a specific dpi.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``.
    """
    import os

    if not hasattr(_babase, 'automation_capture_screenshot'):
        _emit(tag, 'fail', 'not_compiled_in')
        return

    if os.path.isabs(path):
        abs_path = path
    else:
        screenshots_dir = _automation_screenshots_dir()
        os.makedirs(screenshots_dir, exist_ok=True)
        abs_path = os.path.join(screenshots_dir, path)
        # Ensure subdirs in the relative path exist too
        # (e.g. screenshot('menus/main.jpg')).
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    _badev.automation_capture_screenshot(path=abs_path, tag=tag)


def screenshot_sequence(
    path: str,
    count: int,
    interval: float,
    *,
    delay: float = 0.0,
    tag: str = 'screenshot_sequence',
) -> None:
    """Capture a timed run of screenshots, for checking animations.

    Takes ``count`` captures ``interval`` seconds apart (app time),
    starting ``delay`` seconds from now, into the directory ``path``
    (resolved as :func:`screenshot` resolves paths) as
    ``0000.jpg``, ``0001.jpg``, ... Starting with no delay and then
    triggering an animation in the same exec puts frame 0 at the
    trigger.

    Each capture lands on the next frame rendered after its timer,
    so spacing jitters by up to a frame; ``ffmpeg -framerate N -i
    %04d.jpg`` or a contact sheet of them reads the motion fine.
    Emits ``[automation] <tag> ok <count> <dir>`` once all are
    scheduled (captures report their own lines as ``<tag>``).
    """
    import os

    import babase

    if not hasattr(_babase, 'automation_capture_screenshot'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    if count < 1 or interval <= 0.0 or delay < 0.0:
        _emit(tag, 'fail', 'bad_args')
        return
    dirpath = (
        path
        if os.path.isabs(path)
        else os.path.join(_automation_screenshots_dir(), path)
    )
    os.makedirs(dirpath, exist_ok=True)
    for i in range(count):
        babase.apptimer(
            delay + i * interval,
            babase.CallStrict(
                screenshot, os.path.join(dirpath, f'{i:04d}.jpg'), tag=tag
            ),
        )
    _emit(tag, 'ok', f'{count} {dirpath}')


def _automation_screenshots_dir() -> str:
    """Dir a relative screenshot path resolves under.

    ``screenshots/`` beneath the process cwd. This is only for code
    that execs ``screenshot('foo.jpg')`` on the device directly; a
    remote driver's ``--screenshot`` captures to a temp file and
    ships the bytes back instead (see ``baplus._automationsession``).
    """
    import os

    return os.path.join(os.getcwd(), 'screenshots')


def drag_at(
    x: float,
    y: float,
    x2: float,
    y2: float,
    *,
    steps: int = 8,
    cancel: bool = False,
    tag: str = 'drag',
) -> None:
    """Synthesize a mouse drag between two virtual-screen points.

    Presses at ``(x, y)``, delivers ``steps`` interpolated motion
    events towards ``(x2, y2)``, and releases there -- all through the
    normal UI dispatch path, so anything with real press/drag/release
    behavior (the draggable dev-console button, for one) responds the
    way it would to a real pointer. Same coordinate system as
    :func:`click_at` (origin bottom-left, y up).

    With ``cancel`` the drag ends in a mouse-cancel instead of a
    release: what the UI sees when the OS takes a touch gesture away
    mid-drag (an iPad turning a drag from the top of the screen into
    a window drag, say). Nothing should activate, and anything
    tracking the drag should put itself back.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_drag_at_virtual'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_drag_at_virtual(
            x=x, y=y, x2=x2, y2=y2, steps=steps, cancel=cancel
        )
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise
    _emit(
        tag,
        'ok',
        f'{x:.0f},{y:.0f} -> {x2:.0f},{y2:.0f}'
        + (' (canceled)' if cancel else ''),
    )


def mouse_button_at(
    x: float,
    y: float,
    pressed: bool,
    *,
    tag: str = 'mousebutton',
) -> None:
    """Synthesize one half of a mouse click at virtual-screen coords.

    Same coordinate system as :func:`click_at`. Unlike that function --
    which presses and releases in a single dispatch, so no frame ever
    renders between -- this leaves the button held until a matching
    ``pressed=False`` call. That makes it the only way to observe a
    widget's *held* appearance from automation: a pressed button's
    glow, a slider's grabbed nub.

    Always pair a press with a release; leaving one outstanding leaves
    the UI thinking a button is down.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_mouse_button_at_virtual'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_mouse_button_at_virtual(
            button=1, x=x, y=y, pressed=pressed
        )
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise
    updown = 'down' if pressed else 'up'
    _emit(tag, 'ok', f'{updown} @ {x:.0f},{y:.0f}')


def ui_nav(direction: str, tag: str = 'uinav') -> None:
    """Synthesize a UI-navigation event.

    ``direction`` is one of ``'left'``, ``'right'``, ``'up'``,
    ``'down'``, ``'activate'``, ``'cancel'`` -- the messages arrow keys
    and controller d-pads produce. Delivered through the normal UI
    dispatch path, so selection order and message claiming behave as
    they would for real input.

    This is the only way to exercise widgets that consume directional
    messages rather than passing them to navigation; a slider adjusting
    its value on left/right, for one, is unreachable via
    :func:`click_at` or :func:`drag_at`.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_ui_nav'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_ui_nav(direction=direction)
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise
    _emit(tag, 'ok', direction)


def window_size(tag: str = 'window_size') -> None:
    """Report the app's current OS-window size.

    Emits ``[automation] <tag> ok <W>x<H>`` (logical units, which is
    what :func:`set_window_size` accepts -- on retina displays the
    backing framebuffer, and thus screenshot captures, will be larger).
    Only functions where the app runs in a desktop window (the SDL /
    cmake builds); elsewhere emits ``fail not_supported``.

    Fire-and-forget -- the query runs on the main thread and the result
    line lands in the log shortly after.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_get_window_size'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_get_window_size(tag=tag)
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise


def set_window_size(
    width: int, height: int, *, tag: str = 'set_window_size'
) -> None:
    """Resize the app's OS window.

    Takes logical units (the same ones :func:`window_size` reports).
    Only functions where the app runs in a desktop window (the SDL /
    cmake builds) and only in windowed mode; emits ``fail fullscreen``
    or ``fail not_supported`` otherwise. The resize goes through the
    same OS window-resized path a hand-drag does, so UI reflow /
    aspect-clamp behavior gets exercised for real -- useful for
    checking layouts at multiple window shapes within one run.

    Fire-and-forget -- the resize runs on the main thread and a
    ``[automation] <tag> ok <W>x<H>`` line reporting the size actually
    applied (the OS may clamp; e.g. macOS to display bounds) lands in
    the log shortly after. Give the UI a beat to reflow before
    capturing a screenshot of the result.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_set_window_size'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_set_window_size(width=width, height=height, tag=tag)
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise


def _evaluate_lstr_json(raw: str) -> str:
    """Evaluate a JSON-encoded :class:`babase.Lstr` blob to its display text.

    Exposed so :mod:`bauiv1._automation` can flatten localized labels
    without reaching into the private ``_babase`` module directly.
    """
    return str(_babase.evaluate_lstr(raw))


def click_at(x: float, y: float, *, tag: str = 'click') -> None:
    """Synthesize a mouse click at virtual-screen coords.

    Coords are absolute virtual-screen, origin **bottom-left**, y
    growing upward (the OpenGL convention the rest of the automation
    surface uses). To convert from a screenshot pixel ``(px, py)``
    measured top-left in an image of size ``(iw, ih)``::

        vx = px * vw / iw
        vy = vh - py * vh / ih

    where ``(vw, vh)`` is :func:`babase.get_virtual_screen_size()`.

    Prefer :func:`bauiv1._automation.press_by_id` or ``press_by_label``
    whenever the target is a widget you can name -- they are stable
    against layout changes, where coords are not. This exists for the
    cases those cannot reach: overlays and popups that never appear in
    the main-window widget tree (the get-remote window, for one), and
    non-widget hit targets.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_press_at_virtual'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_press_at_virtual(button=1, x=x, y=y)
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise
    _emit(tag, 'ok', f'@ {x:.0f},{y:.0f}')


def key_press(keycode: int, *, tag: str = 'key') -> None:
    """Synthesize a key press for a BA keycode (release with key_release).

    Routes through the same path OS key events take, so a keyboard
    input device sees it: pressing a join key (13, return, works on
    the main keyboard) in a lobby requests a player exactly like a
    real keypress. Printable keys use their ASCII value. On platforms
    that only create keyboard devices when a hardware keyboard is
    attached (iOS), call :func:`ensure_keyboard` first.

    For modifier combos, press the modifier key first and release it
    last: the engine derives modifier state from held keys, so a
    held left-alt (``226 | (1 << 30)``) turns a delete (127) into
    alt+delete. Non-printable keycodes are the scancode with bit 30
    set (left-ctrl 224, left-shift 225, left-alt 226, left-gui 227).
    Space the events across frames (an apptimer chain), since they
    are queued.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``.
    """
    if not hasattr(_babase, 'automation_key_event'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    _badev.automation_key_event(keycode=keycode, down=True)
    _emit(tag, 'ok', f'press {keycode}')


def key_release(keycode: int, *, tag: str = 'key') -> None:
    """Synthesize a key release for a BA keycode (see :func:`key_press`)."""
    if not hasattr(_babase, 'automation_key_event'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    _badev.automation_key_event(keycode=keycode, down=False)
    _emit(tag, 'ok', f'release {keycode}')


def ensure_keyboard(*, tag: str = 'keyboard') -> None:
    """Make sure keyboard input devices exist, creating them if needed.

    iOS and similar only create keyboard devices when a hardware
    keyboard connects; this forces them into existence so
    :func:`key_press` has somewhere to land. Newly created devices
    appear a frame later, so wait briefly before pressing keys. Emits
    ``ok created`` or ``ok existed``.
    """
    if not hasattr(_babase, 'automation_ensure_keyboard'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    created = _badev.automation_ensure_keyboard()
    _emit(tag, 'ok', 'created' if created else 'existed')


def scroll_at(
    x: float,
    y: float,
    dx: float = 0.0,
    dy: float = 0.0,
    *,
    tag: str = 'scroll',
) -> None:
    """Synthesize a mouse-wheel scroll at virtual-screen coords.

    ``dy`` is vertical wheel units — positive scrolls the *content* down
    (wheel up), negative scrolls content up (wheel down) — the sign
    convention mirrors real mouse-wheel events. Same story for ``dx``
    on the horizontal axis.

    Cursor is moved to ``(x, y)`` first since wheel events dispatch to
    whatever widget is under the cursor, so place the coords over the
    scrollable area you actually want to scroll (a character row, the
    outer store container, etc.) rather than off in empty space.

    Typical magnitudes: one physical "notch" of a mouse wheel delivers
    roughly 1.0 unit. Ballistica's scrollwidgets multiply that
    internally, so 1.0–3.0 is a reasonable step; 10+ is a big jump.

    Emits ``[automation] <tag> fail not_compiled_in`` when the build
    was made without ``BA_ENABLE_AUTOMATION``, or ``fail
    headless_mode`` when called from a headless build.
    """
    if not hasattr(_babase, 'automation_scroll_at_virtual'):
        _emit(tag, 'fail', 'not_compiled_in')
        return
    try:
        _badev.automation_scroll_at_virtual(x=x, y=y, dx=dx, dy=dy)
    except RuntimeError as exc:
        if 'headless' in str(exc).lower():
            _emit(tag, 'fail', 'headless_mode')
            return
        raise
    _emit(tag, 'ok', f'@ {x:.0f},{y:.0f} d=({dx:+.2f},{dy:+.2f})')


#: App-config key holding a registered automation device's key.
DEVICE_KEY_CONFIG_KEY = 'Automation Device Key'

_g_registration_changed_call: Callable[[], None] | None = None


def set_registration_changed_call(call: Callable[[], None] | None) -> None:
    """Set what to call when this device's registration changes.

    Installed by the automation channel (``baplus._automationsession``)
    so we can tell it without reaching up a layer.
    """
    global _g_registration_changed_call  # pylint: disable=global-statement
    _g_registration_changed_call = call


def automation_device_id_for_key(key: str) -> str:
    """The non-secret automation-device id for a device key.

    See :func:`bacommon.automationchannel.automation_device_id_for_key`.
    """
    from bacommon.automationchannel import (
        automation_device_id_for_key as _device_id_for_key,
    )

    return _device_id_for_key(key)


def stored_device_key() -> str | None:
    """This device's registered automation key, if it has one.

    Only meaningful in builds with automation compiled in (see
    :func:`available`); anywhere else a stored key does nothing.
    """
    key = _babase.app.config.get(DEVICE_KEY_CONFIG_KEY)
    if not isinstance(key, str) or not _is_valid_device_key(key):
        return None
    return key


def _is_valid_device_key(key: str) -> bool:
    """Whether a string is shaped like a device key (128-bit hex)."""
    return len(key) == 32 and all(c in '0123456789abcdef' for c in key)
    # __PUBSYNC_STRIP_BEGIN__
    # (Begins inside the function above so the blank lines before the
    # next one are stripped too; the public file must end cleanly.)


def register_automation_device(
    key: str | None = None, *, tag: str = 'register'
) -> None:
    """Make this device a registered automation device.

    Stores a persistent automation key in app config, overwriting any
    existing one -- so running this again revokes the old key and
    issues a new one. A stored key is the device's standing opt-in to
    being driven: from then on it offers an automation channel on
    every launch (no ``BA_AUTOMATION_CHANNEL`` needed) and tells the
    cloud where to find it, so a driver holding the key can reach it
    by id (``automation_drive --device``) whether or not anyone
    launched it with a log in view. The key itself is never logged.

    With no ``key``, mints a fresh one and copies it to the clipboard
    for the human to file away; a driver provisioning us passes its
    own. Emits ``[automation] <tag> ok <device-id>``.

    Only exists in automation-enabled builds.
    """
    from secrets import token_hex

    if not available():
        _emit(tag, 'fail', 'not_compiled_in')
        return
    minted = key is None
    if key is None:
        key = token_hex(16)
    key = key.lower()
    if not _is_valid_device_key(key):
        _emit(tag, 'fail', 'bad_key')
        return

    cfg = _babase.app.config
    cfg[DEVICE_KEY_CONFIG_KEY] = key
    cfg.commit()
    device_id = automation_device_id_for_key(key)

    if minted and _babase.clipboard_is_supported():
        _babase.clipboard_set_text(key)
        msg = f'Automation device {device_id} registered; key copied.'
    else:
        msg = f'Automation device {device_id} registered.'
    _babase.screenmessage(msg, color=(0, 1, 0))
    _emit(tag, 'ok', device_id)
    _notify_session()


def unregister_automation_device(*, tag: str = 'unregister') -> None:
    """Forget this device's automation key (revoking it).

    The live channel ends and the device stops offering itself --
    unless it is opted in another way (``BA_AUTOMATION_CHANNEL``), in
    which case it carries on with a per-run key.
    """
    cfg = _babase.app.config
    if DEVICE_KEY_CONFIG_KEY in cfg:
        del cfg[DEVICE_KEY_CONFIG_KEY]
        cfg.commit()
    _babase.screenmessage('Automation device unregistered.')
    _emit(tag, 'ok')
    _notify_session()


def _notify_session() -> None:
    """Let the automation channel know a key appeared, if it can.

    The channel lives in baplus, a layer above us, which installs a
    call via :func:`set_registration_changed_call` when it comes up;
    plus-less builds simply have no channel to tell.
    """
    if _g_registration_changed_call is not None:
        _g_registration_changed_call()


#: How long an automation sign-in waits for the driver's approval.
_SIGN_IN_WAIT_SECONDS = 300.0
_SIGN_IN_POLL_SECONDS = 2.0


class _AutomationSignIn:
    """State of an automation sign-in (see :func:`automation_sign_in`)."""

    def __init__(self) -> None:
        #: requesting | awaiting_approval | signed_in | failed
        self.state = 'requesting'
        self.proxyid: str | None = None
        self.proxykey: str | None = None
        self.error = ''


_g_sign_in: _AutomationSignIn | None = None


def automation_sign_in(*, tag: str = 'signin') -> None:
    """Start a sign-in for an automation driver to approve.

    Requests a sign-in proxy exactly as the account window's browser
    sign-in does, then polls it until approved and signs in with the
    result. Instead of a human approving in a browser, the driver
    reads the proxy id and key via :func:`automation_sign_in_status`
    and has the master server approve it for an account that allows
    automation sign-in (``automation_drive --sign-in-as``). The proxy
    key goes to the driver only; it is never logged.

    Only exists in automation-enabled builds.
    """
    global _g_sign_in  # pylint: disable=global-statement

    plus = _babase.app.plus
    if not available():
        _emit(tag, 'fail', 'not_compiled_in')
        return
    if plus is None:
        _emit(tag, 'fail', 'no_plus')
        return
    state = _AutomationSignIn()
    _g_sign_in = state
    _babase.app.create_async_task(
        _run_sign_in(state), name='automation sign-in'
    )
    _emit(tag, 'ok', 'started')


def automation_sign_in_status(*, tag: str = 'signin_status') -> None:
    """Report the current automation sign-in's state to the driver.

    Emits ``[automation] <tag> <state> ...``; while awaiting approval
    the payload (``<proxyid> <proxykey>``) goes back to the driver only
    and the log line shows it redacted.
    """
    state = _g_sign_in
    if state is None:
        _emit(tag, 'fail', 'not_started')
        return
    if state.state == 'awaiting_approval':
        assert state.proxyid is not None and state.proxykey is not None
        _emit(
            tag,
            state.state,
            f'{state.proxyid} {state.proxykey}',
            redact_log=True,
        )
        return
    _emit(tag, state.state, state.error)


async def _run_sign_in(state: _AutomationSignIn) -> None:
    import asyncio

    import bacommon.cloud
    from efro.error import CommunicationError
    from efro.util import strip_exception_tracebacks

    plus = _babase.app.plus
    assert plus is not None
    try:
        proxy = await plus.cloud.send_message_async(
            bacommon.cloud.LoginProxyRequestMessage()
        )
        state.proxyid = proxy.proxyid
        state.proxykey = proxy.proxykey
        state.state = 'awaiting_approval'

        deadline = _babase.apptime() + _SIGN_IN_WAIT_SECONDS
        while True:
            await asyncio.sleep(_SIGN_IN_POLL_SECONDS)
            if _babase.apptime() >= deadline:
                state.state = 'failed'
                state.error = 'timed_out'
                return
            try:
                status = await plus.cloud.send_message_async(
                    bacommon.cloud.LoginProxyStateQueryMessage(
                        proxyid=proxy.proxyid, proxykey=proxy.proxykey
                    )
                )
            except CommunicationError as exc:
                # Transient connectivity blip; keep polling.
                strip_exception_tracebacks(exc)
                continue
            if status.state is status.State.FAIL:
                state.state = 'failed'
                state.error = 'proxy_failed'
                return
            if status.state is status.State.SUCCESS:
                break

        assert status.credentials is not None
        plus.accounts.set_primary_credentials(status.credentials)
        state.state = 'signed_in'
        state.proxykey = None

        # Courtesy: tell the server we're done with the proxy.
        try:
            await plus.cloud.send_message_async(
                bacommon.cloud.LoginProxyCompleteMessage(proxyid=proxy.proxyid)
            )
        except Exception as exc:  # pylint: disable=broad-except
            strip_exception_tracebacks(exc)
    except Exception as exc:  # pylint: disable=broad-except
        state.state = 'failed'
        state.error = type(exc).__name__
        automationlog.warning('Automation sign-in failed: %r', exc)
        strip_exception_tracebacks(exc)


# __PUBSYNC_STRIP_END__
