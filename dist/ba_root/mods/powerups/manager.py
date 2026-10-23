"""Runtime powerup quantity and display manager for Ballistica API 9.

This module patches the existing PowerupBox and PowerupBoxFactory classes in
memory. It does not edit Ballistica's installed source files. Default box
expiration remains controlled by the game; this module only displays the
remaining default lifetime.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import math
import random
import sys
import time
import weakref
from pathlib import Path
from typing import Any

from . import registry

_LOG = logging.getLogger("Lucifer.PowerupManager")
_CONFIG_PATH = Path(__file__).resolve().parent / "powerups.json"
_CENTRAL_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "setting.json"
_POWERUP_EFFECT_DEFAULTS = {
    "enable": False,
    "effect": "spark",
    "continuous": True,
    "intervalSeconds": 0.25,
}
_MANAGER: "PowerupManager | None" = None
_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}
_POWERUP_EFFECT_MODULE_NAME = "_lucifer_powerup_spaz_effects"


def _load_powerup_effect_module() -> Any:
    """Load spazmod/spaz_effects.py without executing spazmod/__init__.py.

    Some existing spazmod package initializers import the effects module as a
    top-level ``spaz_effects`` module. Importing ``spazmod.spaz_effects`` would
    execute that initializer first and fail with ``No module named spaz_effects``.
    Loading the intended file directly avoids that unrelated package import.
    """
    module = sys.modules.get(_POWERUP_EFFECT_MODULE_NAME)
    if module is not None:
        return module

    effects_path = Path(__file__).resolve().parent.parent / "spazmod" / "spaz_effects.py"
    if not effects_path.is_file():
        raise ModuleNotFoundError(
            f"Powerup effects file not found: {effects_path}"
        )

    spec = importlib.util.spec_from_file_location(
        _POWERUP_EFFECT_MODULE_NAME, effects_path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load powerup effects file: {effects_path}")

    module = importlib.util.module_from_spec(spec)
    # Register before execution so module-level introspection works normally.
    sys.modules[_POWERUP_EFFECT_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_POWERUP_EFFECT_MODULE_NAME, None)
        raise

    missing = [
        name for name in ("PowerupEffectController", "normalize_powerup_effect_name")
        if not hasattr(module, name)
    ]
    if missing:
        sys.modules.pop(_POWERUP_EFFECT_MODULE_NAME, None)
        raise ImportError(
            f"{effects_path} is missing required API: {', '.join(missing)}"
        )
    return module


class PowerupManager:
    """Tracks live powerup boxes and builds name/countdown labels."""

    def __init__(self, bs_module: Any, factory_class: type, default_interval: float):
        self.bs = bs_module
        self.factory_class = factory_class
        self.default_interval = max(1.0, float(default_interval))
        self.config = self._load_config()
        self.powerup_effect_config = self._load_powerup_effect_config()
        self._powerup_effect_warning_emitted = False
        self._powerup_effect_import_failed = False
        # Records contain weak refs to actors so we do not keep boxes alive.
        self._active: dict[int, dict[str, Any]] = {}

    def _load_config(self) -> dict[str, Any]:
        try:
            with _CONFIG_PATH.open("r", encoding="utf-8") as infile:
                data = json.load(infile)
            if not isinstance(data, dict):
                raise ValueError("Root JSON value must be an object.")
            if not isinstance(data.get("settings", {}), dict):
                raise ValueError("'settings' must be an object.")
            if not isinstance(data.get("powerups", {}), dict):
                raise ValueError("'powerups' must be an object.")
            data.setdefault("enabled", True)
            data.setdefault("settings", {})
            data.setdefault("powerups", {})
            data["settings"].setdefault("display_powerup_names", True)
            data["settings"].setdefault("display_disappear_timer", True)
            _LOG.info("Loaded powerup configuration from %s", _CONFIG_PATH)
            return data
        except Exception:
            _LOG.exception(
                "Could not read powerups.json; manager is disabled to preserve default behavior."
            )
            return {
                "enabled": False,
                "settings": {
                    "display_powerup_names": True,
                    "display_disappear_timer": True,
                },
                "powerups": {},
            }

    def _load_powerup_effect_config(self) -> dict[str, Any]:
        """Load visual-effect settings from the central mods/setting.json."""
        result = dict(_POWERUP_EFFECT_DEFAULTS)
        try:
            with _CENTRAL_SETTINGS_PATH.open("r", encoding="utf-8") as infile:
                root = json.load(infile)
            if not isinstance(root, dict):
                raise ValueError("setting.json root must be an object")
            raw = root.get("powerupEffects", {})
            if not isinstance(raw, dict):
                raise ValueError("'powerupEffects' must be an object")
            result["enable"] = bool(raw.get("enable", result["enable"]))
            result["effect"] = str(raw.get("effect", result["effect"])).strip().casefold()
            result["continuous"] = bool(raw.get("continuous", result["continuous"]))
            try:
                interval = float(raw.get("intervalSeconds", result["intervalSeconds"]))
            except (TypeError, ValueError):
                interval = float(result["intervalSeconds"])
            # Prevent extremely frequent emissions from creating scene/particle load.
            result["intervalSeconds"] = min(5.0, max(0.15, interval))
            _LOG.info(
                "Loaded powerup effects settings (enabled=%s, effect=%s, continuous=%s, interval=%.2fs).",
                result["enable"], result["effect"], result["continuous"], result["intervalSeconds"],
            )
        except FileNotFoundError:
            # This is an optional enhancement; missing settings preserve old behavior.
            _LOG.info("Central setting.json not found; powerup visual effects are disabled.")
            result["enable"] = False
        except Exception:
            _LOG.exception("Could not load powerupEffects from setting.json; disabling this enhancement.")
            result["enable"] = False
        return result

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def display_names_enabled(self) -> bool:
        return bool(self.config.get("settings", {}).get("display_powerup_names", True))

    def display_timer_enabled(self) -> bool:
        return bool(self.config.get("settings", {}).get("display_disappear_timer", True))

    def current_activity_id(self) -> int:
        try:
            activity = self.bs.getactivity(doraise=False)
        except TypeError:
            try:
                activity = self.bs.getactivity()
            except Exception:
                activity = None
        except Exception:
            activity = None
        return id(activity) if activity is not None else 0

    def activity_id_for_box(self, box: Any) -> int:
        try:
            activity = box.getactivity(doraise=False)
        except Exception:
            activity = None
        if activity is None:
            return self.current_activity_id()
        return id(activity)

    def _setting(self, powerup_type: str) -> dict[str, Any]:
        value = self.config.get("powerups", {}).get(str(powerup_type), {})
        return value if isinstance(value, dict) else {}

    def _is_managed_type(self, powerup_type: str) -> bool:
        return registry.is_registered(powerup_type) or str(powerup_type) in self.config.get("powerups", {})

    def _prune(self, activity_id: int | None = None) -> None:
        for key, record in list(self._active.items()):
            actor = record["ref"]()
            if actor is None:
                self._discard_record(key, delete_visuals=True)
                continue
            if getattr(actor, "_pm_dead", False):
                self._discard_record(key, delete_visuals=True)
                continue
            if activity_id is not None and record["activity_id"] != activity_id:
                continue
            node = getattr(actor, "node", None)
            if node is None:
                self._discard_record(key, delete_visuals=True)
                continue
            try:
                if not node:
                    self._discard_record(key, delete_visuals=True)
            except Exception:
                pass

    def count(self, powerup_type: str, activity_id: int | None = None) -> int:
        if activity_id is None:
            activity_id = self.current_activity_id()
        self._prune(activity_id)
        return sum(
            1
            for record in self._active.values()
            if record["activity_id"] == activity_id
            and record["powerup_type"] == str(powerup_type)
            and record["ref"]() is not None
        )

    def can_spawn(self, powerup_type: str, activity_id: int | None = None) -> bool:
        """Return whether one more box of this type fits its configured limit."""
        key = str(powerup_type)
        if not self.enabled:
            return True
        if not self._is_managed_type(key):
            # Unknown/third-party types remain untouched until registered/configured.
            return True
        setting = self._setting(key)
        if not bool(setting.get("enabled", True)):
            return False
        try:
            quantity = int(setting.get("quantity", 999999))
        except (TypeError, ValueError):
            quantity = 999999
        quantity = max(0, quantity)
        if quantity == 0:
            return False
        return self.count(key, activity_id) < quantity

    def _distribution(self, factory: Any) -> list[str]:
        """Return stock weighted choices plus configured registered custom types."""
        try:
            distribution = [str(key) for key in list(factory._powerupdist)]
        except Exception:
            distribution = [
                str(key)
                for key in registry.get_powerup_types()
                if (registry.get_spec(str(key)) or {}).get("builtin", False)
            ]

        configured = self.config.get("powerups", {})
        if not isinstance(configured, dict):
            return distribution

        # Custom types are appended according to optional spawn_weight. A weight
        # of 1 is one ticket in the weighted pool; no built-in weight is changed.
        for key in registry.get_powerup_types():
            key = str(key)
            spec = registry.get_spec(key)
            if spec is None or spec.get("builtin", False) or key not in configured:
                continue
            setting = configured.get(key, {})
            if not isinstance(setting, dict):
                continue
            try:
                weight = int(setting.get("spawn_weight", 1))
            except (TypeError, ValueError):
                weight = 1
            weight = max(0, min(weight, 100))
            if weight:
                distribution.extend([key] * weight)
        return distribution

    def choose_alternative(
        self,
        factory: Any,
        activity_id: int,
        exclude: set[str] | None = None,
    ) -> str | None:
        exclude = exclude or set()
        pool = [
            key
            for key in self._distribution(factory)
            if key not in exclude and self.can_spawn(key, activity_id)
        ]
        return random.choice(pool) if pool else None

    def choose_type_for_constructor(self, requested_type: str) -> str | None:
        """Respect limits even for game modes that force a specific type."""
        key = str(requested_type)
        if not self.enabled or self.can_spawn(key, self.current_activity_id()):
            return key
        try:
            factory = self.factory_class.get()
        except Exception:
            _LOG.exception("Could not access PowerupBoxFactory while enforcing quantity limits.")
            return None
        return self.choose_alternative(factory, self.current_activity_id())

    def choose_random_type(
        self, factory: Any, forcetype: str | None, excludetypes: list[str] | None
    ) -> str:
        """Weighted random choice filtered by enabled flags and active limits."""
        if not self.enabled:
            # This path is normally bypassed by the installed wrapper.
            return random.choice(self._distribution(factory) or ["health"])

        activity_id = self.current_activity_id()
        exclusions = set(str(value) for value in (excludetypes or []))
        if forcetype is not None and self.can_spawn(str(forcetype), activity_id):
            factory._lastpoweruptype = str(forcetype)
            return str(forcetype)

        weighted_pool = [
            key
            for key in self._distribution(factory)
            if key not in exclusions and self.can_spawn(key, activity_id)
        ]

        # Preserve the stock curse -> health safety sequence when possible.
        if (
            getattr(factory, "_lastpoweruptype", None) == "curse"
            and "health" in weighted_pool
        ):
            selected = "health"
        elif weighted_pool:
            selected = random.choice(weighted_pool)
        else:
            # If all quantities are at their limits, return a valid stock type;
            # the constructor wrapper will suppress the resulting empty actor.
            fallback = [key for key in self._distribution(factory) if key not in exclusions]
            if not fallback:
                fallback = self._distribution(factory)
            selected = random.choice(fallback) if fallback else "health"

        factory._lastpoweruptype = selected
        return selected

    def _clock_now(self) -> float:
        """Use the scene clock shared by stock powerup expiration timers."""
        try:
            return float(self.bs.time())
        except Exception:
            return time.monotonic()

    def _label_text(self, powerup_type: str, deadline: float | None) -> str:
        lines: list[str] = []
        if self.display_names_enabled():
            setting = self._setting(powerup_type)
            label = setting.get("display_name")
            if not isinstance(label, str) or not label.strip():
                label = registry.get_display_name(powerup_type)
            lines.append(label.strip())
        if self.display_timer_enabled() and deadline is not None:
            # Stay at 1s until the game's own expiration callback removes the box.
            remaining = max(1, int(math.ceil(deadline - self._clock_now())))
            lines.append(f"{remaining}s")
        return "\n".join(lines)

    def _start_powerup_effect(self, record: dict[str, Any], node: Any) -> None:
        """Attach an optional configured effect controller to a live powerup box."""
        config = self.powerup_effect_config
        if not config.get("enable", False) or node is None:
            return
        effect_name = str(config.get("effect", "spark")).strip().casefold()
        if not effect_name or effect_name == "noeffect":
            return
        if self._powerup_effect_import_failed:
            return
        try:
            # Import lazily so a visual-effect issue cannot prevent powerups from working.
            effects_module = _load_powerup_effect_module()
            PowerupEffectController = effects_module.PowerupEffectController
            normalize_powerup_effect_name = effects_module.normalize_powerup_effect_name

            normalized_effect = normalize_powerup_effect_name(effect_name)
            if normalized_effect is None:
                if not self._powerup_effect_warning_emitted:
                    _LOG.warning(
                        "Unsupported powerupEffects.effect=%r. Choose a powerup-compatible effect from spaz_effects.py.",
                        effect_name,
                    )
                    self._powerup_effect_warning_emitted = True
                return

            controller = PowerupEffectController(
                node=node,
                effect_name=normalized_effect,
                continuous=bool(config.get("continuous", True)),
                interval_seconds=float(config.get("intervalSeconds", 0.25)),
                bs_module=self.bs,
            )
            if controller.start():
                record["effect_controller"] = controller
            else:
                controller.stop()
        except Exception:
            if not self._powerup_effect_warning_emitted:
                self._powerup_effect_warning_emitted = True
                _LOG.exception("Could not start configured powerup effect %r.", effect_name)
            self._powerup_effect_import_failed = True

    def track_box(
        self,
        box: Any,
        powerup_type: str,
        node: Any = None,
        lifetime_seconds: float | None = None,
        activity_id: int | None = None,
    ) -> None:
        """Track a built-in or custom box for quota and optional display.

        Custom box implementations can call this after creating their scene
        node, then call untrack_box(self) when collected or destroyed.
        """
        if not self.enabled:
            return
        if activity_id is None:
            activity_id = self.activity_id_for_box(box)
        key = id(box)
        if key in self._active:
            self._discard_record(key, delete_visuals=True)
        deadline = (
            self._clock_now() + max(0.0, float(lifetime_seconds))
            if lifetime_seconds is not None
            else None
        )
        try:
            box_ref = weakref.ref(box, lambda _ref, ident=key: self._on_box_gc(ident))
        except TypeError:
            _LOG.warning("Powerup actor %r cannot be weak-referenced; not tracking it.", box)
            return

        record: dict[str, Any] = {
            "ref": box_ref,
            "activity_id": int(activity_id or 0),
            "powerup_type": str(powerup_type),
            "deadline": deadline,
            "label": None,
            "position_helper": None,
            "timer": None,
            "effect_controller": None,
        }
        self._active[key] = record
        if node is None:
            return
        self._start_powerup_effect(record, node)
        wants_name = self.display_names_enabled()
        wants_timer = self.display_timer_enabled() and deadline is not None
        if not (wants_name or wants_timer):
            return
        try:
            label = self.bs.newnode(
                "text",
                attrs={
                    "text": self._label_text(str(powerup_type), deadline),
                    "in_world": True,
                    "shadow": 1.0,
                    "flatness": 1.0,
                    "color": (1.0, 0.88, 0.35, 1.0),
                    "scale": 0.010,
                    "h_align": "center",
                    "v_align": "center",
                    "position": (0.0, 0.0, 0.0),
                },
            )
            record["label"] = label
            position_helper = self.bs.newnode(
                "math",
                attrs={"input1": (0.0, 1.0, 0.0), "operation": "add"},
            )
            record["position_helper"] = position_helper
            node.connectattr("position", position_helper, "input2")
            position_helper.connectattr("output", label, "position")
            if wants_timer:
                record["timer"] = self.bs.Timer(
                    0.25,
                    self.bs.WeakCallStrict(self._update_label, key),
                    repeat=True,
                )
        except Exception:
            _LOG.exception("Could not create powerup display label for %s.", powerup_type)
            # A cosmetic-label failure must not disable quantity tracking.
            self._clear_display(record)

    def _update_label(self, key: int) -> None:
        record = self._active.get(key)
        if record is None:
            return
        box = record["ref"]()
        if box is None or getattr(box, "_pm_dead", False):
            self._discard_record(key, delete_visuals=True)
            return
        node = getattr(box, "node", None)
        if node is None:
            self._discard_record(key, delete_visuals=True)
            return
        try:
            if not node:
                self._discard_record(key, delete_visuals=True)
                return
            label = record.get("label")
            if label:
                label.text = self._label_text(record["powerup_type"], record["deadline"])
            else:
                self._clear_display(record)
        except Exception:
            _LOG.exception("Error updating powerup countdown label.")
            # Keep counting this live box even if the display nodes fail.
            self._clear_display(record)

    def untrack_box(self, box: Any) -> None:
        self._discard_record(id(box), delete_visuals=True, expected_box=box)

    def _on_box_gc(self, key: int) -> None:
        # The Actor's __del__ normally sends DieMessage first; this is a final
        # bookkeeping fallback. Avoid node operations in a weakref callback.
        self._active.pop(key, None)

    def _discard_record(
        self, key: int, delete_visuals: bool, expected_box: Any | None = None
    ) -> None:
        record = self._active.get(key)
        if record is None:
            return
        current_box = record["ref"]()
        if expected_box is not None and current_box is not expected_box:
            return
        self._active.pop(key, None)
        # Stop repeat and cleanup timers before releasing the box's scene references.
        effect_controller = record.get("effect_controller")
        record["effect_controller"] = None
        if effect_controller is not None:
            try:
                effect_controller.stop()
            except Exception:
                _LOG.exception("Could not stop powerup visual effects during cleanup.")
        if delete_visuals:
            self._clear_display(record)

    def _clear_display(self, record: dict[str, Any]) -> None:
        """Release the countdown timer and label nodes without untracking box."""
        timer = record.get("timer")
        record["timer"] = None
        record_label = record.get("label")
        record_helper = record.get("position_helper")
        record["label"] = None
        record["position_helper"] = None
        for node in (record_label, record_helper):
            try:
                if node:
                    node.delete()
            except Exception:
                pass
        # Keep a local until node cleanup is complete, then allow Timer release.
        timer = None

    def reload_config(self) -> bool:
        """Reload JSON in the current process; return whether it succeeded."""
        new_config = self._load_config()
        if not new_config.get("enabled", False) and not _CONFIG_PATH.exists():
            self.config = new_config
            return False
        self.config = new_config
        return True


def get_manager() -> PowerupManager | None:
    """Return the active manager instance, if installed."""
    return _MANAGER


def install() -> PowerupManager:
    """Install the manager once; safe to call repeatedly from custom_hooks."""
    global _MANAGER, _INSTALLED, _ORIGINALS
    if _INSTALLED and _MANAGER is not None:
        return _MANAGER

    import bascenev1 as bs
    from bascenev1lib.actor.powerupbox import (
        DEFAULT_POWERUP_INTERVAL,
        PowerupBox,
        PowerupBoxFactory,
        _TouchedMessage,
    )

    registry.load_custom_modules()
    manager = PowerupManager(bs, PowerupBoxFactory, DEFAULT_POWERUP_INTERVAL)
    original_init = PowerupBox.__init__
    original_handlemessage = PowerupBox.handlemessage
    original_exists = PowerupBox.exists
    original_random = PowerupBoxFactory.get_random_powerup_type

    def managed_init(
        self: Any,
        position: Any = (0.0, 1.0, 0.0),
        poweruptype: str = "triple_bombs",
        expire: bool = True,
    ) -> None:
        if not manager.enabled:
            original_init(self, position=position, poweruptype=poweruptype, expire=expire)
            return

        selected = manager.choose_type_for_constructor(str(poweruptype))
        if selected is None:
            # Build a valid Actor with no scene node. The caller can safely call
            # .autoretain(); exists() immediately reports False so Ballistica
            # drops the empty actor during its normal actor pruning.
            bs.Actor.__init__(self)
            self.poweruptype = str(poweruptype)
            self.node = None
            self._powersgiven = True
            self._pm_suppressed = True
            self._pm_dead = True
            return

        self._pm_suppressed = False
        self._pm_dead = False
        self._pm_custom_type = None
        selected_key = str(selected)
        spec = registry.get_spec(selected_key)
        implementation = (
            spec.get("factory")
            if spec is not None and not spec.get("builtin", False)
            else None
        )

        if implementation is not None:
            # The stock PowerupBox renderer only knows built-in types. Reuse a
            # stock box appearance/material, then attach our custom pickup type.
            apply_effect = getattr(implementation, "apply_to_spaz", None)
            if not callable(apply_effect):
                _LOG.error(
                    "Custom powerup %s is registered without apply_to_spaz(); "
                    "suppressing this unsupported spawn.",
                    selected_key,
                )
                bs.Actor.__init__(self)
                self.poweruptype = selected_key
                self.node = None
                self._powersgiven = True
                self._pm_suppressed = True
                self._pm_dead = True
                return
            stock_box_type = str(getattr(implementation, "box_type", "shield"))
            original_init(
                self,
                position=position,
                poweruptype=stock_box_type,
                expire=expire,
            )
            self.poweruptype = selected_key
            self._pm_custom_type = selected_key
        else:
            original_init(self, position=position, poweruptype=selected_key, expire=expire)

        self._pm_suppressed = False
        self._pm_dead = False
        lifetime = max(0.0, float(DEFAULT_POWERUP_INTERVAL) - 1.0) if expire else None
        manager.track_box(
            self,
            selected_key,
            getattr(self, "node", None),
            lifetime_seconds=lifetime,
            activity_id=manager.activity_id_for_box(self),
        )

    def managed_handlemessage(self: Any, msg: Any) -> Any:
        if getattr(self, "_pm_suppressed", False):
            if isinstance(msg, bs.DieMessage):
                self._pm_dead = True
            return None

        custom_type = getattr(self, "_pm_custom_type", None)
        if custom_type is not None and isinstance(msg, _TouchedMessage):
            if not getattr(self, "_powersgiven", False):
                try:
                    collision = bs.getcollision()
                    opposing_node = collision.opposingnode
                    from bascenev1lib.actor.spaz import Spaz

                    spaz = opposing_node.getdelegate(Spaz)
                    spec = registry.get_spec(str(custom_type))
                    implementation = spec.get("factory") if spec is not None else None
                    apply_effect = getattr(implementation, "apply_to_spaz", None)
                    if spaz is not None and callable(apply_effect):
                        applied = bool(apply_effect(spaz, bs_module=bs))
                        if applied:
                            # Let stock handling play the pickup sound and run
                            # the original DieMessage/visual cleanup lifecycle.
                            original_handlemessage(self, bs.PowerupAcceptMessage())
                except Exception:
                    _LOG.exception(
                        "Custom powerup pickup failed for %s; box remains available.",
                        custom_type,
                    )
            return None

        try:
            return original_handlemessage(self, msg)
        finally:
            if isinstance(msg, bs.DieMessage):
                self._pm_dead = True
                manager.untrack_box(self)

    def managed_exists(self: Any) -> bool:
        if getattr(self, "_pm_suppressed", False):
            return False
        return bool(original_exists(self))

    def managed_random_type(
        self: Any,
        forcetype: str | None = None,
        excludetypes: list[str] | None = None,
    ) -> str:
        if not manager.enabled:
            return original_random(self, forcetype=forcetype, excludetypes=excludetypes)
        return manager.choose_random_type(self, forcetype, excludetypes)

    _ORIGINALS = {
        "powerup_init": original_init,
        "powerup_handlemessage": original_handlemessage,
        "powerup_exists": original_exists,
        "factory_random": original_random,
    }
    PowerupBox.__init__ = managed_init
    PowerupBox.handlemessage = managed_handlemessage
    PowerupBox.exists = managed_exists
    PowerupBoxFactory.get_random_powerup_type = managed_random_type

    _MANAGER = manager
    _INSTALLED = True
    _LOG.info("Powerup Manager installed; default disappearance timing is unchanged.")
    return manager
