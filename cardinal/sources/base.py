"""Pluggable topic source adapters for the quest generator.

The GDD pipeline never knows where topic data came from: every adapter
returns a normalized TopicData record. Adding a new source (news API, RSS,
a lore wiki...) requires implementing TopicSource — zero changes to the
generation pipeline.

Pre-LLM source quality law (enforced here, before any token is spent):
  - no stub articles
  - no disambiguation pages
  - no texts under 500 words
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

MIN_WORDS = 500


class SourceQualityError(Exception):
    """Topic rejected before the LLM: stub, disambiguation, or too short."""


@dataclass
class TopicData:
    title: str
    summary_text: str
    full_text: str
    source_name: str
    source_url: str
    geo: dict[str, Any] | None = None
    quality_flags: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.full_text.split())


class TopicSource(ABC):
    name = "base"
    enabled = True

    @abstractmethod
    def fetch(self, keyword: str) -> TopicData:
        """Fetch and normalize topic data for a keyword.
        Raises SourceQualityError if the topic fails the pre-LLM filters."""

    def fetch_random(self) -> TopicData:
        raise NotImplementedError(f"source '{self.name}' does not support random topics")

    # ------------------------------------------------------------------ #
    @staticmethod
    def enforce_quality(topic: TopicData) -> TopicData:
        if "stub" in topic.quality_flags:
            raise SourceQualityError(f"'{topic.title}' is a stub article")
        if "disambiguation" in topic.quality_flags:
            raise SourceQualityError(f"'{topic.title}' is a disambiguation page")
        if topic.word_count < MIN_WORDS:
            raise SourceQualityError(
                f"'{topic.title}' has only {topic.word_count} words (< {MIN_WORDS})")
        return topic


_REGISTRY: dict[str, type[TopicSource]] = {}


def register(cls: type[TopicSource]) -> type[TopicSource]:
    _REGISTRY[cls.name] = cls
    return cls


def get_source(name: str) -> TopicSource:
    if name not in _REGISTRY:
        # import side effect: built-in adapters self-register
        from cardinal.sources import gutenberg, news_stub, osm, wikipedia  # noqa: F401
    if name not in _REGISTRY:
        raise KeyError(f"unknown topic source '{name}' (available: {sorted(_REGISTRY)})")
    cls = _REGISTRY[name]
    if not cls.enabled:
        raise RuntimeError(f"topic source '{name}' is registered but disabled")
    return cls()


def available_sources() -> dict[str, bool]:
    from cardinal.sources import gutenberg, news_stub, osm, wikipedia  # noqa: F401

    return {name: cls.enabled for name, cls in _REGISTRY.items()}
