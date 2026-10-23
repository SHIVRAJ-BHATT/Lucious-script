# ba_meta require api 9
# ba_meta export babase.Plugin

import sys
from types import ModuleType

# Python 3.13+ removed audioop. Prefer its maintained compatibility package;
# keep the old no-voice fallback for installations that do not use Discord voice.
try:
    import audioop as _audioop  # noqa: F401
except ImportError:
    try:
        import audioop_lts as _audioop  # type: ignore[import-not-found]
        sys.modules.setdefault("audioop", _audioop)
    except ImportError:
        _audioop = ModuleType("audioop")
        _audioop.mul = lambda *a: a[0]  # type: ignore[attr-defined]
        _audioop.tomono = lambda *a: a[0]  # type: ignore[attr-defined]
        sys.modules.setdefault("audioop", _audioop)

import os
import json
import asyncio
import logging

# Hide only the first startup GC diagnostic associated with Discord initialization.
# Subsequent Ballistica GC warnings remain visible.
class _DiscordFirstStartupGCFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.hidden = False

    def filter(self, record):
        if self.hidden or record.name != "ba.gc":
            return True

        message = record.getMessage()
        if (
            "Explicit gc pass handled" in message
            and "Too many objects garbage-collected" in message
        ):
            self.hidden = True
            return False

        return True


_discord_gc_logger = logging.getLogger("ba.gc")
if not any(
    isinstance(item, _DiscordFirstStartupGCFilter)
    for item in _discord_gc_logger.filters
):
    _discord_gc_logger.addFilter(_DiscordFirstStartupGCFilter())


import hashlib
import time
import tempfile
import threading
from pathlib import Path
from threading import Thread
from datetime import datetime, timezone
from collections import deque
from typing import Any, Callable

# Load third-party dependencies from the local vendor folder.
from pathlib import Path as _DiscordPath
_DISCORD_VENDOR_PATH = str(
    _DiscordPath(__file__).resolve().parent / "vendor"
)
if _DISCORD_VENDOR_PATH not in sys.path:
    sys.path.insert(0, _DISCORD_VENDOR_PATH)

import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    module=r"discord(?:\\.|$)",
)

import discord
from discord.ext.commands import Bot
from discord.ui import View, Button

import babase
import bascenev1 as bs
from playersdata import pdata

_LOG = logging.getLogger("bombsquad.discord_bot")

# ---------------- PATHS ----------------
BASE_DIR = Path(__file__).resolve().parent


def _find_mods_dir(start: Path) -> Path:
    """Find the containing mods directory without assuming plugin depth."""
    for parent in (start, *start.parents):
        if (parent / "setting.json").is_file() and (parent / "stats").is_dir():
            return parent
        if (parent / "playersdata").is_dir() and (parent / "stats").is_dir():
            return parent
    # The normal deployment is mods/plugins/discord_bot.py.
    if start.name in {"plugins", "features"}:
        return start.parent
    return start


MODS_DIR = _find_mods_dir(BASE_DIR)
SETTINGS_PATH = MODS_DIR / "setting.json"
STATS_JSON_PATH = MODS_DIR / "stats" / "stats.json"
STATS_MSG_PATH = MODS_DIR / "stats_message.json"
COIN_PATHS = [
    MODS_DIR / "playersdata" / "coins.json",
    MODS_DIR / ".." / ".." / "coin_data.json",
]
COIN_PATHS = [p.resolve() for p in COIN_PATHS]
COIN_DATA_PATH = next((p for p in COIN_PATHS if p.is_file()), COIN_PATHS[0])
COIN_LOCK = threading.RLock()


_JSON_LOAD_ERRORS: set[Path] = set()


def _load_json(path: Path, default: Any) -> Any:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            result = json.load(handle)
        _JSON_LOAD_ERRORS.discard(path.resolve())
        return result
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        _JSON_LOAD_ERRORS.add(path.resolve())
        _LOG.warning("Could not load JSON file %s: %s", path, exc)
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON atomically to avoid truncating live settings/data files."""
    path = Path(path)
    path.parent.mkdir(parents=False, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=4)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Prefer the same config.json path used by the old plugin, but check nearby
# common server-root locations before deciding that no configuration exists.
_legacy_config_path = (BASE_DIR / ".." / ".." / ".." / ".." / "config.json").resolve()
_config_candidates = [
    _legacy_config_path,
    (MODS_DIR.parent.parent.parent / "config.json").resolve(),
    (MODS_DIR.parent.parent / "config.json").resolve(),
    (MODS_DIR.parent / "config.json").resolve(),
]
_seen_config_paths: set[Path] = set()
_config_candidates = [p for p in _config_candidates if not (p in _seen_config_paths or _seen_config_paths.add(p))]
CONFIG_PATH = next((p for p in _config_candidates if p.is_file()), _legacy_config_path)
CONFIG_FILE_FOUND = CONFIG_PATH.is_file()

settings_loaded = _load_json(SETTINGS_PATH, {})
settings = settings_loaded if isinstance(settings_loaded, dict) else {}
SETTINGS_FILE_VALID = SETTINGS_PATH.is_file() and SETTINGS_PATH.resolve() not in _JSON_LOAD_ERRORS and isinstance(settings_loaded, dict)
dc_loaded = settings.get("discordbot", {})
dc = dc_loaded if isinstance(dc_loaded, dict) else {}
raw_admin_ids = dc.get("adminUsers", [])
if not isinstance(raw_admin_ids, list):
    raw_admin_ids = []
ENABLE = dc.get("enable", False) is True
TOKEN = dc.get("token")
TOKEN = TOKEN.strip() if isinstance(TOKEN, str) else ""
LOGS_CHANNEL_ID = None
STATS_CHANNEL_ID = None


def _as_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


LOGS_CHANNEL_ID = _as_int(dc.get("logsChannelID"))
STATS_CHANNEL_ID = _as_int(dc.get("liveStatsChannelID"))
# Discord server/guild ID. Both key names are accepted for compatibility.
DISCORD_SERVER_ID = _as_int(dc.get("serverID")) or _as_int(dc.get("guildID"))
# Normalize IDs now, including older settings files that stored them as strings.
dc["adminUsers"] = [user_id for user_id in (_as_int(value) for value in raw_admin_ids) if user_id is not None]
settings["discordbot"] = dc
if DISCORD_SERVER_ID is None:
    _LOG.warning("discordbot.serverID is not set; Discord commands are not restricted to one server.")
BOT_PREFIX = str(dc.get("prefix", "a!") or "a!")
CMD_PREFIX = str(dc.get("cmd_prefix", "at") or "at")
WEB_ENABLED = dc.get("webEnable", ENABLE) is True
try:
    WEB_UPDATE_INTERVAL = max(5.0, float(dc.get("webUpdateSeconds", 10)))
except (TypeError, ValueError):
    WEB_UPDATE_INTERVAL = 10.0

server_config_loaded = _load_json(CONFIG_PATH, {})
server_config = server_config_loaded if isinstance(server_config_loaded, dict) else {}
CONFIG_JSON_VALID = CONFIG_PATH.is_file() and CONFIG_PATH.resolve() not in _JSON_LOAD_ERRORS and isinstance(server_config_loaded, dict)
if not CONFIG_FILE_FOUND:
    _LOG.warning("config.json was not found in the expected server-root locations; config-writing commands will report this.")


def get_config(key: str, default: Any = None) -> Any:
    return server_config.get(key, default)


# ---------------- WEB ----------------
API_URL = str(dc.get("webApiURL", "http://65.1.65.75:3000/api/update") or "").strip()
API_KEY = str(dc.get("webApiKey", "") or "").strip()
_cached_ip: str | None = None
_ip_lock = threading.Lock()
_ip_refreshing = False
_ip_last_attempt = 0.0
_web_lock = threading.Lock()
_web_last_started = 0.0
_web_inflight = False


def _refresh_public_ip() -> None:
    """Fetch the public IP off-thread; never block the game or Discord loop."""
    global _cached_ip, _ip_refreshing
    try:
        import requests
        response = requests.get("https://api.ipify.org", timeout=3.0)
        response.raise_for_status()
        ip = response.text.strip()
        if ip:
            with _ip_lock:
                _cached_ip = ip
    except Exception as exc:
        _LOG.debug("Public-IP lookup failed: %s", exc)
    finally:
        with _ip_lock:
            _ip_refreshing = False


def get_public_ip() -> str:
    """Return a cached public IP and schedule non-blocking refreshes as needed."""
    global _ip_refreshing, _ip_last_attempt
    now = time.monotonic()
    with _ip_lock:
        if _cached_ip:
            return _cached_ip
        should_start = not _ip_refreshing and now - _ip_last_attempt >= 60.0
        if should_start:
            _ip_refreshing = True
            _ip_last_attempt = now
    if should_start:
        try:
            Thread(target=_refresh_public_ip, name="BombSquadPublicIP", daemon=True).start()
        except Exception:
            with _ip_lock:
                _ip_refreshing = False
            _LOG.debug("Could not start public-IP lookup thread", exc_info=True)
    return "Unknown"


def update_web(players: int, current_map: str, next_map: str) -> None:
    """Send throttled status updates without spawning overlapping requests."""
    global _web_last_started, _web_inflight
    if not WEB_ENABLED:
        return
    if not API_URL or not API_KEY:
        _LOG.debug("Skipping web status update: configure discordbot.webApiURL and webApiKey first")
        return
    now = time.monotonic()
    with _web_lock:
        if _web_inflight or now - _web_last_started < WEB_UPDATE_INTERVAL:
            return
        _web_inflight = True
        _web_last_started = now

    def _send() -> None:
        global _web_inflight
        try:
            import requests
            response = requests.post(
                API_URL,
                json={
                    "name": get_config("party_name", "SERVER"),
                    "ip": get_public_ip(),
                    "port": get_config("port", 43210),
                    "players": int(players),
                    "currentMap": str(current_map),
                    "nextMap": str(next_map),
                    "status": "online",
                    "key": API_KEY,
                },
                timeout=(2.0, 3.0),
            )
            response.raise_for_status()
        except Exception as exc:
            _LOG.debug("Server web-status update failed: %s", exc)
        finally:
            with _web_lock:
                _web_inflight = False

    Thread(target=_send, name="BombSquadWebStatus", daemon=True).start()


# ---------------- UTILS ----------------
def clean(value: Any) -> str:
    text = str(value or "")
    return "".join(ch for ch in text if not (0xE000 <= ord(ch) <= 0xF8FF) and ch not in "\x00\x1a")


def hash_msg(message: str) -> str:
    return hashlib.md5(message.encode("utf-8", errors="replace")).hexdigest()


logs: list[str] = []
logs_lock = threading.Lock()
LOGS_MAX = 500


def push_log(message: str) -> None:
    message = clean(message).strip()
    if not message:
        return
    with logs_lock:
        if logs and logs[-1] == message:
            return
        logs.append(message)
        if len(logs) > LOGS_MAX:
            del logs[: len(logs) - LOGS_MAX]


def run_on_bs_thread(callback: Callable[[], Any], action_name: str) -> bool:
    """Schedule a callback on BombSquad's logic thread with visible errors."""
    def _wrapped() -> None:
        try:
            callback()
        except Exception:
            _LOG.exception("BombSquad callback failed (%s)", action_name)
            push_log(f"ERROR: {action_name} failed; check server log.")

    try:
        bs.pushcall(_wrapped, from_other_thread=True)
        return True
    except Exception:
        _LOG.exception("Could not queue BombSquad callback (%s)", action_name)
        return False


