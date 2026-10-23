# ba_meta require api 9
"""Lobby profile and character chooser for BombSquad 1.8.0 / API 9.

Port and server integration maintained by Lucifer. This keeps the native
API 9 chooser initializer (including V2/cloud-profile state) and adds a
second stage for choosing a character available on the server.
"""

from __future__ import annotations

import logging
from typing import Any

import babase
import bascenev1 as bs
import bauiv1 as bui
from bascenev1 import _lobby
from bascenev1._lobby import ChangeMessage, PlayerReadyMessage

_LOG = logging.getLogger("Lucifer.CharacterChooser")

# Capture the unmodified API 9 initializer before installing any hooks. It
# initializes newer API 9 profile fields such as cloud-profile state.
_ORIGINAL_CHOOSER_INIT = _lobby.Chooser.__init__
_ORIGINAL_CHOOSER_UPDATE_TEXT = _lobby.Chooser._update_text
_ORIGINAL_CHOOSER_UPDATE_POSITION = _lobby.Chooser.update_position

# These legacy appearances are intentionally excluded from this selector,
# matching the supplied plugin's character list.
_EXCLUDED_CHARACTERS = {
    "Lee",
    "Todd McBurton",
    "Zola",
    "Butch",
    "Witch",
    "warrior",
    "Middle-Man",
    "Alien",
    "OldLady",
    "Gladiator",
    "Wrestler",
    "Gretel",
    "Robot",
}



def _character_label_position(self: Any) -> tuple[float, float]:
    """Return the selected-character label position for this lobby team slot."""
    try:
        team_count = len(self.lobby.sessionteams)
        team_index = int(self._selected_team_index)
        spacing = 350.0
        offset = (
            spacing * -0.5 * team_count
            + spacing * team_index
            + 250.0
        )
        if team_count > 1:
            offset -= 35.0
        # API 9 positions the profile-name box at x=-10+offset with a
        # 180-unit width, so its left edge (and the start of its text) is
        # x=-100+offset. Align this label with that left edge. Place its
        # baseline one line below the profile-name line.
        return (-100.0 + offset, float(self._vpos) + 1.0)
    except Exception:
        _LOG.debug("Could not calculate the character-name label position.", exc_info=True)
        return (-100.0, float(getattr(self, "_vpos", 0.0)) + 1.0)


def _ensure_character_name_node(self: Any) -> None:
    """Create a separate text node; do not write text attrs to API 9's depiction node."""
    if getattr(self, "_lucifer_character_name_node", None) is not None:
        return
    parent_node = getattr(self, "_text_node", None)
    if parent_node is None:
        return
    try:
        self._lucifer_character_name_node = bs.newnode(
            "text",
            owner=parent_node,
            attrs={
                "position": _character_label_position(self),
                "h_attach": "center",
                "v_attach": "top",
                "h_align": "left",
                "v_align": "center",
                "color": (1.0, 1.0, 1.0, 1.0),
                "scale": 0.7,
                "shadow": 0.5,
                "text": babase.Lstr(value=""),
            },
        )
    except Exception:
        self._lucifer_character_name_node = None
        _LOG.exception("Could not create the selected-character name label.")


def _refresh_character_name_node(self: Any) -> None:
    """Show the currently highlighted appearance name during character selection."""
    label = getattr(self, "_lucifer_character_name_node", None)
    if label is None:
        return
    try:
        if not label.exists():
            return
        label.position = _character_label_position(self)
        names = getattr(self, "_character_names", ())
        if not getattr(self, "characterchooser", False) or not names:
            label.text = babase.Lstr(value="")
            return
        index = int(getattr(self, "_character_index", 0)) % len(names)
        character_name = names[index]
        # Keep the selected character's label white, independent of team or
        # profile colors, and show just its name without a prefix.
        label.text = babase.Lstr(value=character_name)
    except Exception:
        # A display-label failure must not break profile/character selection.
        _LOG.debug("Could not refresh the selected-character name label.", exc_info=True)


def _update_text(self: Any) -> None:
    """Preserve native API 9 name rendering, then update our separate label."""
    _ORIGINAL_CHOOSER_UPDATE_TEXT(self)
    _refresh_character_name_node(self)


