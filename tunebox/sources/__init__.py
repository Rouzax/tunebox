"""Now-playing sources, chosen per channel by the config's source type."""

from __future__ import annotations

from tunebox.config import SourceConfig
from tunebox.http import HttpGet
from tunebox.sources.base import Source
from tunebox.sources.npo import NpoSource
from tunebox.sources.qmusic import QmusicSource
from tunebox.sources.triton import TritonSource


def build_source(config: SourceConfig, http_get: HttpGet) -> Source:
    # config.py validates the type against SOURCE_SETTINGS; keep the two in step.
    if config.type == "triton":
        return TritonSource(config.settings["mount"], http_get)
    if config.type == "qmusic":
        return QmusicSource(config.settings["station"], http_get)
    if config.type == "npo":
        return NpoSource(config.settings["site"], http_get)
    raise ValueError(f"unknown source type {config.type!r}")