chat_buffer = deque(maxlen=15)
chat_hashes = deque(maxlen=50)
_chat_hash_set: set[str] = set()
chat_lock = threading.Lock()
stats_lock = threading.RLock()
stats: dict[str, Any] = {}
player_info: dict[str, Any] = {}
stats_message = None
chat_message = None
_last_stats = ""
_background_tasks: dict[str, asyncio.Task[Any]] = {}
_persistent_view_registered = False


def add_chat(message: str) -> None:
    """Normalize a game chat line and keep a short, deduplicated history."""
    message = clean(message).strip()
    if not message:
        return
    if ">" in message:
        message = message.rsplit(">", 1)[-1].strip()
    message = message.replace(" - ", ":").strip()
    if ":" not in message:
        return
    try:
        remaining, text = message.rsplit(":", 1)
        text = text.strip()
        name = remaining.rsplit(":", 1)[-1].strip()
        name = name.replace("**", "").strip()
        if " " in name:
            name = name.split()[-1]
    except Exception:
        return
    if not name or not text:
        return
    display = f"{name}: {text}"
    key = hash_msg(display.casefold())
    with chat_lock:
        if key in _chat_hash_set:
            return
        if len(chat_hashes) == chat_hashes.maxlen and chat_hashes:
            _chat_hash_set.discard(chat_hashes[0])
        chat_hashes.append(key)
        _chat_hash_set.add(key)
        chat_buffer.append(display)


def save_stats_msg_id(message_id: int | None) -> None:
    try:
        _write_json_atomic(STATS_MSG_PATH, {"message_id": message_id})
    except Exception:
        _LOG.exception("Could not save Discord stats-message ID")


def load_stats_msg_id() -> int | None:
    data = _load_json(STATS_MSG_PATH, {})
    if not isinstance(data, dict):
        return None
    return _as_int(data.get("message_id"))


def load_lb() -> list[dict[str, Any]]:
    data = _load_json(STATS_JSON_PATH, {})
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("stats"), dict):
        data = data["stats"]
    players = []
    for player in data.values():
        if not isinstance(player, dict):
            continue
        try:
            players.append({
                "name": clean(player.get("name", "Unknown"))[:80] or "Unknown",
                "score": int(player.get("scores", 0) or 0),
                "kills": int(player.get("kills", 0) or 0),
                "deaths": int(player.get("deaths", 0) or 0),
                "kd": round(float(player.get("kd", 0) or 0), 2),
            })
        except (TypeError, ValueError):
            continue
    players.sort(key=lambda player: player["score"], reverse=True)
    return players[:5]


# Coin files in this project have existed in more than one layout. Update the
# existing layout in-place so extra metadata is not discarded.
def get_coin_balance(pbid: str) -> int:
    with COIN_LOCK:
        data = _load_json(COIN_DATA_PATH, {})
        if not isinstance(data, dict):
            return 0
        container = data.get("coins") if isinstance(data.get("coins"), dict) and pbid not in data else data
        value = container.get(pbid, 0) if isinstance(container, dict) else 0
        if isinstance(value, dict):
            value = value.get("coins", 0)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0


def adjust_coin_balance(pbid: str, delta: int) -> int:
    with COIN_LOCK:
        data = _load_json(COIN_DATA_PATH, {})
        if COIN_DATA_PATH.resolve() in _JSON_LOAD_ERRORS:
            raise ValueError(f"Coin file is invalid JSON: {COIN_DATA_PATH}; it was not overwritten")
        if not isinstance(data, dict):
            raise ValueError(f"Coin file must contain a JSON object: {COIN_DATA_PATH}")
        use_nested_container = isinstance(data.get("coins"), dict) and pbid not in data
        container = data["coins"] if use_nested_container else data
        old_value = container.get(pbid, 0)
        if isinstance(old_value, dict):
            old_balance = int(old_value.get("coins", 0) or 0)
            new_balance = max(0, old_balance + delta)
            record = dict(old_value)
            record["coins"] = new_balance
            container[pbid] = record
        else:
            try:
                old_balance = int(old_value or 0)
            except (TypeError, ValueError):
                old_balance = 0
            new_balance = max(0, old_balance + delta)
            container[pbid] = new_balance
        if use_nested_container:
            data["coins"] = container
        _write_json_atomic(COIN_DATA_PATH, data)
        return new_balance


# ---------------- DISCORD ----------------
BOT_PREFIX = dc.get("prefix", "a!")
CMD_PREFIX = dc.get("cmd_prefix", "at")

intents = discord.Intents.default()
intents.message_content = True
class _DiscordOptionalVoiceWarningFilter(logging.Filter):
    _ignored_messages = {
        "PyNaCl is not installed, voice will NOT be supported",
        "davey is not installed, voice will NOT be supported",
    }

    def filter(self, record):
        return not (
            record.name == "discord.client"
            and record.getMessage() in self._ignored_messages
        )


_discord_client_logger = logging.getLogger("discord.client")
if not any(
    isinstance(item, _DiscordOptionalVoiceWarningFilter)
    for item in _discord_client_logger.filters
):
    _discord_client_logger.addFilter(_DiscordOptionalVoiceWarningFilter())

client = Bot(command_prefix=BOT_PREFIX, intents=intents)

class ChatBtn(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="LIVE CHAT", style=discord.ButtonStyle.primary, custom_id="chat_toggle_btn")
    async def toggle(self, interaction: discord.Interaction, button: Button):
        global chat_message
        if DISCORD_SERVER_ID is not None and (
            interaction.guild is None or interaction.guild.id != DISCORD_SERVER_ID
        ):
            await interaction.response.send_message(
                "This dashboard is configured for another Discord server.", ephemeral=True
            )
            return
        await interaction.response.defer()
        if chat_message is not None:
            try:
                await chat_message.delete()
            except discord.HTTPException:
                pass
            chat_message = None
            return
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("This channel is not available.", ephemeral=True)
            return
        try:
            chat_message = await channel.send(embed=build_chat())
        except discord.HTTPException:
            _LOG.exception("Could not create live-chat message")
            await interaction.followup.send("Could not create the live-chat message; check channel permissions.", ephemeral=True)

class TopListView(View):
    def __init__(self, pages):
        super().__init__(timeout=120)
        self.pages = pages
        self.index = 0

    async def update(self, interaction):
        await interaction.response.edit_message(
            embed=self.pages[self.index],
            view=self
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: Button):
        if self.index > 0:
            self.index -= 1
        await self.update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self.update(interaction)

class ChatHistoryView(View):
    def __init__(self, pages):
        super().__init__(timeout=120)
        self.pages = pages
        self.index = 0

    async def update(self, interaction):
        await interaction.response.edit_message(
            embed=self.pages[self.index],
            view=self
        )

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: Button):
        if self.index > 0:
            self.index -= 1
        await self.update(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: Button):
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self.update(interaction)

# ---------------- EMBEDS ----------------
def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "..."


def _stats_snapshot() -> dict[str, Any]:
    with stats_lock:
        return {
            "roster": dict(stats.get("roster", {})),
            "playlist": dict(stats.get("playlist", {})),
        }


def _online_client_id_for_account(account_id: str) -> int | None:
    """Return the cached client ID for a currently connected V2 account."""
    roster = _stats_snapshot().get("roster", {})
    info = roster.get(account_id)
    if not isinstance(info, dict):
        return None
    try:
        client_id = int(info.get("client_id"))
    except (TypeError, ValueError, OverflowError):
        return None
    return client_id if client_id >= 0 else None


def _queue_custom_tag_refresh(account_id: str) -> bool:
    """Refresh a connected player's custom tag through the game logic thread."""
    client_id = _online_client_id_for_account(account_id)
    if client_id is None:
        return False

    def _refresh() -> None:
        from spazmod import tag as tag_system
        tag_system.refresh_client_tag(client_id)

    return run_on_bs_thread(_refresh, "refresh custom tag")


