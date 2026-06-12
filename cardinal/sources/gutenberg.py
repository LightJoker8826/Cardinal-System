"""Project Gutenberg source adapter — public-domain folklore & literature.

Uses the Gutendex API for search and streams only the opening excerpt of
the book text (never the whole file) as quest source material.
"""
from __future__ import annotations

import requests

from cardinal.sources.base import TopicData, TopicSource, register

GUTENDEX = "https://gutendex.com/books"
HEADERS = {"User-Agent": "CardinalSystem/0.1 (autonomous quest generator; research sandbox)"}
EXCERPT_CHARS = 12000


@register
class GutenbergSource(TopicSource):
    name = "gutenberg"
    enabled = True

    def fetch(self, keyword: str) -> TopicData:
        resp = requests.get(GUTENDEX, params={"search": keyword}, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            raise LookupError(f"Project Gutenberg has no match for '{keyword}'")
        book = results[0]
        text_url = None
        for fmt, url in book.get("formats", {}).items():
            if fmt.startswith("text/plain"):
                text_url = url
                break
        excerpt = self._stream_excerpt(text_url) if text_url else ""
        subjects = "; ".join(book.get("subjects", [])[:6])
        authors = ", ".join(a.get("name", "?") for a in book.get("authors", []))
        summary = f"{book.get('title')} by {authors}. Subjects: {subjects}."
        topic = TopicData(
            title=book.get("title", keyword),
            summary_text=summary,
            full_text=excerpt or summary,
            source_name=self.name,
            source_url=f"https://www.gutenberg.org/ebooks/{book.get('id')}",
            quality_flags=[],
        )
        return self.enforce_quality(topic)

    @staticmethod
    def _stream_excerpt(url: str) -> str:
        try:
            with requests.get(url, headers=HEADERS, timeout=30, stream=True) as resp:
                resp.raise_for_status()
                chunks: list[str] = []
                size = 0
                for chunk in resp.iter_content(chunk_size=4096, decode_unicode=True):
                    if not isinstance(chunk, str):
                        chunk = chunk.decode("utf-8", errors="replace")
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= EXCERPT_CHARS:
                        break
                return "".join(chunks)[:EXCERPT_CHARS]
        except requests.RequestException:
            return ""
