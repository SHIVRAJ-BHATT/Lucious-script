# ba_meta require api 9
# Owner/Editor: Lucifer
"""BombSquad 1.8.x profile color/highlight multipliers.

Default behavior applies modest multipliers to all selected profiles. Optional
profile-name markers override those defaults and can provide a clean display name.
This adjusts the selected player's tint colors; it does not create an aura,
particles, or an attached light.

Marker format:
    ^,2.0,1.5,0,0,Display Name

Fields: marker, color multiplier, highlight multiplier, stabilize-color,
stabilize-highlight, display name. Supported markers: ", ', ^, %, ;, `.

The feature is controlled by the "glowingProfiles" section of mods/setting.json.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

import bascenev1 as bs

_LOG = logging.getLogger("GlowingProfiles")
_MARKERS = ('"', "'", "^", "%", ";", "`")
_FLOAT_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
_INT_RE = re.compile(r"[-+]?\d+")
_ENABLED = False
_SETTINGS_CACHE: dict[str, Any] | None = None
_NO_MATCH_REPORTED: set[tuple[str, str]] = set()
_DEFAULT_APPLIED_REPORTED: set[tuple[str, str]] = set()

# Settings accepted by the plugin. These defaults make the feature visible
# immediately without requiring cloud-profile names to be renamed.
_DEFAULT_COLOR_MULTIPLIER = 2.0
_DEFAULT_HIGHLIGHT_MULTIPLIER = 1.5
_DEFAULT_APPLY_TO_ALL = True
_MAX_MULTIPLIER = 50.0


def _read_settings() -> dict[str, Any]:
    """Read mods/setting.json once and return its glowingProfiles section."""
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is not None:
        return _SETTINGS_CACHE

    try:
        path = Path(__file__).resolve().parent.parent / "setting.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("setting.json root must be a JSON object")
        section = data.get("glowingProfiles", {})
        if not isinstance(section, dict):
            raise ValueError("setting.json glowingProfiles must be a JSON object")
        _SETTINGS_CACHE = section
    except FileNotFoundError:
        # A missing setting file should not prevent the feature from working.
        _LOG.warning("setting.json not found; using Glowing Profiles defaults.")
        _SETTINGS_CACHE = {}
    except Exception:
        _LOG.exception("Could not read glowingProfiles settings; using defaults.")
        _SETTINGS_CACHE = {}

    return _SETTINGS_CACHE


def _get_bool(key: str, default: bool) -> bool:
    value = _read_settings().get(key, default)
    return value if isinstance(value, bool) else default


def _get_multiplier(key: str, default: float) -> float:
    value = _read_settings().get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    # Negative multipliers invert channels rather than make a useful glow.
    return max(0.0, min(number, _MAX_MULTIPLIER))


def _extract_number(pattern: re.Pattern[str], value: str, cast: Any) -> Any:
    match = pattern.search(value)
    if match is None:
        raise ValueError(f"No valid number in {value!r}")
    return cast(match.group(0))


def _parse_marked_name(
    name: str,
) -> tuple[float, float, bool, bool, str] | None:
    """Read per-profile multipliers from a marked profile key or display name."""
    if not isinstance(name, str) or len(name) < 2:
        return None
    marker = name[0]
    if marker not in _MARKERS or not name.startswith(marker + ","):
        return None

    parts = name.split(",", 5)
    if len(parts) < 5:
        _LOG.warning("Ignoring malformed glowing profile name %r.", name)
        return None

    try:
        color_mult = _extract_number(_FLOAT_RE, parts[1], float)
        highlight_mult = _extract_number(_FLOAT_RE, parts[2], float)
        stabilize_color = _extract_number(_INT_RE, parts[3], int) > 0
        stabilize_highlight = _extract_number(_INT_RE, parts[4], int) > 0
    except (ValueError, TypeError, IndexError):
        _LOG.warning("Ignoring malformed glowing profile name %r.", name)
        return None

    if not math.isfinite(color_mult) or not math.isfinite(highlight_mult):
        _LOG.warning("Ignoring non-finite glowing multipliers in %r.", name)
        return None

    color_mult = max(0.0, min(color_mult, _MAX_MULTIPLIER))
    highlight_mult = max(0.0, min(highlight_mult, _MAX_MULTIPLIER))
    display_name = parts[5].strip() if len(parts) > 5 else "Glowing Profile"
    if not display_name:
        display_name = "Glowing Profile"

    return color_mult, highlight_mult, stabilize_color, stabilize_highlight, display_name


def _profile_names(chooser: Any) -> tuple[str, str]:
    """Return the selected profile key and decoded cloud display name."""
    try:
        names = getattr(chooser, "_profilenames", ())
        index = int(getattr(chooser, "_profileindex", -1))
        if index < 0 or index >= len(names):
            return "", ""
        key = str(names[index])
    except Exception:
        return "", ""

    visible_name = ""
    try:
        cloud_names = getattr(chooser, "_cloud_name_by_name", {})
        name_json = cloud_names.get(key) if isinstance(cloud_names, dict) else None
        if isinstance(name_json, str) and name_json:
            from bascenev1._character import name_text

            decoded = name_text(name_json)
            if isinstance(decoded, str):
                visible_name = decoded
    except Exception:
        _LOG.debug("Could not decode cloud profile display name.", exc_info=True)

    return key, visible_name


def _marked_settings(
    chooser: Any,
) -> tuple[tuple[float, float, bool, bool, str] | None, str, str, str]:
    """Return marker settings and matching source, without global fallback."""
    key, visible_name = _profile_names(chooser)
    parsed = _parse_marked_name(key)
    if parsed is not None:
        return parsed, key, visible_name, "profile key"
    parsed = _parse_marked_name(visible_name)
    if parsed is not None:
        return parsed, key, visible_name, "cloud display name"
    return None, key, visible_name, ""


def _scaled_rgb(
    color: Any, multiplier: float, stabilize: bool
) -> tuple[float, float, float]:
    """Multiply RGB channels, optionally touching only the strongest channel."""
    rgb = tuple(float(value) for value in color)
    if len(rgb) != 3 or any(not math.isfinite(value) for value in rgb):
        raise ValueError(f"Expected three finite RGB channels; got {color!r}")

    if not stabilize:
        return rgb[0] * multiplier, rgb[1] * multiplier, rgb[2] * multiplier

    strongest = max(rgb)
    return tuple(
        value * multiplier if value == strongest else value for value in rgb
    )  # type: ignore[return-value]


def _install_update_hook() -> None:
    """Wrap native profile initialization, then apply color multipliers."""
    chooser_class = getattr(bs, "Chooser", None)
    if chooser_class is None:
        raise RuntimeError("bascenev1.Chooser is unavailable on this build")

    original_update = getattr(chooser_class, "update_from_profile", None)
    if not callable(original_update):
        raise RuntimeError("Chooser.update_from_profile() is unavailable")
    if getattr(original_update, "_lucifer_glowing_profiles_wrapped", False):
        return

    def wrapped_update(self: Any, *args: Any, **kwargs: Any) -> Any:
        # Keep the native update intact: 1.8.x needs it to compose cloud looks.
        result = original_update(self, *args, **kwargs)
        try:
            marked, profile_key, visible_name, match_source = _marked_settings(self)
            # The edit-profile entry is a UI action, not a playable profile.
            # Native update_from_profile intentionally leaves its colors alone.
            if profile_key == "_edit":
                return result
            if marked is not None:
                color_mult, highlight_mult, stabilize_color, stabilize_highlight, _ = marked
                source = f"marked {match_source}"
            elif _get_bool("applyToAllProfiles", _DEFAULT_APPLY_TO_ALL):
                color_mult = _get_multiplier("colorMultiplier", _DEFAULT_COLOR_MULTIPLIER)
                highlight_mult = _get_multiplier(
                    "highlightMultiplier", _DEFAULT_HIGHLIGHT_MULTIPLIER
                )
                stabilize_color = _get_bool("stabilizeColor", False)
                stabilize_highlight = _get_bool("stabilizeHighlight", False)
                source = "default settings"
                report_key = (profile_key, visible_name)
                if report_key not in _DEFAULT_APPLIED_REPORTED:
                    _DEFAULT_APPLIED_REPORTED.add(report_key)
                    _LOG.info(
                        "No profile marker for key=%r display=%r; using default multipliers.",
                        profile_key,
                        visible_name,
                    )
            else:
                report_key = (profile_key, visible_name)
                if report_key not in _NO_MATCH_REPORTED:
                    _NO_MATCH_REPORTED.add(report_key)
                    _LOG.info(
                        "No glowing marker found for profile key=%r display=%r; no multiplier applied.",
                        profile_key,
                        visible_name,
                    )
                return result

            old_color = tuple(self._color)
            old_highlight = tuple(self._highlight)
            self._color = _scaled_rgb(old_color, color_mult, stabilize_color)
            self._highlight = _scaled_rgb(
                old_highlight, highlight_mult, stabilize_highlight
            )

            # Native update already refreshed the icon/text; refresh again now
            # that their colors have changed. Each call is guarded separately.
            update_icon = getattr(self, "_update_icon", None)
            if callable(update_icon):
                update_icon()
            update_text = getattr(self, "_update_text", None)
            if callable(update_text):
                update_text()

            _LOG.info(
                "Applied Glowing Profiles (%s) to key=%r display=%r: color x%.3g, highlight x%.3g.",
                source,
                profile_key,
                visible_name,
                color_mult,
                highlight_mult,
            )
        except Exception:
            # Never let a cosmetic plugin interrupt a lobby selection/join.
            _LOG.exception("Could not apply Glowing Profiles colors.")
        return result

    wrapped_update._lucifer_glowing_profiles_wrapped = True  # type: ignore[attr-defined]
    chooser_class.update_from_profile = wrapped_update


def _install_legacy_name_hook() -> None:
    """Show only the configured display-name suffix for marked legacy profiles."""
    chooser_class = getattr(bs, "Chooser", None)
    if chooser_class is None:
        return
    original_getname = getattr(chooser_class, "_getname", None)
    if not callable(original_getname):
        return
    if getattr(original_getname, "_lucifer_glowing_profiles_wrapped", False):
        return

    def wrapped_getname(self: Any, *args: Any, **kwargs: Any) -> str:
        name = original_getname(self, *args, **kwargs)
        try:
            marked, _key, _visible_name, _source = _marked_settings(self)
            if marked is not None:
                display_name = marked[4]
                full = kwargs.get("full", args[0] if args else False)
                if not full and len(display_name) > 10:
                    return display_name[:10] + "..."
                return display_name
        except Exception:
            _LOG.debug("Could not clean marked legacy profile name.", exc_info=True)
        return name

    wrapped_getname._lucifer_glowing_profiles_wrapped = True  # type: ignore[attr-defined]
    chooser_class._getname = wrapped_getname


def _install_cloud_name_hook() -> None:
    """Remove the encoded marker from cloud profile names while preserving styling."""
    chooser_class = getattr(bs, "Chooser", None)
    if chooser_class is None:
        return
    original_method = getattr(chooser_class, "_get_name_depiction_json", None)
    if not callable(original_method):
        _LOG.info("Cloud name-depiction method unavailable; native names retained.")
        return
    if getattr(original_method, "_lucifer_glowing_cloud_name_wrapped", False):
        return

    def wrapped_name_json(self: Any, *args: Any, **kwargs: Any) -> str:
        result = original_method(self, *args, **kwargs)
        try:
            marked, _key, _visible_name, _source = _marked_settings(self)
            if marked is None:
                return result

            from bacommon import depiction as bdep
            from efro.dataclassio import dataclass_from_json, dataclass_to_json

            depiction = dataclass_from_json(bdep.Depiction, result)
            if not isinstance(depiction, bdep.NameDepiction):
                return result

            # NameDepiction's serialized name field stores a name-block JSON.
            # Keep its depiction-level color/team styling; replace only text.
            name_block = json.dumps({"b": {"t": marked[4]}}, separators=(",", ":"))
            try:
                depiction.name = name_block
            except Exception:
                from dataclasses import replace

                depiction = replace(depiction, name=name_block)
            return dataclass_to_json(depiction)
        except Exception:
            _LOG.debug("Could not remove cloud profile marker from its display name.", exc_info=True)
            return result

    wrapped_name_json._lucifer_glowing_cloud_name_wrapped = True  # type: ignore[attr-defined]
    chooser_class._get_name_depiction_json = wrapped_name_json


def enable() -> bool:
    """Install all Glowing Profiles hooks once; return True on success."""
    global _ENABLED
    if _ENABLED:
        return True

    if not _get_bool("enable", True):
        _LOG.info("Glowing Profiles disabled by setting.json.")
        return False

    _install_update_hook()
    _install_legacy_name_hook()
    _install_cloud_name_hook()
    _ENABLED = True
    _LOG.info(
        "Glowing Profiles enabled (applyToAllProfiles=%s, colorMultiplier=%s, highlightMultiplier=%s).",
        _get_bool("applyToAllProfiles", _DEFAULT_APPLY_TO_ALL),
        _get_multiplier("colorMultiplier", _DEFAULT_COLOR_MULTIPLIER),
        _get_multiplier("highlightMultiplier", _DEFAULT_HIGHLIGHT_MULTIPLIER),
    )
    return True