def _queue_custom_effect_refresh(account_id: str) -> bool:
    """Refresh a connected player's saved effects using the server's own handler."""
    client_id = _online_client_id_for_account(account_id)
    if client_id is None:
        return False

    def _refresh() -> None:
        from chathandle.chatcommands.commands.management import _refresh_effects_for_client
        _refresh_effects_for_client(client_id, account_id)

    return run_on_bs_thread(_refresh, "refresh custom effects")


def build_stats() -> discord.Embed:
    snapshot = _stats_snapshot()
    server_name = _truncate(get_config("party_name", "SERVER"), 250) or "SERVER"
    embed = discord.Embed(
        title=f"SERVER | {server_name}",
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc),
    )
    playlist = snapshot.get("playlist", {})
    current_map = _truncate(playlist.get("current", "-"), 500)
    next_map = _truncate(playlist.get("next", "-"), 500)
    embed.add_field(name="🗺️ Maps", value=f"**Current:** {current_map}\n**Next:** {next_map}", inline=False)
    embed.add_field(name="🖥️ Server", value=f"**IP:** `{_truncate(get_public_ip(), 80)}`\n**Port:** `{get_config('port', 43210)}`", inline=False)

    roster = snapshot.get("roster", {})
    embed.add_field(name="👥 Players", value=f"{len(roster)} Online", inline=False)
    if roster:
        roster_lines = [f"• {_truncate(clean(info.get('name', 'Unknown')), 80)} `[{_truncate(pbid, 64)}]`" for pbid, info in list(roster.items())[:100] if isinstance(info, dict)]
        roster_text = "\n".join(roster_lines) or "*Server is empty...*"
        embed.add_field(name="📋 Player List", value=_truncate(roster_text, 1000), inline=False)
    else:
        embed.add_field(name="📋 Player List", value="*Server is empty...*", inline=False)

    leaderboard = load_lb()
    if leaderboard:
        leaderboard_lines = []
        for rank, player in enumerate(leaderboard, 1):
            leaderboard_lines.append(
                f"**{rank}. {_truncate(player['name'], 80)}**\n"
                f"Score: {player['score']} | K: {player['kills']} | D: {player['deaths']} | KD: {player['kd']}"
            )
        leaderboard_text = "\n\n".join(leaderboard_lines)
    else:
        leaderboard_text = "No data available"
    embed.add_field(name="🏆 Leaderboard", value=_truncate(leaderboard_text, 1000), inline=False)
    return embed


def build_chat() -> discord.Embed:
    embed = discord.Embed(title="LIVE IN-GAME CHAT", color=discord.Color.blurple())
    with chat_lock:
        messages = list(chat_buffer)
    lines = []
    for message in messages:
        if ":" in message:
            name, text = message.split(":", 1)
            lines.append(f"• **{_truncate(name.strip(), 80)}**: {_truncate(text.strip(), 300)}")
        else:
            lines.append(f"• {_truncate(message, 350)}")
    description = "\n".join(lines) if lines else "No chat yet..."
    embed.description = _truncate(description, 4000)
    return embed


# ---------------- LOOPS ----------------
async def _get_channel(channel_id: int | None):
    if channel_id is None:
        return None
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            _LOG.warning("Discord channel %s could not be fetched", channel_id)
            return None
    if DISCORD_SERVER_ID is not None:
        guild = getattr(channel, "guild", None)
        if guild is None or guild.id != DISCORD_SERVER_ID:
            _LOG.error(
                "Configured channel %s is not in Discord server %s",
                channel_id,
                DISCORD_SERVER_ID,
            )
            return None
    return channel


async def stats_loop():
    global _last_stats, stats_message
    while not client.is_closed():
        try:
            if stats_message is None:
                await prepare_message()
            elif stats_message is not None:
                embed = build_stats()
                rendered = str(embed.to_dict())
                if rendered != _last_stats:
                    await stats_message.edit(embed=embed)
                    _last_stats = rendered
        except discord.NotFound:
            stats_message = None
            save_stats_msg_id(None)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOG.exception("Stats dashboard update failed")
        await asyncio.sleep(8)


async def chat_loop():
    global chat_message
    while not client.is_closed():
        await asyncio.sleep(2)
        if chat_message is not None:
            try:
                await chat_message.edit(embed=build_chat())
            except discord.NotFound:
                # It was deleted manually; turn off live chat until the button is pressed again.
                chat_message = None
            except asyncio.CancelledError:
                raise
            except discord.HTTPException as exc:
                _LOG.debug("Live-chat edit failed: %s", exc)


async def log_loop():
    channel = await _get_channel(LOGS_CHANNEL_ID)
    while not client.is_closed():
        await asyncio.sleep(4)
        if channel is None:
            channel = await _get_channel(LOGS_CHANNEL_ID)
            if channel is None:
                continue
        with logs_lock:
            pending = logs[:20]
            if pending:
                del logs[:len(pending)]
        if not pending:
            continue
        try:
            await channel.send(embed=discord.Embed(title="Live Logs", description=_truncate("\n".join(pending), 4000), color=0x2F3136))
        except asyncio.CancelledError:
            raise
        except discord.HTTPException as exc:
            _LOG.warning("Could not send Discord log batch: %s", exc)
            with logs_lock:
                logs[0:0] = pending
                if len(logs) > LOGS_MAX:
                    del logs[LOGS_MAX:]


async def prepare_message():
    global stats_message
    channel = await _get_channel(STATS_CHANNEL_ID)
    if channel is None:
        _LOG.warning("Live-stats channel is not configured or inaccessible")
        return

    message_id = load_stats_msg_id()
    if message_id is not None:
        try:
            existing = await channel.fetch_message(message_id)
            await existing.edit(embed=build_stats(), view=ChatBtn())
            stats_message = existing
            return
        except discord.NotFound:
            _LOG.info("Saved live-stats message no longer exists; creating a replacement")
        except discord.HTTPException as exc:
            _LOG.warning("Could not refresh saved live-stats message: %s", exc)

    try:
        stats_message = await channel.send(embed=build_stats(), view=ChatBtn())
        save_stats_msg_id(stats_message.id)
    except discord.HTTPException:
        stats_message = None
        _LOG.exception("Could not create live-stats dashboard; check channel permissions")


@client.event
async def on_message(message):
    if message.author.bot or not message.content:
        return
    if DISCORD_SERVER_ID is not None and (
        message.guild is None or message.guild.id != DISCORD_SERVER_ID
    ):
        return

    # Keep Discord bot commands from being echoed into the game chat.
    is_bot_command = message.content.startswith(BOT_PREFIX)
    if message.channel.id == LOGS_CHANNEL_ID and not is_bot_command:
        formatted_message = f"{message.author.display_name}: {message.content}"
        run_on_bs_thread(lambda: bs.chatmessage(formatted_message), "Discord-to-game chat")

    await client.process_commands(message)


@client.event
async def on_ready():
    global _persistent_view_registered
    _LOG.info("Discord bot ready as %s", client.user)
    if not _persistent_view_registered:
        try:
            client.add_view(ChatBtn())
            _persistent_view_registered = True
        except Exception:
            _LOG.exception("Could not register persistent live-chat button")
    await prepare_message()
    # on_ready can fire again after reconnect; start each loop at most once.
    for task_name, coroutine in (
        ("stats", stats_loop()),
        ("chat", chat_loop()),
        ("logs", log_loop()),
    ):
        existing_task = _background_tasks.get(task_name)
        if existing_task is None or existing_task.done():
            _background_tasks[task_name] = asyncio.create_task(coroutine, name=f"discord-bot-{task_name}")
        else:
            coroutine.close()


# ================= COMMANDS ================= #

@client.command(name="sc")
async def server_code(ctx):
    if DISCORD_SERVER_ID is not None and (
        ctx.guild is None or ctx.guild.id != DISCORD_SERVER_ID
    ):
        return
    server_name = get_config("party_name", "SERVER")
    await ctx.send(f">>> {CMD_PREFIX} - {server_name}")