def _update_position(self: Any) -> None:
    """Preserve native lobby positioning and move the extra label with its team."""
    _ORIGINAL_CHOOSER_UPDATE_POSITION(self)
    _refresh_character_name_node(self)


def _chooser_init(self: Any, vpos: float, sessionplayer: bs.SessionPlayer,
                  lobby: Any) -> None:
    """Keep the native API 9 initialization and add server appearances."""
    self.characterchooser = False
    _ORIGINAL_CHOOSER_INIT(self, vpos, sessionplayer, lobby)

    # Keep the native profile/cloud-profile data populated by API 9, but allow
    # this server's installed character appearances in the second selection
    # stage as the original plugin intended.
    classic = babase.app.classic
    if classic is not None:
        try:
            for name in classic.spaz_appearances:
                if (name not in self._character_names
                        and name not in _EXCLUDED_CHARACTERS):
                    self._character_names.append(name)
        except Exception:
            _LOG.exception("Could not append server character appearances.")

    # Use a separate white text node for the selected character name. API 9's
    # native _text_node is a depiction display, not a regular text node.
    _ensure_character_name_node(self)
    _refresh_character_name_node(self)

    # API 9 may provide an empty cloud-profile list for a remote client while
    # still exposing its older transmitted profile dictionary. Restore those
    # profiles only when there is no usable cloud-profile list.
    _restore_legacy_remote_profiles(self)
    _refresh_character_name_node(self)


def _restore_legacy_remote_profiles(self: Any) -> None:
    """Use transmitted client profiles if API 9 supplied no cloud profiles."""
    try:
        input_device = self._sessionplayer.inputdevice
        if not input_device.is_remote_client:
            return

        # API 9 versions have used different private names for this map.
        # If a cloud profile set exists, never replace it with legacy profiles.
        cloud_profiles = getattr(self, "_cloud_json_by_name", None)
        if cloud_profiles is None:
            cloud_profiles = getattr(self, "_cloud_by_name", {})
        if isinstance(cloud_profiles, dict) and cloud_profiles:
            return

        current_names = list(getattr(self, "_profilenames", ()))
        has_custom = any(
            name not in ("_random", "_edit", "__account__")
            for name in current_names
        )
        if has_custom:
            return

        source_profiles = input_device.get_player_profiles()
        if not isinstance(source_profiles, dict):
            _LOG.warning(
                "No client profile dictionary available; lobby is limited to "
                "the random profile. The client must send/sync profiles for "
                "them to be selectable on this server."
            )
            return

        classic = babase.app.classic
        if classic is None:
            return
        known_appearances = set(classic.spaz_appearances)
        profiles: dict[str, dict[str, Any]] = {}
        for name, raw_profile in source_profiles.items():
            if (
                not isinstance(name, str)
                or name in ("_random", "_edit", "__account__")
                or not isinstance(raw_profile, dict)
            ):
                continue
            profile = dict(raw_profile)
            character = profile.get("character")
            if not isinstance(character, str) or character not in known_appearances:
                profile["character"] = "Spaz"
            profiles[name] = profile

        if not profiles:
            _LOG.warning(
                "Client connected without selectable custom profiles; "
                "the server can only offer the random profile."
            )
            return

        profiles["_random"] = {}
        self._profiles = profiles
        self._profilenames = sorted(profiles, key=str.casefold)

        # Select an initial real profile when possible, while still honoring
        # the server's normal per-input saved selection behavior.
        self._profileindex = self._select_initial_profile()
        self._profilename = self._profilenames[self._profileindex]
        self.characterchooser = False
        self.update_from_profile()
        _LOG.info(
            "Restored %d transmitted client profiles for %s: %s",
            len(profiles) - 1,
            input_device.name,
            ", ".join(name for name in self._profilenames if name != "_random"),
        )
    except Exception:
        _LOG.exception("Could not restore transmitted client profiles.")


