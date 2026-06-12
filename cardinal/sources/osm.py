"""OpenStreetMap source adapter — geographic data for real-world places.

Uses Nominatim search to resolve a place and enrich the topic with geo
data (lat/lon, bounding box, place class). Falls back to Wikipedia text
for the narrative body so the GDD pipeline always gets enough words, with
the OSM geometry attached for the physics classifier.
"""
from __future__ import annotations

import requests

from cardinal.sources.base import TopicData, TopicSource, register

NOMINATIM = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "CardinalSystem/0.1 (autonomous quest generator; research sandbox)"}


@register
class OSMSource(TopicSource):
    name = "osm"
    enabled = True

    def fetch(self, keyword: str) -> TopicData:
        resp = requests.get(
            NOMINATIM,
            params={"q": keyword, "format": "jsonv2", "limit": 1,
                    "extratags": 1, "namedetails": 1},
            headers=HEADERS, timeout=20)
        resp.raise_for_status()
        results = resp.json()
        if not results:
            raise LookupError(f"OSM has no match for '{keyword}'")
        place = results[0]
        geo = {
            "lat": float(place["lat"]),
            "lon": float(place["lon"]),
            "category": place.get("category") or place.get("class"),
            "place_type": place.get("type"),
            "display_name": place.get("display_name"),
            "bounding_box": place.get("boundingbox"),
        }

        # Narrative body from Wikipedia (OSM provides geometry, not prose).
        from cardinal.sources.wikipedia import WikipediaSource

        wiki = WikipediaSource().fetch(keyword)
        topic = TopicData(
            title=wiki.title,
            summary_text=wiki.summary_text,
            full_text=wiki.full_text,
            source_name=self.name,
            source_url=f"https://www.openstreetmap.org/?mlat={geo['lat']}&mlon={geo['lon']}",
            geo=geo,
            quality_flags=wiki.quality_flags,
        )
        return self.enforce_quality(topic)
