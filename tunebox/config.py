"""Loads and validates tunebox.toml; a bad file stops startup with the key named."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from tunebox.tracks import Rules

# Source types and the settings each one requires.
SOURCE_SETTINGS: dict[str, tuple[str, ...]] = {"triton": ("mount",)}
DEFAULTS: dict[str, float] = {
    "delay_s": 7,
    "pawtunes_delay_s": 28,
    "poll_interval_s": 12,
    "history_size": 50,
}
_CHANNEL_KEYS = {"id", "name", "stream_url", "default_image", "source", "rules", *DEFAULTS}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class SourceConfig:
    type: str
    settings: dict[str, str]


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    name: str
    stream_url: str
    default_image: str | None
    source: SourceConfig
    rules: Rules
    delay_s: float
    pawtunes_delay_s: float
    poll_interval_s: float
    history_size: int


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    data_dir: Path


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    channels: tuple[ChannelConfig, ...]


def load(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigError(f"{path}: {error.strerror}") from error
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: {error}") from error
    return parse(data, base_dir=path.parent)


def parse(data: dict[str, Any], base_dir: Path) -> Config:
    _no_unknown(data, {"server", "defaults", "channels"}, "")
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
    return Config(server, tuple(channels))


def _channel(table: dict[str, Any], where: str, defaults: dict[str, float]) -> ChannelConfig:
    _no_unknown(table, _CHANNEL_KEYS, where)
    channel_id = _str(table, "id", f"{where}.id")
    if not _ID.match(channel_id):
        raise ConfigError(f"{where}.id: lower-case letters, digits and '-' only")
    stream_url = _str(table, "stream_url", f"{where}.stream_url")
    if not stream_url.startswith(("https://", "http://")):
        raise ConfigError(f"{where}.stream_url: must be an http(s) URL")
    numbers = {**defaults, **_numbers(table, where)}
    return ChannelConfig(
        id=channel_id,
        name=_str(table, "name", f"{where}.name"),
        stream_url=stream_url,
        default_image=_optional_str(table, "default_image", f"{where}.default_image"),
        source=_source(_table(table, "source", f"{where}.source"), f"{where}.source"),
        rules=_rules(_table(table, "rules", f"{where}.rules"), f"{where}.rules"),
        delay_s=numbers["delay_s"],
        pawtunes_delay_s=numbers["pawtunes_delay_s"],
        poll_interval_s=numbers["poll_interval_s"],
        history_size=int(numbers["history_size"]),
    )


def _source(table: dict[str, Any], where: str) -> SourceConfig:
    source_type = _str(table, "type", f"{where}.type")
    required = SOURCE_SETTINGS.get(source_type)
    if required is None:
        known = ", ".join(sorted(SOURCE_SETTINGS))
        raise ConfigError(f"{where}.type: unknown source type {source_type!r} (known: {known})")
    _no_unknown(table, {"type", *required}, where)
    return SourceConfig(source_type, {key: _str(table, key, f"{where}.{key}") for key in required})


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
