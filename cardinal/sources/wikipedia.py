"""Wikipedia REST API source adapter (the default for the prototype)."""
from __future__ import annotations

import random
from datetime import date

import requests

from cardinal.sources.base import TopicData, TopicSource, register

HEADERS = {"User-Agent": "CardinalSystem/0.1 (autonomous quest generator; research sandbox)"}
SUMMARY_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
FEATURED_API = "https://en.wikipedia.org/api/rest_v1/feed/featured/{y}/{m:02d}/{d:02d}"
EXTRACT_API = "https://en.wikipedia.org/w/api.php"


@register
class WikipediaSource(TopicSource):
    name = "wikipedia"
    enabled = True

    def fetch(self, keyword: str) -> TopicData:
        title = keyword.strip().replace(" ", "_")
        resp = requests.get(SUMMARY_API.format(title=title), headers=HEADERS, timeout=20)
        if resp.status_code == 404:
            raise LookupError(f"Wikipedia has no page for '{keyword}'")
        resp.raise_for_status()
        summary = resp.json()

        flags: list[str] = []
        if summary.get("type") == "disambiguation":
            flags.append("disambiguation")

        full_text = self._full_text(summary.get("title", keyword))
        if not full_text:
            full_text = summary.get("extract", "")
        # Wikipedia marks stubs in the article text/categories; cheap heuristic:
        if "stub" in full_text[-500:].lower() and len(full_text.split()) < 700:
            flags.append("stub")

        topic = TopicData(
            title=summary.get("title", keyword),
            summary_text=summary.get("extract", ""),
            full_text=full_text,
            source_name=self.name,
            source_url=summary.get("content_urls", {}).get("desktop", {}).get(
                "page", f"https://en.wikipedia.org/wiki/{title}"),
            quality_flags=flags,
        )
        return self.enforce_quality(topic)

    def fetch_random(self) -> TopicData:
        today = date.today()
        resp = requests.get(
            FEATURED_API.format(y=today.year, m=today.month, d=today.day),
            headers=HEADERS, timeout=20)
        resp.raise_for_status()
        feed = resp.json()
        candidates: list[str] = []
        tfa = feed.get("tfa")
        if tfa:
            candidates.append(tfa.get("title", ""))
        for article in feed.get("mostread", {}).get("articles", [])[:15]:
            candidates.append(article.get("title", ""))
        candidates = [c for c in candidates if c and ":" not in c]
        random.shuffle(candidates)
        last_err: Exception | None = None
        for title in candidates[:5]:
            try:
                return self.fetch(title.replace("_", " "))
            except Exception as err:  # quality reject or fetch error — try next
                last_err = err
        raise LookupError(f"no usable featured article today ({last_err})")

    @staticmethod
    def _full_text(title: str) -> str:
        try:
            resp = requests.get(
                EXTRACT_API,
                params={"action": "query", "prop": "extracts", "explaintext": 1,
                        "format": "json", "titles": title, "redirects": 1},
                headers=HEADERS, timeout=20)
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                return page.get("extract", "") or ""
        except requests.RequestException:
            return ""
        return ""