@client.command(name=CMD_PREFIX)
async def server_cmd(ctx, subcommand: str = None, *, arg: str = None):

    # Only respond in the configured Discord server when serverID is set.
    if DISCORD_SERVER_ID is not None and (
        ctx.guild is None or ctx.guild.id != DISCORD_SERVER_ID
    ):
        return

    # ===== PERMISSION CHECK ===== #
    raw_admin_ids = settings.get("discordbot", {}).get("adminUsers", [])
    admin_ids = {_as_int(user_id) for user_id in raw_admin_ids}
    admin_ids.discard(None)

    if ctx.author.id not in admin_ids:
        await ctx.send(">>> You don't have permission to use this bot ?")
        return

    try:
        global stats

        if not subcommand:
            await ctx.send(
                f"Use: `{BOT_PREFIX}{CMD_PREFIX} info` or "
                f"`{BOT_PREFIX}{CMD_PREFIX} stats <pbid>`"
            )
            return

        subcommand = subcommand.lower()

        # ================= INFO ================= #
        if subcommand == "info":
            server_name = get_config("party_name", "Unknown Server")
            ip = get_public_ip()
            port = get_config("port", 43210)

            roster = _stats_snapshot().get("roster", {})
            current_players = len(roster)
            max_players = get_config("max_party_size", 16)

            playlist = _stats_snapshot().get("playlist", {})
            current_map = playlist.get("current", "-")
            next_map = playlist.get("next", "-")

            embed = discord.Embed(
                title="🖥️ Server Info",
                color=discord.Color.green()
            )
            embed.add_field(name="Server Name", value=server_name, inline=False)
            embed.add_field(name="IP", value=f"`{ip}`", inline=True)
            embed.add_field(name="Port", value=f"`{port}`", inline=True)
            embed.add_field(name="Players", value=f"`{current_players}/{max_players}`", inline=False)
            embed.add_field(name="Current Map", value=current_map, inline=True)
            embed.add_field(name="Next Map", value=next_map, inline=True)
            embed.set_footer(text=f"Use {BOT_PREFIX}{CMD_PREFIX} stats <pbid>")

            await ctx.send(embed=embed)
            return

        # ================= STATS ================= #
        elif subcommand == "stats":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} stats <pbid>`")
                return

            pbid = arg.strip()

            try:
                with open(STATS_JSON_PATH) as f:
                    data = json.load(f)
                if "stats" in data:
                    data = data["stats"]
            except Exception as e:
                await ctx.send("Stats file not found ?")
                print("Stats error:", e)
                return

            if pbid not in data:
                await ctx.send("Player not found ?")
                return

            p = data[pbid]
            name = clean(p.get("name", "Unknown"))
            rank = p.get("rank", "-")
            score = p.get("scores", 0)
            kills = p.get("kills", 0)
            deaths = p.get("deaths", 0)
            kd = p.get("kd", 0)

            coins = get_coin_balance(pbid)

            server_name = get_config("party_name", "SERVER")
            embed = discord.Embed(
                title=server_name,
                description=(
                    f"Name - {name}\n"
                    f"Pb - {pbid}\n"
                    f"Rank - {rank}\n"
                    f"Score - {score}\n"
                    f"Kills - {kills}\n"
                    f"Death - {deaths}\n"
                    f"Kd - {kd}\n"
                    f"Coins - {coins}"
                ),
                color=discord.Color.gold()
            )
            await ctx.send(embed=embed)
            return

        # ================= TOPLIST ================= #
        elif subcommand == "toplist":
            try:
                with open(STATS_JSON_PATH) as f:
                    data = json.load(f)
                if "stats" in data:
                    data = data["stats"]
            except Exception as e:
                await ctx.send("Stats file not found ?")
                print("Toplist error:", e)
                return

            players = []
            for pbid, p in data.items():
                players.append({
                    "name": clean(p.get("name", "Unknown")),
                    "pbid": pbid,
                    "score": int(p.get("scores", 0)),
                    "kills": int(p.get("kills", 0)),
                    "deaths": int(p.get("deaths", 0))
                })

            players.sort(key=lambda x: x["score"], reverse=True)

            per_page = 5
            pages = []
            server_name = get_config("party_name", "SERVER")
            total_players = min(len(players), 10)
            total_pages = (total_players + per_page - 1) // per_page

            for i in range(0, total_players, per_page):
                chunk = players[i:i + per_page]
                desc = ""
                for idx, p in enumerate(chunk, start=i + 1):
                    desc += (
                        f"Top {idx} - {p['name']}\n"
                        f"Pb - {p['pbid']}\n"
                        f"Kills - {p['kills']} | Score - {p['score']} | Death - {p['deaths']}\n\n"
                    )

                page_num = (i // per_page) + 1
                embed = discord.Embed(
                    title=f"{server_name} | Page {page_num}/{total_pages}",
                    description=desc,
                    color=discord.Color.gold()
                )
                pages.append(embed)

            if not pages:
                await ctx.send("No data ?")
                return

            view = TopListView(pages)
            await ctx.send(embed=pages[0], view=view)
            return

        # ================= GET ROLES ================= #
        elif subcommand == "getroles":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} getroles <pbid>`")
                return

            pbid = arg.strip()
            roles = pdata.get_player_roles(pbid)

            if not roles:
                await ctx.send("No roles found ?")
                return

            name = "Unknown"
            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> {name} ({pbid}) has **{', '.join(r.upper() for r in roles)}** in {server_name}")
            return

