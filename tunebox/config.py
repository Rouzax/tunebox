"""Loads and validates tunebox.toml and the stored configuration; errors name the key."""

from __future__ import annotations

import ipaddress
import math
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from tunebox.tracks import Rules

# Source types and the settings each one requires.
SOURCE_SETTINGS: dict[str, tuple[str, ...]] = {
    "triton": ("mount",),
    "qmusic": ("station",),
    "npo": ("site",),
}
# Settings whose value must be one of a fixed set, per source type.
SOURCE_CHOICES: dict[str, dict[str, tuple[str, ...]]] = {
    "npo": {"site": ("nporadio1", "nporadio2", "npo3fm")},
}
CODECS = ("aac", "mp3")
DEFAULTS: dict[str, float] = {
    "delay_s": 7,
    "pawtunes_delay_s": 28,
    "poll_interval_s": 12,
    "history_size": 50,
}
_CHANNEL_KEYS = {
    "id",
    "name",
    "stream_url",
    "streams",
    "default_image",
    "source",
    "rules",
    "group",
    "logo",
    "always",
    "needs_pin",
    *DEFAULTS,
}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# A logo uploaded in the panel, named by the SHA-256 of its bytes.
BLOB_REF = re.compile(r"blob:([0-9a-f]{64})")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class SourceConfig:
    type: str
    settings: dict[str, str]


@dataclass(frozen=True)
class StreamConfig:
    name: str
    url: str
    codec: str | None
    kbps: int | None


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    name: str
    streams: tuple[StreamConfig, ...]
    default_image: str | None
    source: SourceConfig
    rules: Rules
    delay_s: float
    pawtunes_delay_s: float
    poll_interval_s: float
    history_size: int
    group: str | None = None
    logo: str | None = None
    # A pinned channel is polled even while nobody listens.
    always: bool = False
    # Hidden from devices without the PIN while the PIN is in "stations" mode.
    needs_pin: bool = False

    @property
    def stream_url(self) -> str:
        return self.streams[0].url


@dataclass(frozen=True)
class PlayerConfig:
    latitude: float | None = None
    longitude: float | None = None
    default_channel: str | None = None


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    data_dir: Path
    # Only these may say who the client is (X-Forwarded-For).
    trusted_proxies: tuple[Network, ...] = ()
    # A copy of the log lines in a file, for readers without the container's output.
    log_file: Path | None = None


@dataclass(frozen=True)
class AdminConfig:
    enabled: bool = False
    setup_page: bool = False
    # Scheme and host of the public address, as a browser sends it in
    # Origin; every state-changing admin request must come from it.
    public_origin: str | None = None

    @property
    def plain_http(self) -> bool:
        return urllib.parse.urlsplit(self.public_origin or "").scheme == "http"


@dataclass(frozen=True)
class ChannelRow:
    """One station as stored: its [[channels]] table without the group,
    which is kept apart so groups can be ordered and renamed in one place."""

    id: str
    group: str | None
    enabled: bool
    doc: dict[str, Any]


@dataclass(frozen=True)
class ConfigState:
    """Everything the admin panel edits, in the shape it is stored. The
    order of channels is their position; groups are listed in their order."""

    defaults: dict[str, float] = field(default_factory=dict[str, float])
    player: dict[str, Any] = field(default_factory=dict[str, Any])
    groups: tuple[str, ...] = ()
    channels: tuple[ChannelRow, ...] = ()


@dataclass(frozen=True)
class Resolved:
    # Enabled channels in listing order: ungrouped first, as the player shows them.
    channels: tuple[ChannelConfig, ...]
    player: PlayerConfig


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    channels: tuple[ChannelConfig, ...]
    player: PlayerConfig = PlayerConfig()
    state: ConfigState = ConfigState()
    admin: AdminConfig = AdminConfig()


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(data, base_dir=path.parent)


