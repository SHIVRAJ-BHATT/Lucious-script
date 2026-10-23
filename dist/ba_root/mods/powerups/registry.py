"""Registry for built-in and custom powerup types.

New custom types can register from a Python module under powerups/custom/ by
calling register_powerup("my_type", "My Powerup", factory=MyPowerupImplementation).
A custom implementation declares ``box_type`` (a built-in texture/material to
reuse) and an ``apply_to_spaz(spaz, bs_module=...)`` method. The manager handles
box lifetime, display labels, quota tracking, and pickup routing.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
from typing import Any

_LOG = logging.getLogger("Lucifer.PowerupManager")

_BUILTIN_DISPLAY_NAMES = {
    "triple_bombs": "Triple Bombs",
    "punch": "Boxing Gloves",
    "ice_bombs": "Ice Bombs",
    "impact_bombs": "Impact Bombs",
    "land_mines": "Land Mines",
    "sticky_bombs": "Sticky Bombs",
    "shield": "Shield",
    "health": "Health",
    "curse": "Curse",
}

_REGISTRY: dict[str, dict[str, Any]] = {}
_KEY_RE = re.compile(r"^[a-z0-9_]+$")


def register_powerup(
    powerup_type: str,
    display_name: str | None = None,
    factory: Any = None,
) -> None:
    """Register a powerup type and optional implementation factory.

    ``factory`` is metadata for custom code; the stock PowerupBox constructor
    does not automatically know how to render or apply effects for new types.
    """
    key = str(powerup_type).strip().lower()
    if not key or not _KEY_RE.fullmatch(key):
        raise ValueError(
            "Powerup type must contain only lowercase letters, digits, and underscores."
        )
    existing = _REGISTRY.get(key)
    if existing is not None and existing.get("builtin") and factory is not None:
        raise ValueError(f"Cannot replace built-in powerup type {key!r}.")
    _REGISTRY[key] = {
        "powerup_type": key,
        "display_name": str(display_name or _pretty_name(key)),
        "factory": factory if factory is not None else (existing or {}).get("factory"),
        "builtin": bool((existing or {}).get("builtin", False)),
    }


def _register_builtins() -> None:
    for key, label in _BUILTIN_DISPLAY_NAMES.items():
        _REGISTRY[key] = {
            "powerup_type": key,
            "display_name": label,
            "factory": None,
            "builtin": True,
        }


def _pretty_name(value: str) -> str:
    return " ".join(part.capitalize() for part in str(value).replace("-", "_").split("_"))


def get_powerup_types() -> tuple[str, ...]:
    """Return the currently registered powerup type keys."""
    return tuple(_REGISTRY.keys())


def is_registered(powerup_type: str) -> bool:
    return str(powerup_type) in _REGISTRY


def get_spec(powerup_type: str) -> dict[str, Any] | None:
    """Return a shallow copy of registered metadata, if present."""
    spec = _REGISTRY.get(str(powerup_type))
    return None if spec is None else dict(spec)


def get_display_name(powerup_type: str) -> str:
    spec = _REGISTRY.get(str(powerup_type))
    if spec is not None:
        return str(spec.get("display_name") or _pretty_name(powerup_type))
    return _pretty_name(powerup_type)


def load_custom_modules() -> None:
    """Import .py modules in the custom package; continue if one fails.

    A custom module may register at import time or expose a zero-argument
    ``register()`` function. Invalid custom modules are logged but do not stop
    the built-in powerup manager from loading.
    """
    try:
        from . import custom

        package_path = list(custom.__path__)
        prefix = custom.__name__ + "."
    except Exception:
        _LOG.exception("Could not access powerups.custom package.")
        return

    for module_info in pkgutil.iter_modules(package_path, prefix):
        short_name = module_info.name.rsplit(".", 1)[-1]
        if short_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(module_info.name)
            register_fn = getattr(module, "register", None)
            if callable(register_fn):
                register_fn()
            _LOG.info("Loaded custom powerup module: %s", module_info.name)
        except Exception:
            _LOG.exception("Failed loading custom powerup module %s", module_info.name)


_register_builtins()