# ================= ADD ROLE ================= #
        elif subcommand == "addrole":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addrole <pbid> <rolename>`")
                return

            parts = arg.split()
            if len(parts) < 2:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addrole <pbid> <rolename>`")
                return

            pbid = parts[0]
            input_role = parts[1]

            roles = pdata.get_roles()

            # -------- CASE INSENSITIVE ROLE MATCH -------- #
            role_name = None
            for r in roles:
                if r.casefold() == input_role.casefold():
                    role_name = r
                    break

            if role_name is None:
                await ctx.send("Role not found.")
                return

            if role_name.casefold() == "owner":
                await ctx.send("The OWNER role is protected by the server. Assign it through the in-game owner account.")
                return

            if pbid in roles[role_name].get("ids", []):
                await ctx.send("Already has this role.")
                return

            try:
                saved_role = pdata.add_player_role(role_name, pbid)
            except Exception as e:
                await ctx.send(f"Role assignment failed: {_truncate(e, 250)}")
                return
            if saved_role is None:
                await ctx.send("Role assignment failed. Check that the PBID is a valid V2 account ID (a-...) and the role exists.")
                return

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Added **{saved_role.upper()}** to {pbid} in {server_name}")
            return

        # ================= REMOVE ROLE ================= #
        elif subcommand == "removerole":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removerole <pbid> <rolename>`")
                return

            parts = arg.split()
            if len(parts) < 2:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removerole <pbid> <rolename>`")
                return

            pbid = parts[0]
            input_role = parts[1]
            roles = pdata.get_roles()
            role_name = next((stored for stored in roles if stored.casefold() == input_role.casefold()), None)

            if role_name is None:
                await ctx.send("Role not found.")
                return
            if role_name.casefold() == "owner" or pdata.is_owner_account_id(pbid):
                await ctx.send("The OWNER role and protected owner account cannot be modified through this command.")
                return
            if pbid not in roles[role_name].get("ids", []):
                await ctx.send("Player doesn't have this role.")
                return

            try:
                removed_role = pdata.remove_player_role(role_name, pbid)
            except Exception as e:
                await ctx.send(f"Role removal failed: {_truncate(e, 250)}")
                return
            if removed_role is None:
                await ctx.send("Role removal failed; verify the PBID and role assignment.")
                return
            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Removed **{removed_role.upper()}** from {pbid} in {server_name}")
            return

        # ================= ADD TAG ================= #
        elif subcommand == "addtag":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addtag <pbid> <tag>`")
                return

            legacy_anim = None
            legacy_parts = arg.split(maxsplit=2)
            if len(legacy_parts) >= 3 and legacy_parts[1].lstrip("+-").isdigit():
                # Old syntax: <pbid> <anim> <tag>. Current pdata stores text-only tags.
                pbid, legacy_anim, tag = legacy_parts[0], legacy_parts[1], legacy_parts[2].strip()
            else:
                parts = arg.split(maxsplit=1)
                if len(parts) < 2:
                    await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addtag <pbid> <tag>`")
                    return
                pbid, tag = parts[0], parts[1].strip()

            try:
                saved = pdata.set_custom_tag(pbid, tag)
            except Exception as e:
                await ctx.send(f"Error saving custom tag: {_truncate(e, 250)}")
                return
            if not saved:
                await ctx.send("Could not save tag. Use a valid V2 PBID (a-...) and a tag of 1-32 characters.")
                return

            _queue_custom_tag_refresh(pbid)
            name = "Unknown"
            try:
                with open(STATS_JSON_PATH, encoding="utf-8") as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            note = " (legacy animation value ignored; current server tags are text-only)" if legacy_anim is not None else ""
            await ctx.send(f">>> Custom tag **{tag}** saved for {name} ({pbid}) in {server_name}{note}")
            return

        # ================= REMOVE TAG ================= #
        elif subcommand == "removetag":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removetag <pbid>`")
                return

            pbid = arg.strip()
            try:
                old_tag = pdata.get_custom_tag(pbid)
                if not old_tag:
                    await ctx.send("Player doesn't have a saved custom tag, or the PBID is invalid.")
                    return
                removed = pdata.remove_custom_tag(pbid)
            except Exception as e:
                await ctx.send(f"Error removing custom tag: {_truncate(e, 250)}")
                return
            if not removed:
                await ctx.send("Custom tag could not be removed.")
                return

            _queue_custom_tag_refresh(pbid)
            name = "Unknown"
            try:
                with open(STATS_JSON_PATH, encoding="utf-8") as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Removed custom tag **{old_tag}** from {name} ({pbid}) in {server_name}")
            return

        # ================= EFFECT LIST ================= #
        elif subcommand == "effectlist":
            try:
                from spazmod import spaz_effects
                effects = sorted(spaz_effects.EFFECT_NAMES)

                if not effects:
                    await ctx.send("No effects found ?")
                    return

                per_line = 5
                lines = []
                for i in range(0, len(effects), per_line):
                    lines.append(" | ".join(effects[i:i + per_line]))

                server_name = get_config("party_name", "SERVER")
                embed = discord.Embed(
                    title=f"{server_name} Effects List",
                    description="\n".join(lines),
                    color=discord.Color.purple()
                )
                await ctx.send(embed=embed)
            except Exception as e:
                await ctx.send("Error loading effects ?")
                print("EffectList Error:", e)
            return

        # ================= ADD EFFECT ================= #
        elif subcommand == "addeffect":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addeffect <pbid> <effectname>`")
                return

            parts = arg.split()
            if len(parts) < 2:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addeffect <pbid> <effectname>`")
                return

            pbid = parts[0]
            effect_name = parts[1].lower()

            try:
                from spazmod import spaz_effects
                normalized_effect = spaz_effects.normalize_effect_name(effect_name)
                if normalized_effect is None:
                    await ctx.send(f"Unsupported effect '{effect_name}'. Use `{BOT_PREFIX}{CMD_PREFIX} effectlist` to see valid names.")
                    return
                if not pdata.set_effect(normalized_effect, pbid):
                    await ctx.send("Effect was not saved. Check that the PBID is a valid V2 account ID (a-...).")
                    return
                _queue_custom_effect_refresh(pbid)
                effect_name = normalized_effect
            except Exception as e:
                await ctx.send(f"Error adding effect: {_truncate(e, 250)}")
                return

            name = "Unknown"
            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Successfully added **{effect_name}** effect to {name} ({pbid}) in {server_name}")
            return

        # ================= REMOVE EFFECT ================= #
        elif subcommand == "removeeffect":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removeeffect <pbid> <effectname>`")
                return

            parts = arg.split()
            if len(parts) < 2:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removeeffect <pbid> <effectname>`")
                return

            pbid = parts[0]
            effect_name = parts[1].lower()

            try:
                from spazmod import spaz_effects
                normalized_effect = spaz_effects.normalize_effect_name(effect_name)
                if normalized_effect is None:
                    await ctx.send(f"Unsupported effect '{effect_name}'. Use `{BOT_PREFIX}{CMD_PREFIX} effectlist` to see valid names.")
                    return
                if normalized_effect not in pdata.get_custom_effects(pbid):
                    await ctx.send("Player doesn't have this saved effect, or the PBID is invalid.")
                    return
                if not pdata.remove_effect(pbid, normalized_effect):
                    await ctx.send("Effect could not be removed.")
                    return
                _queue_custom_effect_refresh(pbid)
            except Exception as e:
                await ctx.send(f"Error removing effect: {_truncate(e, 250)}")
                return

            name = "Unknown"
            try:
                with open(STATS_JSON_PATH, encoding="utf-8") as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Successfully removed **{normalized_effect}** effect from {name} ({pbid}) in {server_name}")
            return

        # ================= ADD COIN ================= #
        elif subcommand == "addcoin":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addcoin <pbid> <amount>`")
                return

            parts = arg.split()
            if len(parts) < 2:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} addcoin <pbid> <amount>`")
                return

            pbid = parts[0]
            try:
                amount = int(parts[1])
            except Exception:
                await ctx.send("Amount must be number ?")
                return

            if amount <= 0:
                await ctx.send("Amount must be greater than 0 ?")
                return

            try:
                new_balance = adjust_coin_balance(pbid, amount)
            except Exception as e:
                await ctx.send(f"Error updating coin file ?: {_truncate(e, 300)}")
                return

            name = "Unknown"
            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Added **{amount} coins** to {name} ({pbid}) in {server_name}")
            return

        # ================= REMOVE COIN ================= #
        elif subcommand == "removecoin":
            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removecoin <pbid> <amount>`")
                return

            parts = arg.split()
            if len(parts) < 2:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} removecoin <pbid> <amount>`")
                return

            pbid = parts[0]
            try:
                amount = int(parts[1])
            except Exception:
                await ctx.send("Amount must be number ?")
                return

            if amount <= 0:
                await ctx.send("Amount must be greater than 0 ?")
                return

            current_balance = get_coin_balance(pbid)
            if current_balance <= 0:
                await ctx.send("Player has no coins ?")
                return
            try:
                new_balance = adjust_coin_balance(pbid, -amount)
            except Exception as e:
                await ctx.send(f"Error updating coin file ?: {_truncate(e, 300)}")
                return

            name = "Unknown"
            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except Exception:
                pass

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Removed **{amount} coins** from {name} ({pbid}) in {server_name}")
            return

        # ================= QUIT ================= #
        elif subcommand == "quit":
            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Requested graceful shutdown for **{server_name}**. Automatic restart depends on your server manager.")
            quit_func = getattr(babase, "quit", None)
            quit_type = getattr(getattr(babase, "QuitType", None), "HARD", None)
            if callable(quit_func):
                run_on_bs_thread(lambda: quit_func(quit_type=quit_type), "graceful server shutdown")
            else:
                await ctx.send("This build does not expose babase.quit(); no shutdown was initiated.")
            return

# ================= BAN PLAYER ================= #
        elif subcommand == "ban":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} ban <pbid>`")
                return

            pbid = arg.strip()

            try:
                # Preserve the old intended duration: 999 days, converted to this server API's hours.
                saved = pdata.ban_player(
                    pbid,
                    reason="Banned via Discord",
                    duration_hours=999 * 24,
                    client_id_at_ban=_online_client_id_for_account(pbid),
                )
            except Exception as e:
                await ctx.send(f"Ban failed: {_truncate(e, 250)}")
                return
            if not saved:
                await ctx.send("Ban failed. Check that the PBID is a valid V2 account ID and is not the protected owner.")
                return
            online_client_id = _online_client_id_for_account(pbid)
            if online_client_id is not None:
                run_on_bs_thread(
                    lambda: bs.disconnect_client(online_client_id),
                    "disconnect banned player",
                )

            # -------- GET NAME -------- #
            name = "Unknown"
            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)

                if "stats" in stats_data:
                    stats_data = stats_data["stats"]

                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except:
                pass

            server_name = get_config("party_name", "SERVER")

            await ctx.send(f">>> Successfully banned {name} ({pbid}) in {server_name}")
            return

        # ================= UNBAN PLAYER ================= #
        elif subcommand == "unban":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} unban <pbid>`")
                return

            pbid = arg.strip()

            try:
                removed = pdata.unban_player(pbid)
            except Exception as e:
                await ctx.send(f"Unban failed: {_truncate(e, 250)}")
                return
            if not removed:
                await ctx.send("No saved ban was removed. Check the PBID and whether this player is banned.")
                return

            # -------- GET NAME -------- #
            name = "Unknown"
            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)

                if "stats" in stats_data:
                    stats_data = stats_data["stats"]

                if pbid in stats_data:
                    name = clean(stats_data[pbid].get("name", "Unknown"))
            except:
                pass

            server_name = get_config("party_name", "SERVER")

            await ctx.send(f">>> Successfully unbanned {name} ({pbid}) in {server_name}")
            return

# ================= ONLINE PLAYERS ================= #
        elif subcommand == "players":

            server_name = get_config("party_name", "SERVER")
            roster = _stats_snapshot().get("roster", {})

            if not roster:
                await ctx.send("No players online ?")
                return

            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
            except:
                stats_data = {}

            lines = []
            for pbid, info in roster.items():
                name = clean(info.get("name", "Unknown"))
                cid = info.get("client_id", "-")
                rank = stats_data.get(pbid, {}).get("rank", "-")

                lines.append(
                    f"{name}\n"
                    f"Pb - {pbid}\n"
                    f"CID - {cid}\n"
                    f"Rank - {rank}"
                )

            embed = discord.Embed(
                title=server_name,
                description="\n\n".join(lines),
                color=discord.Color.green()
            )

            await ctx.send(embed=embed)
            return


        # ================= RECENT PLAYERS ================= #
        elif subcommand == "recents":

            server_name = get_config("party_name", "SERVER")

            lines = []
            try:
                try:
                    from serverdata import serverdata
                    recent_records = list(getattr(serverdata, "recents", []))
                except (ImportError, AttributeError):
                    recent_data = _load_json(MODS_DIR / "playersdata" / "recents.json", [])
                    if isinstance(recent_data, dict):
                        recent_records = recent_data.get("recents", list(recent_data.values()))
                    else:
                        recent_records = recent_data if isinstance(recent_data, list) else []

                roster = _stats_snapshot().get("roster", {})
                for player in recent_records[-15:]:
                    if not isinstance(player, dict):
                        continue
                    pbid = str(player.get("pbid", player.get("account_id", "Unknown")))
                    client_id = player.get("client_id", "-")
                    name = clean(player.get("name", "Unknown"))
                    if name == "Unknown" and pbid in roster and isinstance(roster[pbid], dict):
                        name = clean(roster[pbid].get("name", "Unknown"))
                    lines.append(f"{name} | {pbid} | ClientID: {client_id}")
            except Exception as e:
                _LOG.exception("Could not read recent players")
                await ctx.send(f"Error getting recents ?: {_truncate(e, 250)}")
                return

            if not lines:
                lines = ["No recent players"]

            embed = discord.Embed(
                title=server_name,
                description="\n".join(lines),
                color=discord.Color.gold()
            )

            await ctx.send(embed=embed)
            return

# ================= KICK / MUTE / UNMUTE ================= #
        elif subcommand in ("kick", "mute", "unmute"):

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} {subcommand} <pbid/clientid>`")
                return

            target = arg.strip()
            roster = _stats_snapshot().get("roster", {})

            pbid = None
            cid = None
            name = "Unknown"

            # -------- FIND BY PBID OR CID -------- #
            for r_pbid, info in roster.items():
                r_cid = str(info.get("client_id", "-"))

                if target == r_pbid or target == r_cid:
                    pbid = r_pbid
                    cid = info.get("client_id", None)
                    name = clean(info.get("name", "Unknown"))
                    break

            # if only pbid given (offline player)
            if pbid is None and target.startswith("a-"):
                pbid = target

            if pbid is None:
                await ctx.send("Player not found ?")
                return

            server_name = get_config("party_name", "SERVER")

            # ================= KICK ================= #
            if subcommand == "kick":

                if cid is None or cid == "-":
                    await ctx.send("Player is not online, cannot kick ?")
                    return

                try:
                    bs.pushcall(
                        lambda: bs.disconnect_client(int(cid)),
                        from_other_thread=True
                    )
                except Exception as e:
                    await ctx.send(f"Kick failed ?: {e}")
                    return

                await ctx.send(f">>> Kick request queued for {name} ({pbid}) in {server_name}")
                return

            # ================= MUTE ================= #
            if subcommand == "mute":
                try:
                    muted = pdata.mute_player(
                        pbid,
                        reason="Muted via Discord",
                        duration_hours=999 * 24,
                        client_id_at_mute=int(cid) if cid is not None and str(cid).isdigit() else None,
                    )
                except Exception as e:
                    await ctx.send(f"Mute failed: {_truncate(e, 250)}")
                    return
                if not muted:
                    await ctx.send("Mute failed. Check the PBID and ensure the protected owner is not targeted.")
                    return

                await ctx.send(f">>> Successfully muted {name} ({pbid}) for 999 days in {server_name}")
                return

            # ================= UNMUTE ================= #
            if subcommand == "unmute":
                try:
                    unmuted = pdata.unmute_player(pbid)
                except Exception as e:
                    await ctx.send(f"Unmute failed: {_truncate(e, 250)}")
                    return
                if not unmuted:
                    await ctx.send("No saved mute was removed. Check the PBID and whether this player is muted.")
                    return

                await ctx.send(f">>> Successfully unmuted {name} ({pbid}) in {server_name}")
                return

