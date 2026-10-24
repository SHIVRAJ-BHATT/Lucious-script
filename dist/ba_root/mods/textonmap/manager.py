# Released under the MIT License. See LICENSE for details.
# ba_meta require api 9
"""TextOnMap manager: switchable HUD variants + team bar + falling bots + random bg."""

from __future__ import annotations

import contextlib
import functools
import importlib
import inspect
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterator

import bascenev1 as bs
import setting

from textonmap import falling_bots, random_bg, team_bar

_LOG = logging.getLogger("Lucious.TextOnMap")
_DIR = Path(__file__).resolve().parent
_STATE_PATH = _DIR / "state.json"
_HUD_ATTR = "_textonmap_hud"
_FEATURES = ("team_bar", "falling_bots", "random_bg")
_DEFAULTS: dict[str, Any] = {
    "active": "text_on_map",
    "team_bar": True,
    "falling_bots": True,
    "random_bg": True,
}
_state: dict[str, Any] | None = None


# ------------------------------------------------------------------ state
def get_state() -> dict[str, Any]:
    global _state
    if _state is None:
        merged = dict(_DEFAULTS)
        try:
            data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update({k: v for k, v in data.items() if k in _DEFAULTS})
        except FileNotFoundError:
            pass
        except Exception:
            _LOG.warning("Could not read state.json; using defaults.",
                         exc_info=True)
        _state = merged
    return _state


def _save() -> None:
    tmp = _STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(get_state(), indent=2), encoding="utf-8")
    os.replace(tmp, _STATE_PATH)


def set_feature(key: str, value: bool) -> None:
    if key not in _FEATURES:
        raise KeyError(key)
    get_state()[key] = bool(value)
    _save()


# --------------------------------------------------------------- variants
def _natural_key(stem: str) -> list:
    # text_on_map2 sorts before text_on_map10.
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", stem)]


def list_variants() -> list[str]:
    return sorted((p.stem for p in _DIR.glob("text_on_map*.py")),
                  key=_natural_key)


def resolve_variant(name: str) -> str | None:
    key = name.strip().casefold()
    if key.endswith(".py"):
        key = key[:-3]
    available = {v.casefold(): v for v in list_variants()}
    if key in available:
        return available[key]
    if key.isdigit():                      # "/txt 2" -> text_on_map2
        found = available.get(f"text_on_map{key}")
        if found is None and key in ("0", "1"):
            found = available.get("text_on_map")   # the un-numbered default
        return found
    return None


def _load_variant(name: str, reload: bool = False):
    full = f"textonmap.{name}"
    module = sys.modules.get(full)
    if module is None:
        importlib.invalidate_caches()
        return importlib.import_module(full)
    return importlib.reload(module) if reload else module


def _call_factory(factory, settings):
    """Call a variant factory with or without the settings argument.

    Lets old variants (``def __init__(self)``) keep working instead of
    crashing with "takes 1 positional argument but 2 were given".
    """
    try:
        inspect.signature(factory).bind(settings)
    except TypeError:
        return factory()
    except ValueError:
        pass                      # signature not introspectable; try normally
    return factory(settings)


@contextlib.contextmanager
def _record_nodes() -> Iterator[list]:
    """Remember every node a variant creates so it can be removed on switch."""
    created: list = []
    real = bs.newnode

    def recording(*args: Any, **kwargs: Any):
        node = real(*args, **kwargs)
        created.append(node)
        return node

    bs.newnode = recording
    try:
        yield created
    finally:
        bs.newnode = real


def _delete_nodes(activity: Any, nodes: list) -> None:
    try:
        with activity.context:
            for node in nodes:
                try:
                    if node is not None and node.exists():
                        node.delete()
                except Exception:
                    pass
    except Exception:
        _LOG.debug("Could not delete HUD nodes.", exc_info=True)


def _teardown_hud(activity: Any) -> None:
    state = getattr(activity, _HUD_ATTR, None)
    setattr(activity, _HUD_ATTR, None)
    if not state:
        return
    # Let the variant stop its own timers and delete timer-made nodes.
    close = getattr(state["instance"], "close", None)
    if callable(close):
        try:
            with activity.context:
                close()
        except Exception:
            _LOG.debug("HUD close() failed.", exc_info=True)
    _delete_nodes(activity, state["nodes"])