def _clear_cloud_look_for_override(self: Any) -> None:
    """Switch from a complete cloud look to the selected server appearance."""
    # In current API 9, cloud profiles are composed full looks. Clear the
    # active look override before changing _character_index so the native
    # session uses the selected server appearance instead of the cloud SpazDef.
    if hasattr(self, "_cloud_look_name"):
        self._cloud_look_name = None
    if hasattr(self, "_cloud_spaz_def"):
        self._cloud_spaz_def = None
    if hasattr(self, "_cloud_icon"):
        self._cloud_icon = None


def _set_ready(self: Any, ready: bool) -> None:
    """Use first confirmation for character selection; second joins the game."""
    classic = babase.app.classic
    profilename = self._profilenames[self._profileindex]

    # Profile editing is only available for local input devices; keep the
    # native API 9 route for completeness.
    if profilename == "_edit" and ready:
        if classic is not None:
            with babase.ContextRef.empty():
                classic.profile_browser_window()
                babase.set_main_ui_input_device(self._sessionplayer.inputdevice.id)
        return

    if ready is False:
        # Back at the profile selector. Restore the selected profile's own
        # look (including a cloud profile's composed look) before showing it.
        self.characterchooser = False
        try:
            if hasattr(self, "_cloud_look_name"):
                self._cloud_look_name = None
            self.update_from_profile()
        except Exception:
            _LOG.exception("Could not restore the selected profile appearance.")

        self._sessionplayer.assigninput(
            babase.InputType.LEFT_PRESS,
            babase.CallStrict(self.handlemessage, ChangeMessage("team", -1)),
        )
        self._sessionplayer.assigninput(
            babase.InputType.RIGHT_PRESS,
            babase.CallStrict(self.handlemessage, ChangeMessage("team", 1)),
        )
        self._sessionplayer.assigninput(
            babase.InputType.BOMB_PRESS,
            babase.CallStrict(self.handlemessage, ChangeMessage("character", 1)),
        )
        self._sessionplayer.assigninput(
            babase.InputType.UP_PRESS,
            babase.CallStrict(self.handlemessage, ChangeMessage("profileindex", -1)),
        )
        self._sessionplayer.assigninput(
            babase.InputType.DOWN_PRESS,
            babase.CallStrict(self.handlemessage, ChangeMessage("profileindex", 1)),
        )
        self._sessionplayer.assigninput(
            (
                babase.InputType.JUMP_PRESS,
                babase.InputType.PICK_UP_PRESS,
                babase.InputType.PUNCH_PRESS,
            ),
            babase.CallStrict(self.handlemessage, ChangeMessage("ready", 1)),
        )
        self._ready = False
        self._update_text()
        self._sessionplayer.setname("untitled", real=False)
        return

    # The first confirmation selects the profile and opens the character stage.
    self.characterchooser = True
    _clear_cloud_look_for_override(self)
    # A cloud-profile icon is a composed depiction; redraw it as the selected
    # server appearance icon before the user starts cycling characters.
    self._update_icon()

    self._sessionplayer.assigninput(
        (
            babase.InputType.LEFT_PRESS,
            babase.InputType.RIGHT_PRESS,
            babase.InputType.UP_PRESS,
            babase.InputType.DOWN_PRESS,
            babase.InputType.JUMP_PRESS,
            babase.InputType.BOMB_PRESS,
            babase.InputType.PICK_UP_PRESS,
        ),
        self._do_nothing,
    )
    self._sessionplayer.assigninput(
        babase.InputType.UP_PRESS,
        babase.CallStrict(
            self.handlemessage, ChangeMessage("characterchooser", -1)
        ),
    )
    self._sessionplayer.assigninput(
        babase.InputType.DOWN_PRESS,
        babase.CallStrict(
            self.handlemessage, ChangeMessage("characterchooser", 1)
        ),
    )
    self._sessionplayer.assigninput(
        babase.InputType.BOMB_PRESS,
        babase.CallStrict(self.handlemessage, ChangeMessage("ready", 0)),
    )
    self._sessionplayer.assigninput(
        (
            babase.InputType.JUMP_PRESS,
            babase.InputType.PICK_UP_PRESS,
            babase.InputType.PUNCH_PRESS,
        ),
        babase.CallStrict(self.handlemessage, ChangeMessage("ready", 2)),
    )

    # Persist the chosen profile for this input device, matching normal API 9
    # chooser behavior. This changes a visible player profile, not account ID.
    input_device = self._sessionplayer.inputdevice
    profilekey = input_device.name + " " + input_device.unique_identifier
    device_profiles = babase.app.config.setdefault("Default Player Profiles", {})
    special = ("_random", "_edit", "__account__")
    have_custom_profiles = any(profile not in special for profile in self._profiles)
    if profilename == "_random" and not have_custom_profiles:
        device_profiles.pop(profilekey, None)
    else:
        device_profiles[profilekey] = profilename
    babase.app.config.commit()

    self._sessionplayer.setname(
        self._getname(), self._getname(full=True), real=True
    )
    self._ready = True
    self._update_text()


