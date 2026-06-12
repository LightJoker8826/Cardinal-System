"""Community sentiment reader — the Social Control module's listening post.

Monitors configured subreddits (Async PRAW) for player feedback, parses
sentiment through Cardinal's LLM layer (keyword scorer at L2/Mock), weights
it by engagement (upvotes + comments), and stores everything in
sentiment_log.

Signal routing:
  - boredom with an enemy type      -> flag that enemy for SEC evolution
  - praise/complaints about an item -> soft trigger for the Order Control
                                       module (read by balancer.sentiment_soft_flags)
  - high-engagement suggestions     -> weighted topic inputs for quest gen

Credentials come from .env (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET /
REDDIT_USER_AGENT) and the module FAILS SILENTLY when they are missing.
Default schedule: every 6 hours (CARDINAL_SENTIMENT_INTERVAL_HOURS).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from cardinal.core import db
from cardinal.core.config import SEV_DEBUG, SEV_INFO, SEV_WARNING, get_config, log_event
from cardinal.llm.provider import complete_with_fallback

SENTIMENT_SYSTEM_PROMPT = (
    "You are a community sentiment analyzer for a game studio. "
    "Given posts, output only a JSON array of objects with fields "
    "'topic' (string) and 'sentiment_score' (float in [-1, 1]). No commentary."
)

BOREDOM_WORDS = ("boring", "bored", "stale", "repetitive", "same old", "snooze")


async def fetch_posts(limit: int = 25) -> list[dict[str, Any]]:
    """Pull recent posts from configured subreddits. Empty list (silently)
    when credentials are not configured or the API is unreachable."""
    cfg = get_config()
    if not cfg.reddit_configured:
        log_event("sentiment", "reddit credentials not configured — skipping silently", SEV_DEBUG)
        return []
    try:
        import asyncpraw

        posts: list[dict[str, Any]] = []
        async with asyncpraw.Reddit(
            client_id=cfg.reddit_client_id,
            client_secret=cfg.reddit_client_secret,
            user_agent=cfg.reddit_user_agent,
        ) as reddit:
            for sub_name in cfg.subreddits:
                try:
                    subreddit = await reddit.subreddit(sub_name)
                    async for submission in subreddit.hot(limit=limit):
                        posts.append({
                            "title": submission.title or "",
                            "text": (submission.selftext or "")[:2000],
                            "score": int(submission.score or 0),
                            "num_comments": int(submission.num_comments or 0),
                            "url": f"https://reddit.com{submission.permalink}",
                            "subreddit": sub_name,
                        })
                except Exception:
                    continue  # one bad subreddit must not kill the cycle
        return posts
    except Exception as err:
        log_event("sentiment", f"reddit fetch failed silently: {type(err).__name__}", SEV_DEBUG)
        return []


def engagement_weight(post: dict[str, Any]) -> float:
    """0..1+ weight from upvotes and comment activity."""
    import math

    return round(math.log10(1 + max(0, post.get("score", 0)) + 2 * max(0, post.get("num_comments", 0))) / 3, 4)


def analyze_and_store(posts: list[dict[str, Any]]) -> int:
    """Score posts through the LLM layer and write sentiment_log rows.
    Returns the number of rows stored."""
    if not posts:
        return 0
    resp = complete_with_fallback(
        "sentiment", "sentiment", SENTIMENT_SYSTEM_PROMPT,
        json.dumps([{"title": p["title"], "text": p["text"][:500]} for p in posts]),
        context={"posts": posts},
    )
    try:
        scores = json.loads(resp.text)
    except json.JSONDecodeError:
        log_event("sentiment", "sentiment output unparseable — cycle skipped", SEV_WARNING)
        return 0

    stored = 0
    now = datetime.now(timezone.utc).isoformat()
    for post, score_row in zip(posts, scores):
        score = float(score_row.get("sentiment_score", 0.0))
        weight = engagement_weight(post)
        routed = _route_signal(post, score, weight)
        db.execute(
            """INSERT INTO sentiment_log
               (source, topic, sentiment_score, engagement_weight, post_url, excerpt, routed_to, timestamp)
               VALUES (?,?,?,?,?,?,?,?)""",
            (f"reddit/{post['subreddit']}", post["title"][:200], score, weight,
             post["url"], post["text"][:500], routed, now),
        )
        stored += 1
    log_event("sentiment", f"stored {stored} sentiment readings", SEV_INFO)
    return stored


def _route_signal(post: dict[str, Any], score: float, weight: float) -> str:
    """Route strong signals to the right Cardinal module. Returns routing tag."""
    text = f"{post['title']} {post['text']}".lower()
    routes: list[str] = []

    # Boredom with an enemy type -> flag for SEC behavioral evolution
    if any(w in text for w in BOREDOM_WORDS):
        try:
            from cardinal.modules import sec

            for enemy_type in sec.load_policies()["defaults"]:
                if enemy_type.lower() in text:
                    sec.evolve(enemy_type, force=True)
                    routes.append(f"sec:{enemy_type}")
                    _notify_soft_trigger(f"Community boredom with {enemy_type}",
                                         post["url"], score, weight)
        except Exception:
            pass

    # Item praise/complaints -> soft trigger consumed by the balancer
    # (balancer.sentiment_soft_flags reads sentiment_log directly; we only tag)
    if abs(score) >= 0.5 and weight >= 0.3:
        routes.append("balancer:soft")

    # High-engagement positive suggestions -> quest topic inputs
    if score > 0.5 and weight >= 0.5:
        routes.append("questgen:topic")

    return ",".join(routes) or "none"


def _notify_soft_trigger(reason: str, url: str, score: float, weight: float) -> None:
    try:
        from cardinal.modules import notifier

        notifier.notify_sync(
            "Community sentiment trigger",
            f"{reason}\n{url}",
            notifier.COLOR_WARNING,
            {"sentiment": f"{score:+.2f}", "engagement": f"{weight:.2f}"},
        )
    except Exception:
        pass


async def run_cycle() -> int:
    posts = await fetch_posts()
    return await asyncio.to_thread(analyze_and_store, posts)


async def daemon() -> None:
    cfg = get_config()
    interval_s = cfg.sentiment_interval_hours * 3600
    db.init_db()
    if not cfg.reddit_configured:
        log_event("sentiment",
                  "Reddit credentials not configured — sentiment reader idle (fail-silent)",
                  SEV_INFO)
    log_event("sentiment", f"sentiment reader online (every {cfg.sentiment_interval_hours:g}h)", SEV_INFO)
    while True:
        try:
            await run_cycle()
        except Exception as err:  # the reader must never crash the schedule
            log_event("sentiment", f"cycle error suppressed: {type(err).__name__}", SEV_DEBUG)
        await asyncio.sleep(interval_s)