# ================= PLAYLIST ================= #
        elif subcommand == "playlist":

            server_name = get_config("party_name", "SERVER")
            playlist_code = get_config("playlist_code", "-")

            pl = _stats_snapshot().get("playlist", {})
            items = pl.get("items", [])

            if items:
                playlist_text = "\n".join(items[:25])
            else:
                playlist_text = (
                    f"Current Map - {pl.get('current', '-')}\n"
                    f"Next Map - {pl.get('next', '-')}"
                )

            embed = discord.Embed(
                title=server_name,
                description=(
                    f">>> Playlist - {playlist_code}\n\n"
                    f"{playlist_text}"
                ),
                color=discord.Color.gold()
            )

            await ctx.send(embed=embed)
            return

# ================= SET SERVER NAME ================= #
        elif subcommand == "setname":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} setname <new server name>`")
                return

            old_name = get_config("party_name", "SERVER")
            new_name = arg.strip()
            if not new_name:
                await ctx.send("Server name cannot be empty ?")
                return
            if len(new_name) > 64:
                await ctx.send("Server name must be 64 characters or fewer ?")
                return

            persisted = False
            try:
                server_config["party_name"] = new_name
                if CONFIG_JSON_VALID:
                    _write_json_atomic(CONFIG_PATH, server_config)
                    persisted = True
            except Exception as e:
                _LOG.exception("Could not persist server name")
                await ctx.send(f"Could not save server name ?: {_truncate(e, 250)}")
                return

            setter = getattr(bs, "set_public_party_name", None)
            live_requested = False
            if callable(setter):
                live_requested = run_on_bs_thread(lambda: setter(new_name), "set public party name")
            if not persisted and not live_requested:
                await ctx.send(f"Name was not changed: config file was not found and the live API is unavailable. Checked `{CONFIG_PATH}`.")
                return

            status = "live party name update requested" if live_requested else "saved to config"
            if not persisted:
                status += "; not persisted to config.json"
            await ctx.send(f">>> Server name changed from **{_truncate(old_name, 64)}** to **{new_name}** ({status}).")
            return


        # ================= CHANGE PLAYLIST ================= #
        elif subcommand == "changepl":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} changepl <playlist_code>`")
                return

            try:
                playlist_code = int(arg.strip())
            except:
                await ctx.send("Playlist code must be a number ?")
                return

            old_code = get_config("playlist_code", "-")

            try:
                server_config["playlist_code"] = playlist_code

                if not CONFIG_JSON_VALID:
                    raise FileNotFoundError(f"a valid config.json was not found at {CONFIG_PATH}")
                _write_json_atomic(CONFIG_PATH, server_config)

            except Exception as e:
                await ctx.send(f"Failed to change playlist ?: {e}")
                return

            server_name = get_config("party_name", "SERVER")
            await ctx.send(f">>> Saved playlist code for **{server_name}**: **{old_code} ? {playlist_code}**. This updates config.json; whether it applies immediately depends on the server launcher/config consumer.")
            return

# ================= FIND PLAYER ================= #
        elif subcommand == "findplayer":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} findplayer <name>`")
                return

            query = arg.lower()

            try:
                with open(STATS_JSON_PATH) as f:
                    data = json.load(f)
                if "stats" in data:
                    data = data["stats"]
            except:
                await ctx.send("Stats file not found ?")
                return

            results = []
            for pbid, p in data.items():
                name = clean(p.get("name", "Unknown"))
                if query in name.lower():
                    results.append((pbid, p))

            if not results:
                await ctx.send("Player not found ?")
                return

            pbid, p = results[0]

            name = clean(p.get("name", "Unknown"))
            rank = p.get("rank", "-")
            score = p.get("scores", 0)
            kills = p.get("kills", 0)
            deaths = p.get("deaths", 0)
            kd = p.get("kd", 0)

            coins = get_coin_balance(pbid)

            server_name = get_config("party_name", "SERVER")

            embed = discord.Embed(
                title=server_name,
                description=(
                    f"Name - {name}\n"
                    f"Pb - {pbid}\n"
                    f"Rank - {rank}\n"
                    f"Score - {score}\n"
                    f"Kills - {kills}\n"
                    f"Death - {deaths}\n"
                    f"Kd - {kd}\n"
                    f"Coins - {coins}"
                ),
                color=discord.Color.gold()
            )

            await ctx.send(embed=embed)
            return


        # ================= MESSAGE / BROADCAST ================= #
        elif subcommand == "msg":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} msg <message>`")
                return

            message = arg.strip()
            server_name = get_config("party_name", "SERVER")

            if not run_on_bs_thread(lambda: bs.chatmessage(message), "Discord broadcast"):
                await ctx.send("Failed to queue game message ?")
                return

            await ctx.send(f">>> Message queued for **{server_name}**: {_truncate(message, 1200)}")
            return

# ================= ADD / REMOVE DISCORD USER ================= #
        elif subcommand in ("adduser", "removeuser"):

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} {subcommand} <@user/userid>`")
                return

            # get discord user id from mention or raw id
            raw = arg.strip().replace("<@", "").replace("!", "").replace(">", "")
            user_id = _as_int(raw)
            if user_id is None:
                await ctx.send("Invalid user id ?")
                return

            server_name = get_config("party_name", "SERVER")

            if "adminUsers" not in dc:
                dc["adminUsers"] = []

            # get user name
            user_name = str(user_id)
            try:
                user = await client.fetch_user(user_id)
                user_name = user.name
            except:
                pass

            # ================= ADD USER ================= #
            if subcommand == "adduser":

                if user_id in dc["adminUsers"]:
                    await ctx.send("User is already an admin.")
                    return

                if not SETTINGS_FILE_VALID:
                    await ctx.send(f"Cannot modify admins: setting.json is missing or invalid at `{SETTINGS_PATH}`.")
                    return
                dc["adminUsers"].append(user_id)
                settings["discordbot"] = dc
                _write_json_atomic(SETTINGS_PATH, settings)

                await ctx.send(f">>> Successfully added **{user_name}** as admin in {server_name}")
                return

            # ================= REMOVE USER ================= #
            if subcommand == "removeuser":

                if user_id not in dc["adminUsers"]:
                    await ctx.send("User is not an admin.")
                    return

                if not SETTINGS_FILE_VALID:
                    await ctx.send(f"Cannot modify admins: setting.json is missing or invalid at `{SETTINGS_PATH}`.")
                    return
                dc["adminUsers"].remove(user_id)
                settings["discordbot"] = dc
                _write_json_atomic(SETTINGS_PATH, settings)

                await ctx.send(f">>> Successfully removed **{user_name}** from admin in {server_name}")
                return

# ================= ROLE LIST ================= #
        elif subcommand == "rolelist":

            try:
                roles = pdata.get_roles()
            except Exception as e:
                await ctx.send(f"Error loading roles ?: {e}")
                return

            if not roles:
                await ctx.send("No roles found ?")
                return

            server_name = get_config("party_name", "SERVER")

            lines = []
            for role_name, role_info in roles.items():
                ids = role_info.get("ids", [])
                commands = role_info.get("commands", [])

                lines.append(
                    f"**{role_name.upper()}**\n"
                    f"Players - {len(ids)} | Cmds - {len(commands)}"
                )

            embed = discord.Embed(
                title=f"{server_name} Role List",
                description="\n\n".join(lines),
                color=discord.Color.gold()
            )

            await ctx.send(embed=embed)
            return

# ================= ROLE INFO ================= #
        elif subcommand == "roleinfo":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} roleinfo <rolename>`")
                return

            input_role = arg.strip()
            roles = pdata.get_roles()

            role_name = None
            for r in roles:
                if r.casefold() == input_role.casefold():
                    role_name = r
                    break

            if role_name is None:
                await ctx.send("Role not found.")
                return

            role = roles[role_name]
            players_count = len(role.get("ids", []))
            commands = role.get("commands", [])

            cmd_text = ", ".join(commands) if commands else "No commands"

            embed = discord.Embed(
                title=f"{role_name.upper()} INFO",
                description=(
                    f"Players - {players_count}\n"
                    f"Commands - {cmd_text}"
                ),
                color=discord.Color.blue()
            )

            await ctx.send(embed=embed)
            return


        # ================= WHO IS ROLE ================= #
        elif subcommand == "whoisrole":

            if not arg:
                await ctx.send(f"Use: `{BOT_PREFIX}{CMD_PREFIX} whoisrole <rolename>`")
                return

            input_role = arg.strip()
            roles = pdata.get_roles()

            role_name = None
            for r in roles:
                if r.casefold() == input_role.casefold():
                    role_name = r
                    break

            if role_name is None:
                await ctx.send("Role not found.")
                return

            ids = roles[role_name].get("ids", [])

            if not ids:
                await ctx.send("No players in this role ?")
                return

            try:
                with open(STATS_JSON_PATH) as f:
                    stats_data = json.load(f)
                if "stats" in stats_data:
                    stats_data = stats_data["stats"]
            except:
                stats_data = {}

            lines = []
            for pbid in ids:
                name = stats_data.get(pbid, {}).get("name", "Unknown")
                name = clean(name)
                lines.append(f"{name} ({pbid})")

            embed = discord.Embed(
                title=f"{role_name.upper()} PLAYERS",
                description="\n".join(lines),
                color=discord.Color.green()
            )

            await ctx.send(embed=embed)
            return