def parse(data: dict[str, Any], base_dir: Path) -> Config:
    _no_unknown(data, {"server", "defaults", "channels", "groups", "player", "admin"}, "")
    server_table = _table(data, "server", "server")
    _no_unknown(server_table, {"host", "port", "data_dir", "trusted_proxies", "log_file"}, "server")
    port = _int(server_table, "port", "server.port", 8080)
    if not 1 <= port <= 65535:
        raise ConfigError("server.port: must be between 1 and 65535")
    data_dir = Path(_str(server_table, "data_dir", "server.data_dir", str(base_dir)))
    log_file = _optional_str(server_table, "log_file", "server.log_file")
    if log_file is not None and not log_file.strip():
        raise ConfigError("server.log_file: must not be empty")
    server = ServerConfig(
        # Inside the container; the compose file publishes the port on loopback only.
        host=_str(server_table, "host", "server.host", "0.0.0.0"),
        port=port,
        data_dir=data_dir,
        trusted_proxies=_networks(server_table),
        # A relative path is inside the data folder.
        log_file=data_dir / log_file if log_file is not None else None,
    )

    state = state_from_toml(data)
    resolved = validate_state(state)
    admin = _admin(_table(data, "admin", "admin"))
    return Config(server, resolved.channels, resolved.player, state, admin)


def _networks(table: dict[str, Any]) -> tuple[Network, ...]:
    raw: object = table.get("trusted_proxies", [])
    if not isinstance(raw, list):
        raise ConfigError("server.trusted_proxies: expected a list of addresses or ranges")
    networks: list[Network] = []
    for index, item in enumerate(cast("list[object]", raw)):
        try:
            networks.append(ipaddress.ip_network(str(item), strict=False))
        except ValueError as error:
            raise ConfigError(f"server.trusted_proxies[{index}]: {error}") from error
    return tuple(networks)


def _admin(table: dict[str, Any]) -> AdminConfig:
    _no_unknown(table, {"enabled", "setup_page", "public_origin"}, "admin")
    enabled = _bool(table, "enabled", "admin.enabled", False)
    origin = _optional_str(table, "public_origin", "admin.public_origin")
    if enabled and origin is None:
        raise ConfigError("admin.public_origin: required when the admin panel is enabled")
    if origin is not None:
        origin = _origin(origin)
    return AdminConfig(enabled, _bool(table, "setup_page", "admin.setup_page", False), origin)


def _origin(value: str) -> str:
    """The origin as a browser sends it: lower case, no default port. Plain
    http is for a home network: the server then serves the panel only to
    clients on it."""
    parts = urllib.parse.urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        port = -1
    host = parts.hostname
    good = parts.scheme in ("https", "http") and port != -1 and not parts.username
    if host is None or not good or parts.path or parts.query or parts.fragment:
        raise ConfigError(
            "admin.public_origin: an origin without a path, such as"
            " https://radio.example or http://tunebox.home.arpa:8083"
        )
    if ":" in host:
        host = f"[{host}]"
    default = {"http": 80, "https": 443}[parts.scheme]
    return f"{parts.scheme}://{host}" + (f":{port}" if port not in (None, default) else "")


_START_UP_ONLY = ("server", "admin")


