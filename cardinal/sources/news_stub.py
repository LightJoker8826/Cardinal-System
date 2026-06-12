"""News API source adapter — placeholder stub.

Interface-conformant and registered, but DISABLED. Activating a real news
source later (NewsAPI, GDELT, RSS...) means implementing fetch() and
flipping `enabled` — zero changes to the GDD generation pipeline.
"""
from __future__ import annotations

from cardinal.sources.base import TopicData, TopicSource, register


@register
class NewsStubSource(TopicSource):
    name = "news"
    enabled = False  # flip to True once a real backend is wired in

    def fetch(self, keyword: str) -> TopicData:
        raise NotImplementedError(
            "news source adapter is a placeholder — configure a news API backend "
            "and set enabled=True to activate")