def _build_hud(activity: Any, name: str, reload: bool = False) -> None:
    module = _load_variant(name, reload)
    factory = getattr(module, "textonmap", None) or getattr(module, "build", None)
    if not callable(factory):
        raise RuntimeError(f"{name}.py has no textonmap class or build() function")
    settings = setting.get_settings_data()
    with _record_nodes() as nodes:
        try:
            with activity.context:
                instance = _call_factory(factory, settings)
        except Exception:
            _delete_nodes(activity, nodes)
            raise
    setattr(activity, _HUD_ATTR,
            {"name": name, "instance": instance, "nodes": nodes})


def get_instance():
    """Current HUD object (for other mods that used text_on_map.get_instance)."""
    activity = bs.get_foreground_host_activity()
    state = getattr(activity, _HUD_ATTR, None) if activity is not None else None
    return state["instance"] if state else None


# ------------------------------------------------------------ live switch
def _rebuild_current(reload: bool = False) -> bool:
    activity = bs.get_foreground_host_activity()
    if not isinstance(activity, bs.GameActivity):
        return False
    if not activity.has_begun() or activity.has_ended():
        return False
    _teardown_hud(activity)
    name = get_state()["active"]
    if name != "off":
        _build_hud(activity, name, reload)
    return True


def switch(name: str) -> str:
    """Persist the choice and apply it to the running map. Rolls back on error."""
    if name.strip().casefold() == "off":
        canonical = "off"
    else:
        canonical = resolve_variant(name)
        if canonical is None:
            raise ValueError(f"Unknown variant '{name}'")
        _load_variant(canonical, reload=True)   # surfaces syntax errors early

    state = get_state()
    previous = state["active"]
    state["active"] = canonical
    try:
        _rebuild_current(reload=False)
    except Exception:
        _LOG.exception("Variant %s failed to build; rolling back.", canonical)
        state["active"] = previous
        try:
            _rebuild_current(reload=False)
        except Exception:
            _LOG.exception("Rollback to %s also failed.", previous)
        raise RuntimeError(
            f"{canonical} failed to build; kept {previous}. See server log.")
    try:
        _save()
    except Exception:
        # The switch itself worked; only persistence failed.
        _LOG.warning("Could not save state.json.", exc_info=True)
    return canonical


# ------------------------------------------------------------- game hook
def _run(activity: Any, label: str, fn):
    try:
        with activity.context:
            return fn(activity)
    except Exception:
        _LOG.exception("TextOnMap: %s failed.", label)
        return None


def _on_activity_begin(activity: Any) -> None:
    if not isinstance(activity, bs.GameActivity):
        return
    if getattr(activity, "_textonmap_built", False):
        return
    activity._textonmap_built = True
    st = get_state()

    # Creation order = draw order: background first, HUD on top.
    if st["random_bg"]:
        _run(activity, "random background", random_bg.apply)
    if st["falling_bots"]:
        _run(activity, "falling bots", falling_bots.build)
    if st["active"] != "off":
        try:
            _build_hud(activity, st["active"])
        except Exception:
            _LOG.exception("HUD variant %s failed to build.", st["active"])
    if st["team_bar"]:
        bar = _run(activity, "team bar", team_bar.build)
        if bar is not None:
            activity._textonmap_bar = bar   # keeps its refresh timer alive


def install() -> bool:
    original = getattr(bs.Activity, "begin", None)
    if not callable(original):
        _LOG.error("bascenev1.Activity.begin is unavailable.")
        return False
    if not getattr(original, "_textonmap_begin_wrapped", False):
        @functools.wraps(original)
        def wrapped_begin(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            try:
                _on_activity_begin(self)
            except Exception:
                _LOG.exception("TextOnMap begin hook failed.")
            return result

        wrapped_begin._textonmap_begin_wrapped = True
        bs.Activity.begin = wrapped_begin
    try:
        current = bs.get_foreground_host_activity()
        if isinstance(current, bs.GameActivity) and current.has_begun():
            _on_activity_begin(current)
    except Exception:
        _LOG.exception("Could not attach to the running activity.")
    return True
