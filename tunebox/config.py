"""Loads and validates tunebox.toml; a bad file stops startup with the key named."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
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
    *DEFAULTS,
}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


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

    @property
    def stream_url(self) -> str:
        return self.streams[0].url


@dataclass(frozen=True)
class PlayerConfig:
    latitude: float | None = None
    longitude: float | None = None
    default_channel: str | None = None


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    data_dir: Path


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    channels: tuple[ChannelConfig, ...]
    player: PlayerConfig = PlayerConfig()


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(data, base_dir=path.parent)


def parse(data: dict[str, Any], base_dir: Path) -> Config:
    _no_unknown(data, {"server", "defaults", "channels", "player"}, "")
    server_table = _table(data, "server", "server")
    _no_unknown(server_table, {"host", "port", "data_dir"}, "server")
    port = _int(server_table, "port", "server.port", 8080)
    if not 1 <= port <= 65535:
        raise ConfigError("server.port: must be between 1 and 65535")
    server = ServerConfig(
        # Inside the container; the compose file publishes the port on loopback only.
        host=_str(server_table, "host", "server.host", "0.0.0.0"),
        port=port,
        data_dir=Path(_str(server_table, "data_dir", "server.data_dir", str(base_dir))),
    )

    defaults_table = _table(data, "defaults", "defaults")
    _no_unknown(defaults_table, set(DEFAULTS), "defaults")
    defaults = {**DEFAULTS, **_numbers(defaults_table, "defaults")}

    raw_channels: object = data.get("channels")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ConfigError("channels: required, at least one [[channels]] table")
    channels: list[ChannelConfig] = []
    for index, raw in enumerate(cast("list[object]", raw_channels)):
        where = f"channels[{index}]"
        if not isinstance(raw, dict):
            raise ConfigError(f"{where}: expected a table")
        channel = _channel(cast("dict[str, Any]", raw), where, defaults)
        if any(c.id == channel.id for c in channels):
            raise ConfigError(f"{where}.id: duplicate id {channel.id!r}")
        channels.append(channel)
    player = _player(_table(data, "player", "player"), {c.id for c in channels})
    return Config(server, tuple(channels), player)


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
    if logo is not None and not logo.startswith("https://"):
        raise ConfigError(f"{where}.logo: must be an https URL")
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


def _rules(table: dict[str, Any], where: str) -> Rules:
    _no_unknown(table, {"station_id", "played_by", "artist_separator"}, where)
    station_id = _pattern(table, "station_id", f"{where}.station_id")
    played_by = _pattern(table, "played_by", f"{where}.played_by")
    if played_by is not None and played_by.groups < 1:
        raise ConfigError(f"{where}.played_by: needs a group for the part to keep")
    separator = _str(table, "artist_separator", f"{where}.artist_separator", ",")
    if not separator:
        raise ConfigError(f"{where}.artist_separator: must not be empty")
    return Rules(station_id=station_id, played_by=played_by, artist_separator=separator)


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