def state_from_import(text: str) -> tuple[ConfigState, list[str]]:
    """The stations, groups, defaults and player of a tunebox.toml or an
    export. Start-up settings are left out with a note: they belong to the
    file tunebox starts from, not to the stored configuration. The state is
    not validated; the caller does that."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(str(error)) from error
    except RecursionError as error:
        raise ConfigError("the file is too deeply nested") from error
    except ValueError as error:
        # Python refuses to read an integer of more than 4300 digits.
        raise ConfigError("the file holds a number with too many digits") from error
    notes = [
        f"[{key}] is a start-up setting and was left out" for key in _START_UP_ONLY if key in data
    ]
    for key in _START_UP_ONLY:
        data.pop(key, None)
    _no_unknown(data, {"defaults", "channels", "player", "groups"}, "")
    return state_from_toml(data), notes


def state_from_toml(data: dict[str, Any]) -> ConfigState:
    defaults_table = _table(data, "defaults", "defaults")
    _no_unknown(defaults_table, set(DEFAULTS), "defaults")
    raw_channels: object = data.get("channels", [])
    if not isinstance(raw_channels, list):
        raise ConfigError("channels: expected [[channels]] tables")
    listed = _group_names(data.get("groups"))
    groups: list[str] = [] if listed is None else listed
    rows: list[ChannelRow] = []
    for index, raw in enumerate(cast("list[object]", raw_channels)):
        where = f"channels[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: expected a table")
        doc = dict(cast("dict[str, Any]", raw))
        group = _optional_str(doc, "group", f"{where}.group")
        doc.pop("group", None)
        # With a [[groups]] list the order is the file's; a station naming a
        # group outside it is left for validate_state to refuse.
        if listed is None and group is not None and group not in groups:
            groups.append(group)
        enabled = _bool(doc, "enabled", f"{where}.enabled", True)
        doc.pop("enabled", None)
        raw_id: object = doc.get("id", "")
        rows.append(ChannelRow(raw_id if isinstance(raw_id, str) else "", group, enabled, doc))
    _numbers(defaults_table, "defaults")
    # Stored as written: _numbers turns history_size into a float, which a
    # second validation would refuse.
    return ConfigState(
        cast("dict[str, float]", dict(defaults_table)),
        _table(data, "player", "player"),
        tuple(groups),
        tuple(rows),
    )


def _group_names(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ConfigError("groups: expected [[groups]] tables")
    names: list[str] = []
    for index, item in enumerate(cast("list[object]", raw)):
        table = cast("dict[str, Any]", item) if isinstance(item, dict) else {}
        _no_unknown(table, {"name"}, f"groups[{index}]")
        name: object = table.get("name")
        if not isinstance(name, str):
            raise ConfigError(f"groups[{index}].name: expected a string")
        names.append(name)
    return names


def state_to_toml(state: ConfigState) -> dict[str, object]:
    """The export shape: tunebox.toml's own, plus the group order (with
    groups that have no station) and the stations that are off."""
    channels: list[dict[str, object]] = []
    for row in state.channels:
        doc: dict[str, object] = {"id": row.id, **{k: v for k, v in row.doc.items() if k != "id"}}
        if row.group is not None:
            doc["group"] = row.group
        if not row.enabled:
            doc["enabled"] = False
        channels.append(doc)
    out: dict[str, object] = {}
    if state.defaults:
        out["defaults"] = dict(state.defaults)
    if state.player:
        out["player"] = dict(state.player)
    if state.groups:
        out["groups"] = [{"name": name} for name in state.groups]
    out["channels"] = channels
    return out


def validate_state(state: ConfigState) -> Resolved:
    """Checks the whole configuration, disabled channels included, so a
    change that breaks a rule spanning channels is refused as a whole."""
    _text(dict(state.defaults), "defaults")
    _text(state.player, "player")
    _text(list(state.groups), "groups")
    for index, row in enumerate(state.channels):
        _text(row.doc, f"channels[{index}]")
        _text(row.group, f"channels[{index}].group")
    _no_unknown(state.defaults, set(DEFAULTS), "defaults")
    defaults = {**DEFAULTS, **_numbers(dict(state.defaults), "defaults")}
    seen: dict[str, tuple[ChannelRow, ChannelConfig]] = {}
    for index, row in enumerate(state.channels):
        where = f"channels[{index}]"
        table = row.doc if row.group is None else {**row.doc, "group": row.group}
        channel = _channel(table, where, defaults)
        if channel.id in seen:
            raise ConfigError(f"{where}.id: duplicate id {channel.id!r}")
        if channel.id != row.id:
            raise ConfigError(f"{where}.id: {channel.id!r} is stored as {row.id!r}")
        if channel.group is not None and channel.group not in state.groups:
            raise ConfigError(f"{where}.group: no group named {channel.group!r}")
        seen[channel.id] = (row, channel)
    for index, name in enumerate(state.groups):
        if not name.strip() or state.groups.index(name) != index:
            raise ConfigError(f"groups[{index}]: names must be unique and not empty")
    rank = {name: position for position, name in enumerate(state.groups)}
    enabled = [channel for row, channel in seen.values() if row.enabled]
    # sorted() is stable, so channels keep their positions within a group.
    ordered = sorted(enabled, key=lambda c: -1 if c.group is None else rank[c.group])
    return Resolved(tuple(ordered), _player(dict(state.player), {c.id for c in ordered}))


def keeps_a_channel(before: ConfigState, after: ConfigState) -> None:
    """Refuses a state that switches off the last enabled channel of the
    stored one. A stored state with none enabled can be replaced by any."""
    if any(c.enabled for c in before.channels) and not any(c.enabled for c in after.channels):
        raise ConfigError("channels: at least one enabled channel is required")


def _text(value: object, where: str) -> None:
    """Refuses a string that cannot be stored as UTF-8: JSON allows a lone
    surrogate such as \\ud800, SQLite does not."""
    if isinstance(value, str) and not _encodable(value):
        raise ConfigError(f"{where}: not valid text")
    if isinstance(value, dict):
        for key, item in cast("dict[str, object]", value).items():
            if not _encodable(key):
                raise ConfigError(f"{where}: a key that is not valid text")
            _text(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(cast("list[object]", value)):
            _text(item, f"{where}[{index}]")


def _encodable(text: str) -> bool:
    try:
        text.encode()
    except UnicodeEncodeError:
        return False
    return True


def _channel(table: dict[str, Any], where: str, defaults: dict[str, float]) -> ChannelConfig:
    _no_unknown(table, _CHANNEL_KEYS, where)
    channel_id = _str(table, "id", f"{where}.id")
    if not _ID.match(channel_id):
        raise ConfigError(f"{where}.id: lower-case letters, digits and '-' only")
    numbers = {**defaults, **_numbers(table, where)}
    group = _optional_str(table, "group", f"{where}.group")
    if group is not None and not group.strip():
        raise ConfigError(f"{where}.group: must not be empty")
    logo = _optional_str(table, "logo", f"{where}.logo")
    if logo is not None and not (logo.startswith("https://") or BLOB_REF.fullmatch(logo)):
        raise ConfigError(f"{where}.logo: must be an https URL or an uploaded logo")
    return ChannelConfig(
        id=channel_id,
        name=_str(table, "name", f"{where}.name"),
        streams=_streams(table, where),
        default_image=_optional_str(table, "default_image", f"{where}.default_image"),
        source=_source(_table(table, "source", f"{where}.source"), f"{where}.source"),
        rules=_rules(_table(table, "rules", f"{where}.rules"), f"{where}.rules"),
        delay_s=numbers["delay_s"],
        pawtunes_delay_s=numbers["pawtunes_delay_s"],
        poll_interval_s=numbers["poll_interval_s"],
        history_size=int(numbers["history_size"]),
        group=group,
        logo=logo,
        always=_bool(table, "always", f"{where}.always", False),
        needs_pin=_bool(table, "needs_pin", f"{where}.needs_pin", False),
    )


def _streams(table: dict[str, Any], where: str) -> tuple[StreamConfig, ...]:
    raw: object = table.get("streams")
    if "stream_url" in table:
        if raw is not None:
            raise ConfigError(f"{where}: give stream_url or streams, not both")
        url = _str(table, "stream_url", f"{where}.stream_url")
        # Kept as it was before streams existed: http is accepted here.
        if not url.startswith(("https://", "http://")):
            raise ConfigError(f"{where}.stream_url: must be an http(s) URL")
        return (StreamConfig("Default", url, None, None),)
    if raw is None:
        raise ConfigError(f"{where}.streams: required (or stream_url)")
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{where}.streams: expected at least one [[channels.streams]] table")
    streams: list[StreamConfig] = []
    for index, item in enumerate(cast("list[object]", raw)):
        at = f"{where}.streams[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{at}: expected a table")
        entry = cast("dict[str, Any]", item)
        _no_unknown(entry, {"name", "url", "codec", "kbps"}, at)
        name = _str(entry, "name", f"{at}.name")
        if not name.strip():
            raise ConfigError(f"{at}.name: must not be empty")
        if any(s.name == name for s in streams):
            raise ConfigError(f"{at}.name: duplicate name {name!r}")
        url = _str(entry, "url", f"{at}.url")
        # The player page is served over https and browsers block http audio on it.
        if not url.startswith("https://"):
            raise ConfigError(f"{at}.url: must be an https URL")
        codec = _optional_str(entry, "codec", f"{at}.codec")
        if codec is not None and codec not in CODECS:
            raise ConfigError(f"{at}.codec: one of {', '.join(CODECS)}")
        streams.append(StreamConfig(name, url, codec, _optional_int(entry, "kbps", f"{at}.kbps")))
    return tuple(streams)


def _player(table: dict[str, Any], channel_ids: set[str]) -> PlayerConfig:
    _no_unknown(table, {"latitude", "longitude", "default_channel"}, "player")
    latitude = _optional_float(table, "latitude", "player.latitude", 90)
    longitude = _optional_float(table, "longitude", "player.longitude", 180)
    if (latitude is None) != (longitude is None):
        raise ConfigError("player: give latitude and longitude together")
    default = _optional_str(table, "default_channel", "player.default_channel")
    if default is not None and default not in channel_ids:
        raise ConfigError(f"player.default_channel: no channel with id {default!r}")
    return PlayerConfig(latitude, longitude, default)


def _source(table: dict[str, Any], where: str) -> SourceConfig:
    source_type = _str(table, "type", f"{where}.type")
    required = SOURCE_SETTINGS.get(source_type)
    if required is None:
        known = ", ".join(sorted(SOURCE_SETTINGS))
        raise ConfigError(f"{where}.type: unknown source type {source_type!r} (known: {known})")
    _no_unknown(table, {"type", *required}, where)
    settings = {key: _str(table, key, f"{where}.{key}") for key in required}
    for key, allowed in SOURCE_CHOICES.get(source_type, {}).items():
        if settings[key] not in allowed:
            raise ConfigError(f"{where}.{key}: one of {', '.join(allowed)}")
    return SourceConfig(source_type, settings)


# The rule keys that hold a regular expression; rulecheck guards exactly these.
PATTERN_RULES = ("station_id", "played_by")


def _rules(table: dict[str, Any], where: str) -> Rules:
    _no_unknown(table, {*PATTERN_RULES, "artist_separator"}, where)
    patterns = {key: _pattern(table, key, f"{where}.{key}") for key in PATTERN_RULES}
    played_by = patterns["played_by"]
    if played_by is not None and played_by.groups < 1:
        raise ConfigError(f"{where}.played_by: needs a group for the part to keep")
    separator = _str(table, "artist_separator", f"{where}.artist_separator", ",")
    if not separator:
        raise ConfigError(f"{where}.artist_separator: must not be empty")
    return replace(Rules(artist_separator=separator), **patterns)


def _pattern(table: dict[str, Any], key: str, where: str) -> re.Pattern[str] | None:
    source = _str(table, key, where, "")
    if not source:
        return None
    try:
        return re.compile(source)
    except re.error as error:
        raise ConfigError(f"{where}: {error}") from error


def _numbers(table: dict[str, Any], where: str) -> dict[str, float]:
    found: dict[str, float] = {}
    for key in DEFAULTS:
        if key not in table:
            continue
        value: object = table[key]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{where}.{key}: expected a number")
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"{where}.{key}: expected a finite number")
        if key == "history_size" and (not isinstance(value, int) or value < 1):
            raise ConfigError(f"{where}.{key}: expected a whole number of at least 1")
        if value < 0 or (key == "poll_interval_s" and value == 0):
            raise ConfigError(f"{where}.{key}: out of range")
        found[key] = float(value)
    return found


def _no_unknown(table: dict[str, Any], allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            prefix = f"{where}." if where else ""
            raise ConfigError(f"{prefix}{key}: unknown key")


def _table(table: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value: object = table.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a table")
    return cast("dict[str, Any]", value)


def _str(table: dict[str, Any], key: str, where: str, default: str | None = None) -> str:
    if key not in table:
        if default is None:
            raise ConfigError(f"{where}: required")
        return default
    value: object = table[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where}: expected a string")
    return value


def _optional_str(table: dict[str, Any], key: str, where: str) -> str | None:
    return _str(table, key, where) if key in table else None


def _int(table: dict[str, Any], key: str, where: str, default: int) -> int:
    value: object = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: expected a whole number")
    return value


def _optional_int(table: dict[str, Any], key: str, where: str) -> int | None:
    if key not in table:
        return None
    value: object = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: expected a whole number of at least 1")
    return value


def _optional_float(table: dict[str, Any], key: str, where: str, limit: float) -> float | None:
    if key not in table:
        return None
    value: object = table[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: expected a number")
    # The comparison is also false for nan.
    if not -limit <= value <= limit:
        raise ConfigError(f"{where}: must be between {-limit:g} and {limit:g}")
    return float(value)


def _bool(table: dict[str, Any], key: str, where: str, default: bool) -> bool:
    value: object = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected true or false")
    return value