def handlemessage(self: Any, msg: Any) -> Any:
    """Handle lobby profile, team, and server-character selection input."""
    if not isinstance(msg, ChangeMessage):
        return None

    self._handle_repeat_message_attack()
    if self._dead or not self._text_node:
        _LOG.debug("Ignoring chooser input after its lobby nodes were removed.")
        return None

    if msg.what == "characterchooser":
        if self._character_names:
            _clear_cloud_look_for_override(self)
            self._click_sound.play()
            self._character_index = (
                self._character_index + msg.value
            ) % len(self._character_names)
            self._update_text()
            self._update_icon()

    elif msg.what == "team":
        sessionteams = self.lobby.sessionteams
        if len(sessionteams) > 1:
            self._swish_sound.play()
        self._selected_team_index = (
            self._selected_team_index + msg.value
        ) % len(sessionteams)
        self._update_text()
        self.update_position()
        self._update_icon()

    elif msg.what == "profileindex":
        if len(self._profilenames) <= 1:
            self._errorsound.play()
        else:
            self._deek_sound.play()
            self._profileindex = (
                self._profileindex + msg.value
            ) % len(self._profilenames)
            if hasattr(self, "_cloud_look_name"):
                self._cloud_look_name = None
            self.characterchooser = False
            self.update_from_profile()

    elif msg.what == "character":
        if self._character_names:
            _clear_cloud_look_for_override(self)
            self._click_sound.play()
            self.characterchooser = True
            self._character_index = (
                self._character_index + msg.value
            ) % len(self._character_names)
            self._update_text()
            self._update_icon()

    elif msg.what == "ready":
        if msg.value == 2:
            # The second confirmation is the actual join. Current API 9's
            # _handle_ready_msg(bool) cannot represent the legacy stage value 2.
            if not self._ready or not self.characterchooser:
                return None
            try:
                self._punchsound.play()
                session = bs.getsession()
                if session is not None:
                    session.handlemessage(PlayerReadyMessage(self))
            except Exception:
                _LOG.exception("Could not complete lobby join from character chooser.")
        else:
            if msg.value == 0:
                self.characterchooser = False
                if hasattr(self, "_cloud_look_name"):
                    self._cloud_look_name = None
                try:
                    self.update_from_profile()
                except Exception:
                    _LOG.exception("Could not restore profile after cancelling character selection.")
            # Use the native API 9 readiness path for team auto-balance and
            # input-device checks. It calls our patched _set_ready method.
            self._handle_ready_msg(bool(msg.value))

    return None


def install() -> bool:
    """Install the API 9 lobby chooser patch exactly once.

    Native profile-name rendering is preserved. A separately owned text node
    displays the highlighted server character name while the second selection
    stage is active, avoiding unsupported writes to the native depiction node.
    """
    chooser_class = _lobby.Chooser
    if getattr(chooser_class, "_lucifer_character_chooser_installed", False):
        return True

    try:
        chooser_class.__init__ = _chooser_init
        chooser_class._set_ready = _set_ready
        # Wrap (do not replace) native text and positioning behavior.
        chooser_class._update_text = _update_text
        chooser_class.update_position = _update_position
        chooser_class.handlemessage = handlemessage
        chooser_class._lucifer_character_chooser_installed = True
        _LOG.info("Installed API 9 lobby profile/character chooser.")
        return True
    except Exception:
        _LOG.exception("Could not install API 9 lobby profile/character chooser.")
        return False

def enable() -> None:
    """Compatibility alias for manual plugin installation."""
    install()
