"""Central settings manager for Lucifer's BombSquad 1.8.3 / API 9 mods.

The configuration lives beside this file at ``mods/setting.json``.
Missing files are initialized with safe defaults. Unknown keys are preserved
so settings can be extended without destroying unrelated configuration.
"""

# ba_meta require api 9
# Owner/Editor: Lucifer
from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import tempfile
from functools import lru_cache
from typing import Any

_LOG = logging.getLogger("Lucifer.Settings")
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "setting.json")

DEFAULT_SETTINGS: dict[str, Any] = {
    "autoNightMode": {
        "enable": False,
        "startTime": "18:30",
        "endTime": "06:30",
        "timezone": "Asia/Kolkata",
    },
    "mapTint": {
        "enable": True,
        "color": [1.0, 1.0, 1.0],
    },
    "enabletags": True,
    "enablehptag": True,
    "enablestats": True,
    "enablePingDisplay": True,
    "leaderboard": {
        "enable": True,
        "refreshSeconds": 10,
    },
    "enableTop3effects": False,
    "hiteffects": True,
    "top3Effects": {
        "rank1": "ringstars",
        "rank2": "glow",
        "rank3": "spark",
    },
    "textonmap": {
        "enable": True,
    },
    "colorful_explosions": {
        "enable": True,
    },
    "snowfall": {
        "enable": True,
        "size": 0.75,
        "fallSpeed": 50.0,
        "particleCount": 10,
    },
    "playermod": {
        "default_boxing_gloves": True,
        "default_bomb_count": 2,
        "default_bomb": "normal",
    },
    "economy": {
        "enable": True,
        "dailyReward": 100,
        "dailyCooldownHours": 24,
        "ffaWinReward": 50,
        "teamWinReward": 50,
        "tagPrice": 100,
        "effectPrice": 200,
        "paidCommands": {
            "enable": True,
            "nv": 1000,
            "gm": 2000,
            "end": 5000,
        },
    },
    "hitmessages": True,
    "soloBot": {
        "enable": False,
        "name": "?LUCI?",
        "character": "Pixel",
        "nameColor": [1.0, 1.0, 1.0],
        "color": [0.0, 0.0, 0.0],
        "highlight": [0.0, 0.0, 0.0],
        "spawnSeconds": 0.35,
        "respawnSeconds": 0.65,
        "showAnnouncements": True,
        "spawnAnnouncement": "?  ?  {name} ?  ?  HAS SPAWNED  ?\n?  PREPARE FOR A FIGHT  ?",
        "returnAnnouncement": "?  ? {name} RETURNS! ?  ?\n?  THE FIGHT CONTINUES  ?",
        "spawnAnnouncementColor": [0.2, 1.0, 0.75],
        "returnAnnouncementColor": [0.35, 0.75, 1.0],
    },
    "quiz": {
        "enable": False,
        "firstQuestionDelaySeconds": 30,
        "intervalSeconds": 180,
        "answerWindowSeconds": 30,
        "rewardCoins": 25,
        "announcerName": "? LUCI QUIZ ?",
    },
    "StumbledScoreScreen": False,
    "discordbot": {
        "enable": False,
        "token": "",
        "serverID": "",
        "logsChannelID": "",
        "liveStatsChannelID": "",
        "adminUsers": [],
        "prefix": "a!",
        "cmd_prefix": "at",
        "webEnable": False,
        "webUpdateSeconds": 10,
        "webApiURL": "",
        "webApiKey": "",
    },
}