# ================= LAST CHAT ================= #
        elif subcommand == "lastchat":

            server_name = get_config("party_name", "SERVER")

            with chat_lock:
                chats = list(chat_buffer)

            if not chats:
                await ctx.send("No chat found ?")
                return

            per_page = 10
            pages = []

            for i in range(0, len(chats), per_page):
                chunk = chats[i:i + per_page]

                desc = ""
                for idx, msg in enumerate(chunk, start=i + 1):
                    desc += f"Chat {idx} - {msg}\n"

                page_num = (i // per_page) + 1
                total_pages = (len(chats) + per_page - 1) // per_page

                embed = discord.Embed(
                    title=f"{server_name} | Chat Page {page_num}/{total_pages}",
                    description=desc,
                    color=discord.Color.blurple()
                )

                pages.append(embed)

            view = ChatHistoryView(pages)
            await ctx.send(embed=pages[0], view=view)
            return


        # ================= END GAME ================= #
        elif subcommand == "endgame":

            server_name = get_config("party_name", "SERVER")

            pl = _stats_snapshot().get("playlist", {})
            current_map = pl.get("current", "-")
            next_map = pl.get("next", "-")

            def _end_current_game() -> None:
                activity = bs.get_foreground_host_activity()
                if activity is None or not callable(getattr(activity, "end_game", None)):
                    raise RuntimeError("There is no active game to end")
                activity.end_game()

            if not run_on_bs_thread(_end_current_game, "end current game"):
                await ctx.send("Could not schedule end-game request ?")
                return

            embed = discord.Embed(
                title=server_name,
                description=(
                    f"End request queued for - {current_map}\n"
                    f"Next map - {next_map}"
                ),
                color=discord.Color.red()
            )

            await ctx.send(embed=embed)
            return

# ================= CREATOR ================= # Dont change
        elif subcommand == "creator":

            DISCORD_LINK = "https://discord.gg/UN42ck7FCY"
            SCRIPT_LINK = "https://github.com/paradise0403/AS-PARADISE.git"
            WEBSITE_LINK = "http://65.1.65.75:3000/"

            THUMBNAIL_URL = "https://cdn.discordapp.com/attachments/1285299569474928754/1499608486193926247/Mui.jpg?ex=69f56abc&is=69f4193c&hm=3053093f1af3f3e85bad18baf0d8249e0228a650b3bf976cdcc9493abdcf90a2&"
            BANNER_URL = "https://cdn.discordapp.com/attachments/1285299569474928754/1499608553579614278/Wallpaper_programmer.jpg?ex=69f56acc&is=69f4194c&hm=4e6faecd9a0499a6c6d18c3cd746dd8b04562d698a92381c318e5a176eac420c&"

            class CreatorView(View):
                def __init__(self):
                    super().__init__(timeout=None)

                    self.add_item(discord.ui.Button(
                        label="Discord Server",
                        style=discord.ButtonStyle.link,
                        url=DISCORD_LINK
                    ))

                    self.add_item(discord.ui.Button(
                        label="Script",
                        style=discord.ButtonStyle.link,
                        url=SCRIPT_LINK
                    ))

                    self.add_item(discord.ui.Button(
                        label="Website",
                        style=discord.ButtonStyle.link,
                        url=WEBSITE_LINK
                    ))

            embed = discord.Embed(
                title="AS-PARADISE Creator",
                description=(
                    "**Creator** - paradise0403 (ASHX)\n"
                    "**Language** - Python\n"
                    "**Created on** - April 2026\n\n"
                    "Thanks for using my **AS-PARADISE** script.\n"
                    "If you got any error, join our support Discord server.\n\n"
                    f"**Discord Link** - {DISCORD_LINK}\n"
                    f"**Script Link** - {SCRIPT_LINK}\n"
                    f"**Website Link** - {WEBSITE_LINK}"
                ),
                color=discord.Color.gold()
            )

            embed.set_thumbnail(url=THUMBNAIL_URL)
            embed.set_image(url=BANNER_URL)
            embed.set_footer(text="AS-PARADISE • Made by paradise0403")

            await ctx.send(embed=embed, view=CreatorView())
            return

# ================= HELP ================= #
        elif subcommand == "help":

            p = f"{BOT_PREFIX}{CMD_PREFIX}"

            embed = discord.Embed(
                title="**AS PARADISE HELP PANEL**",
                description=(
                    f"**{p} info**\n"
                    f"• Shows server information\n\n"

                    f"**{p} stats <pbid>**\n"
                    f"• Shows player stats\n\n"

                    f"**{p} toplist**\n"
                    f"• Shows top leaderboard\n\n"

                    f"**{p} getroles <pbid>**\n"
                    f"• Shows player roles\n\n"

                    f"**{p} addrole <pbid> <role>**\n"
                    f"• Adds role to player pbid\n\n"

                    f"**{p} removerole <pbid> <role>**\n"
                    f"• Removes role from player pbid\n\n"

                    f"**{p} rolelist**\n"
                    f"• Shows all server roles\n\n"

                    f"**{p} roleinfo <role>**\n"
                    f"• Shows role information\n\n"

                    f"**{p} whoisrole <role>**\n"
                    f"• Shows players in a role\n\n"

                    f"**{p} addtag <pbid> <tag>**\n"
                    f"• Adds a text-only custom tag to a V2 account\n\n"

                    f"**{p} removetag <pbid>**\n"
                    f"• Removes custom tag from player\n\n"

                    f"**{p} effectlist**\n"
                    f"• Shows available effects\n\n"

                    f"**{p} addeffect <pbid> <effect>**\n"
                    f"• Adds effect to player\n\n"

                    f"**{p} removeeffect <pbid> <effect>**\n"
                    f"• Removes effect from player\n\n"

                    f"**{p} addcoin <pbid> <amount>**\n"
                    f"• Adds coins to player\n\n"

                    f"**{p} removecoin <pbid> <amount>**\n"
                    f"• Removes coins from player\n\n"

                    f"**{p} ban <pbid>**\n"
                    f"• Bans player from server\n\n"

                    f"**{p} unban <pbid>**\n"
                    f"• Unbans player from server\n\n"

                    f"**{p} kick <pbid/clientid>**\n"
                    f"• Kicks online player\n\n"

                    f"**{p} mute <pbid/clientid>**\n"
                    f"• Mutes player\n\n"

                    f"**{p} unmute <pbid/clientid>**\n"
                    f"• Unmutes player\n\n"

                    f"**{p} players**\n"
                    f"• Shows online players\n\n"

                    f"**{p} recents**\n"
                    f"• Shows recent joined players\n\n"

                    f"**{p} playlist**\n"
                    f"• Shows current playlist\n\n"

                    f"**{p} setname <name>**\n"
                    f"• Changes server name\n\n"

                    f"**{p} changepl <playlistcode>**\n"
                    f"• Changes playlist code\n\n"

                    f"**{p} findplayer <name>**\n"
                    f"• Finds player by name\n\n"

                    f"**{p} msg <message>**\n"
                    f"• Sends message in game\n\n"

                    f"**{p} lastchat**\n"
                    f"• Shows recent game chats\n\n"

                    f"**{p} endgame**\n"
                    f"• Ends current game\n\n"

                    f"**{p} creator**\n"
                    f"• Shows creator information\n\n"

                    f"**{p} quit**\n"
                    f"• Requests a graceful server shutdown; the manager decides whether to restart\n\n"

                    f"**{p} adduser <@user/id>**\n"
                    f"• Adds Discord admin user\n\n"

                    f"**{p} removeuser <@user/id>**\n"
                    f"• Removes Discord admin user"
                ),
                color=discord.Color.gold()
            )

            embed.set_footer(text="AS PARADISE • Help Panel")
            await ctx.send(embed=embed)
            return

        # ================= INVALID ================= #
        else:
            await ctx.send("Unknown subcommand ?")
            return

    except Exception as e:
        await ctx.send(f"Error: {e}")


# ---------------- BS THREAD ----------------
class BsDataThread:
    """Poll BombSquad state from the game logic thread."""
    def __init__(self):
        # Track join/leave transitions by client ID, not account ID. On API 9,
        # account IDs can appear late or change from a client-ID fallback while
        # a single client is authenticating; tracking by account ID logs a fake
        # second JOIN/LEFT for the same person.
        self.old_roster: dict[str, Any] = {}
        self._last_msg_raw: str | None = None
        self.timer = bs.AppTimer(2.0, babase.CallStrict(self.update), repeat=True)

    @staticmethod
    def _roster_text(value: Any) -> str:
        """Normalize a roster display/name field into a useful string."""
        if isinstance(value, (list, tuple)):
            value = " ".join(str(part) for part in value if part is not None)
        if not isinstance(value, str):
            return ""
        value = " ".join(value.replace("\r", " ").replace("\n", " ").split())
        return clean(value).strip()

    @classmethod
    def _player_name(cls, entry: dict[str, Any]) -> str:
        """Read the best available name from API 9 roster entry layouts."""
        for field in ("name_full", "name", "display_string", "spec_string"):
            name = cls._roster_text(entry.get(field))
            if name and name.casefold() != "unknown":
                return name[:80]

        details = entry.get("players", [])
        if isinstance(details, (list, tuple)):
            for detail in details:
                if not isinstance(detail, dict):
                    continue
                for field in ("name_full", "name", "name_disambig", "display_string"):
                    name = cls._roster_text(detail.get(field))
                    if name and name.casefold() != "unknown":
                        return name[:80]
        return "Unknown"

    @staticmethod
    def _is_generated_server_name(name: str) -> bool:
        """Identify Ballistica's generated Server<digits> placeholder name."""
        if not isinstance(name, str):
            return False
        cleaned = name.strip()
        suffix = cleaned[6:] if cleaned[:6].casefold() == "server" else ""
        return bool(suffix) and suffix.isdigit()

    def update(self) -> None:
        roster: dict[str, dict[str, Any]] = {}
        event_roster: dict[str, dict[str, Any]] = {}
        try:
            raw_roster = bs.get_game_roster()
        except Exception:
            _LOG.exception("Could not read BombSquad game roster")
            raw_roster = []

        # First normalize and deduplicate by stable client ID. Multiple roster
        # representations of one connection must produce just one log event.
        grouped: dict[str, dict[str, Any]] = {}
        for player in raw_roster:
            try:
                if not isinstance(player, dict):
                    continue

                raw_client_id = player.get("client_id")
                try:
                    client_id = int(raw_client_id) if raw_client_id is not None else None
                except (TypeError, ValueError, OverflowError):
                    client_id = None

                # Only real, non-negative client IDs represent connected players.
                # Account-only roster rows can be server metadata/placeholder entries;
                # accepting them produced false JOIN/LEFT events such as Server26900851.
                if client_id is None or client_id < 0:
                    continue

                player_name = self._player_name(player)
                # BombSquad may expose its generated Server<digits> identity as a
                # roster row. It is not a human joining the game, so omit it from
                # both event tracking and the displayed connected-player roster.
                if self._is_generated_server_name(player_name):
                    continue

                raw_account_id = player.get("account_id")
                account_id = str(raw_account_id).strip() if raw_account_id not in (None, "") else ""
                if not account_id:
                    account_id = str(client_id)

                stable_key = f"client:{client_id}"
                candidate = {
                    "account_id": account_id,
                    "name": player_name,
                    "client_id": client_id if client_id is not None else player.get("client_id", "-"),
                }
                previous = grouped.get(stable_key)
                if previous is None:
                    grouped[stable_key] = candidate
                else:
                    # Prefer a real name and a real account ID if another entry
                    # for the same client has more complete data.
                    if previous["name"] == "Unknown" and player_name != "Unknown":
                        previous["name"] = player_name
                    if previous["account_id"].isdigit() and not account_id.isdigit():
                        previous["account_id"] = account_id
                    if previous["client_id"] == "-" and candidate["client_id"] != "-":
                        previous["client_id"] = candidate["client_id"]
            except Exception:
                _LOG.debug("Skipped malformed roster entry: %r", player, exc_info=True)

        for stable_key, info in grouped.items():
            name = info["name"]
            # If the roster temporarily loses a display name, retain the last
            # known name for this client so LEFT events do not become Unknown.
            old_info = self.old_roster.get(stable_key)
            if name == "Unknown" and isinstance(old_info, dict):
                old_name = old_info.get("name")
                if isinstance(old_name, str) and old_name != "Unknown":
                    name = old_name

            event_roster[stable_key] = {
                "name": name,
                "client_id": info["client_id"],
            }

            account_id = str(info["account_id"])
            # Avoid clobbering a different client if malformed roster data gives
            # two connections the same account key in a single poll.
            if account_id in roster:
                account_id = f"{account_id}#client-{info['client_id']}"
            roster[account_id] = {
                "name": name,
                "client_id": info["client_id"],
            }

        new_ids, old_ids = set(event_roster.keys()), set(self.old_roster.keys())
        for stable_key in new_ids - old_ids:
            name = event_roster[stable_key]["name"]
            # Do not emit placeholder events while Ballistica is still filling
            # in the account/name fields. If the name appears on the next poll,
            # the transition is logged below under the same client ID.
            if name != "Unknown":
                push_log(f"JOIN: {name}")
        for stable_key in old_ids - new_ids:
            name = self.old_roster[stable_key].get("name", "Unknown")
            if isinstance(name, str) and name != "Unknown":
                push_log(f"LEFT: {name}")
        for stable_key in new_ids & old_ids:
            old_name = self.old_roster[stable_key].get("name", "Unknown")
            new_name = event_roster[stable_key].get("name", "Unknown")
            if old_name == "Unknown" and new_name != "Unknown":
                # The same client became identifiable after the initial poll.
                push_log(f"JOIN: {new_name}")
        self.old_roster = event_roster

        try:
            messages = bs.get_chat_messages()
            if messages:
                # The newest entry is sufficient for a short live-chat feed.
                current_last = str(messages[-1])
                if current_last != self._last_msg_raw:
                    add_chat(current_last)
                    self._last_msg_raw = current_last
        except Exception:
            _LOG.debug("Could not poll BombSquad chat", exc_info=True)

        current_map = "-"
        next_map = "-"
        playlist_items: list[str] = []
        try:
            session = bs.get_foreground_host_session()
            if session is not None:
                try:
                    description = session.get_next_game_description()
                    next_map = description.evaluate() if hasattr(description, "evaluate") else str(description)
                except Exception:
                    _LOG.debug("Could not read next game description", exc_info=True)
                try:
                    # These fields are implementation details; keep this best-effort
                    # fallback isolated so a minor Ballistica internal change is harmless.
                    spec = getattr(session, "_current_game_spec", None)
                    if isinstance(spec, dict):
                        resolved_type = spec.get("resolved_type")
                        display_fn = getattr(resolved_type, "get_settings_display_string", None)
                        if callable(display_fn):
                            display = display_fn(spec)
                            current_map = display.evaluate() if hasattr(display, "evaluate") else str(display)
                except Exception:
                    _LOG.debug("Could not read current game specification", exc_info=True)
                try:
                    playlist = getattr(session, "_playlist", [])
                    for index, spec in enumerate(playlist, 1):
                        try:
                            resolved_type = spec.get("resolved_type") if isinstance(spec, dict) else None
                            display_fn = getattr(resolved_type, "get_settings_display_string", None)
                            if callable(display_fn):
                                display = display_fn(spec)
                                game_name = display.evaluate() if hasattr(display, "evaluate") else str(display)
                            else:
                                game_name = str(spec)
                        except Exception:
                            game_name = str(spec)
                        playlist_items.append(f"{index}. {_truncate(game_name, 160)}")
                except Exception:
                    _LOG.debug("Could not read playlist items", exc_info=True)

        except Exception:
            _LOG.debug("Could not read current host session", exc_info=True)

        with stats_lock:
            stats["roster"] = roster
            stats["playlist"] = {
                "current": _truncate(current_map, 500),
                "next": _truncate(next_map, 500),
                "items": playlist_items,
            }

        update_web(len(roster), current_map, next_map)


_client_loop: asyncio.AbstractEventLoop | None = None
_client_thread: Thread | None = None


def _discord_worker() -> None:
    global _client_loop
    loop = asyncio.new_event_loop()
    _client_loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(client.start(TOKEN))
    except asyncio.CancelledError:
        pass
    except Exception:
        _LOG.exception("Discord bot stopped with an error")
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            _LOG.debug("Discord event-loop cleanup encountered an error", exc_info=True)
        finally:
            loop.close()
            _client_loop = None


_plugin_lifecycle_lock = threading.Lock()
_plugin_lifecycle_started = False
_custom_hooks_plugin_instance = None


def init() -> None:
    global _client_thread
    if not ENABLE:
        _LOG.info("Discord bot is disabled in %s", SETTINGS_PATH)
        return
    if not TOKEN:
        _LOG.error("Discord bot is enabled but no token is configured in %s", SETTINGS_PATH)
        return
    if _client_thread is not None and _client_thread.is_alive():
        return
    # Warm the IP cache asynchronously; the getter itself never blocks.
    get_public_ip()
    _client_thread = Thread(target=_discord_worker, name="BombSquadDiscordBot", daemon=True)
    _client_thread.start()


class DiscordBotPlugin(babase.Plugin):
    def on_app_running(self):
        global _plugin_lifecycle_started
        with _plugin_lifecycle_lock:
            if _plugin_lifecycle_started:
                return
            _plugin_lifecycle_started = True
        try:
            if ENABLE or WEB_ENABLED:
                self.data_handler = BsDataThread()
            init()
        except Exception:
            _LOG.exception("Discord bot plugin startup failed")
            with _plugin_lifecycle_lock:
                _plugin_lifecycle_started = False
            raise

    def on_app_shutdown(self):
        shutdown_from_custom_hooks()


def start_from_custom_hooks() -> bool:
    """Idempotent entry point for mods/custom_hooks.py."""
    global _custom_hooks_plugin_instance
    if not (ENABLE or WEB_ENABLED):
        _LOG.info("Discord integration is disabled in setting.json")
        return False
    if _custom_hooks_plugin_instance is not None:
        return True
    instance = DiscordBotPlugin()
    # Store it before startup so its timer/plugin state remain strongly referenced.
    _custom_hooks_plugin_instance = instance
    try:
        instance.on_app_running()
    except Exception:
        _custom_hooks_plugin_instance = None
        raise
    _LOG.info("Discord bot integration started from custom_hooks.py")
    return True


def shutdown_from_custom_hooks() -> None:
    """Request an orderly Discord-client shutdown from the plugin loop."""
    loop = _client_loop
    if loop is not None and loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(client.close(), loop)
        except Exception:
            _LOG.debug("Could not request Discord client shutdown", exc_info=True)