def _deep_merge(defaults: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
    """Merge supplied values over defaults without mutating either input."""
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.datetime.strptime(value.strip(), "%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def _normalise_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Merge defaults and normalize the options currently supported."""
    result = _deep_merge(DEFAULT_SETTINGS, data)

    bool_keys = (
        "enabletags",
        "enablehptag",
        "enablestats",
        "enablePingDisplay",
        "enableTop3effects",
        "hiteffects",
        "hitmessages",
        "StumbledScoreScreen",
    )
    for key in bool_keys:
        if not isinstance(result.get(key), bool):
            _LOG.warning("Invalid setting %s; using default %s.", key, DEFAULT_SETTINGS[key])
            result[key] = DEFAULT_SETTINGS[key]

    for section_name in (
        "autoNightMode", "mapTint", "leaderboard", "textonmap", "colorful_explosions",
        "snowfall", "top3Effects", "playermod", "economy", "soloBot",
        "quiz", "discordbot",
    ):
        if not isinstance(result.get(section_name), dict):
            _LOG.warning("Invalid settings section %s; using defaults.", section_name)
            result[section_name] = copy.deepcopy(DEFAULT_SETTINGS[section_name])

    night = result["autoNightMode"]
    if not isinstance(night.get("enable"), bool):
        night["enable"] = DEFAULT_SETTINGS["autoNightMode"]["enable"]
    for key in ("startTime", "endTime"):
        if not _valid_time(night.get(key)):
            _LOG.warning("Invalid autoNightMode.%s; restoring its default.", key)
            night[key] = DEFAULT_SETTINGS["autoNightMode"][key]
    if not isinstance(night.get("timezone"), str) or not night["timezone"].strip():
        night["timezone"] = DEFAULT_SETTINGS["autoNightMode"]["timezone"]

    map_tint = result["mapTint"]
    if not isinstance(map_tint.get("enable"), bool):
        _LOG.warning("Invalid mapTint.enable; using its default.")
        map_tint["enable"] = DEFAULT_SETTINGS["mapTint"]["enable"]
    tint_color = map_tint.get("color")
    if (
        not isinstance(tint_color, list)
        or len(tint_color) != 3
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= value <= 1.0
            for value in tint_color
        )
    ):
        _LOG.warning("Invalid mapTint.color; restoring its default RGB values.")
        map_tint["color"] = copy.deepcopy(DEFAULT_SETTINGS["mapTint"]["color"])
    else:
        map_tint["color"] = [float(value) for value in tint_color]

    leaderboard = result["leaderboard"]
    if not isinstance(leaderboard.get("enable"), bool):
        leaderboard["enable"] = DEFAULT_SETTINGS["leaderboard"]["enable"]
    interval = leaderboard.get("refreshSeconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 3600:
        leaderboard["refreshSeconds"] = DEFAULT_SETTINGS["leaderboard"]["refreshSeconds"]
    else:
        leaderboard["refreshSeconds"] = int(interval)

    textonmap = result["textonmap"]
    if not isinstance(textonmap.get("enable"), bool):
        textonmap["enable"] = DEFAULT_SETTINGS["textonmap"]["enable"]

    colorful_explosions = result["colorful_explosions"]
    if not isinstance(colorful_explosions.get("enable"), bool):
        colorful_explosions["enable"] = DEFAULT_SETTINGS["colorful_explosions"]["enable"]

    snowfall = result["snowfall"]
    if not isinstance(snowfall.get("enable"), bool):
        _LOG.warning("Invalid snowfall.enable; using its default.")
        snowfall["enable"] = DEFAULT_SETTINGS["snowfall"]["enable"]

    # Snow particle scale multiplier. 0.75 preserves the previous default look.
    snow_size = snowfall.get("size")
    if (isinstance(snow_size, bool) or not isinstance(snow_size, (int, float))
            or not 0.1 <= snow_size <= 5.0):
        _LOG.warning("Invalid snowfall.size; using the default.")
        snowfall["size"] = DEFAULT_SETTINGS["snowfall"]["size"]
    else:
        snowfall["size"] = float(snow_size)

    # Positive vertical velocity magnitude; particles travel downward at -fallSpeed.
    fall_speed = snowfall.get("fallSpeed")
    if (isinstance(fall_speed, bool) or not isinstance(fall_speed, (int, float))
            or not 1.0 <= fall_speed <= 150.0):
        _LOG.warning("Invalid snowfall.fallSpeed; using the default.")
        snowfall["fallSpeed"] = DEFAULT_SETTINGS["snowfall"]["fallSpeed"]
    else:
        snowfall["fallSpeed"] = float(fall_speed)

    particle_count = snowfall.get("particleCount")
    if (isinstance(particle_count, bool) or not isinstance(particle_count, int)
            or not 1 <= particle_count <= 100):
        _LOG.warning("Invalid snowfall.particleCount; using the default.")
        snowfall["particleCount"] = DEFAULT_SETTINGS["snowfall"]["particleCount"]

    effects = result["top3Effects"]
    for key, default_value in DEFAULT_SETTINGS["top3Effects"].items():
        value = effects.get(key)
        if not isinstance(value, str) or not value.strip():
            effects[key] = default_value
        else:
            effects[key] = value.strip()

    playermod = result["playermod"]
    gloves = playermod.get("default_boxing_gloves")
    if not isinstance(gloves, bool):
        playermod["default_boxing_gloves"] = DEFAULT_SETTINGS["playermod"]["default_boxing_gloves"]

    bomb_count = playermod.get("default_bomb_count")
    if (isinstance(bomb_count, bool) or not isinstance(bomb_count, int)
            or not 1 <= bomb_count <= 10):
        _LOG.warning("Invalid playermod.default_bomb_count; using default 2.")
        playermod["default_bomb_count"] = DEFAULT_SETTINGS["playermod"]["default_bomb_count"]

    bomb_type = playermod.get("default_bomb")
    supported_bomb_types = {"normal", "sticky", "impact", "ice"}
    if not isinstance(bomb_type, str) or bomb_type.strip().lower() not in supported_bomb_types:
        _LOG.warning("Invalid playermod.default_bomb; using 'normal'.")
        playermod["default_bomb"] = DEFAULT_SETTINGS["playermod"]["default_bomb"]
    else:
        playermod["default_bomb"] = bomb_type.strip().lower()

    # Economy settings: integer amounts only, bounded to protect wallet data.
    economy = result["economy"]
    if not isinstance(economy.get("enable"), bool):
        economy["enable"] = DEFAULT_SETTINGS["economy"]["enable"]

    for key in ("dailyReward", "ffaWinReward", "teamWinReward", "tagPrice", "effectPrice"):
        value = economy.get(key)
        default_value = DEFAULT_SETTINGS["economy"][key]
        minimum = 0 if key in ("ffaWinReward", "teamWinReward") else 1
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 1_000_000_000:
            _LOG.warning("Invalid economy.%s; restoring its default.", key)
            economy[key] = default_value

    cooldown = economy.get("dailyCooldownHours")
    if (isinstance(cooldown, bool) or not isinstance(cooldown, (int, float))
            or not 1 <= cooldown <= 720):
        _LOG.warning("Invalid economy.dailyCooldownHours; restoring its default.")
        economy["dailyCooldownHours"] = DEFAULT_SETTINGS["economy"]["dailyCooldownHours"]
    else:
        economy["dailyCooldownHours"] = int(cooldown)

    paid_commands = economy.get("paidCommands")
    if not isinstance(paid_commands, dict):
        _LOG.warning("Invalid economy.paidCommands; restoring its defaults.")
        paid_commands = copy.deepcopy(DEFAULT_SETTINGS["economy"]["paidCommands"])
        economy["paidCommands"] = paid_commands
    if not isinstance(paid_commands.get("enable"), bool):
        paid_commands["enable"] = DEFAULT_SETTINGS["economy"]["paidCommands"]["enable"]
    for key in ("end", "nv", "gm"):
        value = paid_commands.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1_000_000_000:
            _LOG.warning("Invalid economy.paidCommands.%s; restoring its default.", key)
            paid_commands[key] = DEFAULT_SETTINGS["economy"]["paidCommands"][key]

    # Optional Solo Bot configuration.
    solo_bot = result["soloBot"]
    for key in ("enable", "showAnnouncements"):
        if not isinstance(solo_bot.get(key), bool):
            solo_bot[key] = DEFAULT_SETTINGS["soloBot"][key]
    for key in ("name", "character", "spawnAnnouncement", "returnAnnouncement"):
        if not isinstance(solo_bot.get(key), str):
            solo_bot[key] = DEFAULT_SETTINGS["soloBot"][key]
    for key in ("spawnSeconds", "respawnSeconds"):
        value = solo_bot.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.05 <= value <= 60:
            solo_bot[key] = DEFAULT_SETTINGS["soloBot"][key]
        else:
            solo_bot[key] = float(value)
    for key in ("nameColor", "color", "highlight", "spawnAnnouncementColor", "returnAnnouncementColor"):
        value = solo_bot.get(key)
        if (
            not isinstance(value, list)
            or len(value) != 3
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0 for v in value)
        ):
            solo_bot[key] = copy.deepcopy(DEFAULT_SETTINGS["soloBot"][key])
        else:
            solo_bot[key] = [float(v) for v in value]

    # Optional quiz configuration.
    quiz = result["quiz"]
    if not isinstance(quiz.get("enable"), bool):
        quiz["enable"] = DEFAULT_SETTINGS["quiz"]["enable"]
    if not isinstance(quiz.get("announcerName"), str):
        quiz["announcerName"] = DEFAULT_SETTINGS["quiz"]["announcerName"]
    for key in ("firstQuestionDelaySeconds", "intervalSeconds", "answerWindowSeconds", "rewardCoins"):
        value = quiz.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            quiz[key] = DEFAULT_SETTINGS["quiz"][key]

    # Discord configuration is read by mods/plugins/discord_bot.py.
    discordbot = result["discordbot"]
    for key in ("enable", "webEnable"):
        if not isinstance(discordbot.get(key), bool):
            discordbot[key] = DEFAULT_SETTINGS["discordbot"][key]
    for key in ("token", "serverID", "guildID", "logsChannelID", "liveStatsChannelID", "prefix", "cmd_prefix", "webApiURL", "webApiKey"):
        value = discordbot.get(key)
        if value is None:
            discordbot[key] = DEFAULT_SETTINGS["discordbot"].get(key, "")
        elif not isinstance(value, (str, int)) or isinstance(value, bool):
            discordbot[key] = DEFAULT_SETTINGS["discordbot"].get(key, "")
        else:
            discordbot[key] = str(value)
    admin_users = discordbot.get("adminUsers")
    if not isinstance(admin_users, list):
        discordbot["adminUsers"] = []
    else:
        discordbot["adminUsers"] = [
            value for value in admin_users
            if not isinstance(value, bool) and isinstance(value, (str, int)) and str(value).strip().isdigit()
        ]
    interval = discordbot.get("webUpdateSeconds")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 5 <= interval <= 3600:
        discordbot["webUpdateSeconds"] = DEFAULT_SETTINGS["discordbot"]["webUpdateSeconds"]
    else:
        discordbot["webUpdateSeconds"] = int(interval)

    return result


def _write_json_atomically(data: dict[str, Any]) -> None:
    """Write beside setting.json and replace it atomically."""
    directory = os.path.dirname(os.path.abspath(SETTINGS_PATH))
    os.makedirs(directory, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".setting-",
            suffix=".tmp",
            delete=False,
        ) as setting_file:
            temporary_path = setting_file.name
            json.dump(data, setting_file, indent=4, ensure_ascii=False)
            setting_file.write("\n")
            setting_file.flush()
            os.fsync(setting_file.fileno())
        os.replace(temporary_path, SETTINGS_PATH)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass


def _ensure_file_exists() -> None:
    if os.path.exists(SETTINGS_PATH):
        return
    try:
        _write_json_atomically(copy.deepcopy(DEFAULT_SETTINGS))
        _LOG.info("Created default settings file at %s", SETTINGS_PATH)
    except OSError:
        _LOG.warning("Could not create settings file at %s.", SETTINGS_PATH, exc_info=True)


@lru_cache(maxsize=1)
def get_settings_data() -> dict[str, Any]:
    """Return the normalized settings dictionary from mods/setting.json."""
    _ensure_file_exists()
    if not os.path.exists(SETTINGS_PATH):
        return copy.deepcopy(DEFAULT_SETTINGS)

    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as setting_file:
            data = json.load(setting_file)
    except (OSError, json.JSONDecodeError):
        _LOG.warning(
            "Could not read %s; using in-memory defaults without overwriting it.",
            SETTINGS_PATH,
            exc_info=True,
        )
        return copy.deepcopy(DEFAULT_SETTINGS)

    if not isinstance(data, dict):
        _LOG.warning("Settings root must be a JSON object; using in-memory defaults.")
        return copy.deepcopy(DEFAULT_SETTINGS)
    return _normalise_settings(data)


def get_bool(key: str, default: bool = False) -> bool:
    """Return a boolean setting, falling back safely for missing/wrong values."""
    value = get_settings_data().get(key, default)
    return value if isinstance(value, bool) else default


def get_value(key: str, default: Any = None) -> Any:
    """Return a top-level setting, falling back when absent."""
    return get_settings_data().get(key, default)


def get_section(name: str) -> dict[str, Any]:
    """Return a shallow copy of a named settings section."""
    value = get_settings_data().get(name, {})
    return dict(value) if isinstance(value, dict) else {}


def refresh_cache() -> None:
    """Discard cached settings and reload them from setting.json."""
    get_settings_data.cache_clear()
    get_settings_data()


def commit(data: dict[str, Any]) -> None:
    """Validate and persist settings, preserving unknown configuration keys."""
    if not isinstance(data, dict):
        raise TypeError("Settings data must be a dictionary.")
    normalized = _normalise_settings(data)
    _write_json_atomically(normalized)
    refresh_cache()


def is_invalid_time_format(time_string: Any, time_format: str = "%H:%M") -> bool:
    """Return True unless a string matches the requested time format."""
    if not isinstance(time_string, str) or not time_string.strip():
        return True
    try:
        datetime.datetime.strptime(time_string.strip(), time_format)
        return False
    except (TypeError, ValueError):
        return True
