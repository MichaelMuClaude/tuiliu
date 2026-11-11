"""Choose the next cards to show and record what was seen. No AI calls here, so swiping stays instant.

Topics come up at random, all equally often (the user's wish, 2026-11-06: no learned topic weights).
The one exception, also the user's choice (2026-11-11): 莉娅's cards of the day come up about one card in eight.
"""
import json
import random
from collections import defaultdict

from . import db, weather

RECENT_TOPICS = 6       # a topic among the last few cards seen waits until others have had a turn
RECYCLE_AFTER_DAYS = 21  # evergreen cards may come back after this long
WEIGHT_MIN, WEIGHT_MAX = 0.2, 3.0   # only for the topics API; the feed no longer uses weights
REVIEW_SHARE = 0.08      # about one card in twelve is a favourite coming back for another look
REVIEW_FIRST_GAP = 3     # days before a favourite first comes back; the gap doubles each time it is shown
REVIEW_MAX_GAP = 60
LIYA_EVERY = 8           # 莉娅's cards for today: about one in this many cards (user, 2026-11-11)

_CARD_SQL = """
SELECT c.id, c.title, c.points, c.takeaway, c.score, c.evergreen, c.created, c.topic_id,
       c.shown_count, c.last_shown, c.reaction, c.image,
       COALESCE(t.name, '') AS topic, COALESCE(t.kind, '') AS kind, COALESCE(th.name, '') AS theme, th.id AS theme_id
FROM cards c
LEFT JOIN topics t ON t.id = c.topic_id
LEFT JOIN themes th ON th.id = t.theme_id
WHERE c.reaction >= 0
  AND c.topic_id IS NOT NULL          -- cards of a deleted topic stop showing (favourites keep them)
  AND COALESCE(t.paused, 0) = 0 AND COALESCE(th.paused, 0) = 0
"""
# The weather card comes first once and 莉娅's cards have their own place; neither is picked at random.
_NOT_SPECIAL = " AND COALESCE(t.kind, '') NOT IN ('weather', 'liya')"
# 莉娅's cards (topic kind 'liya'): a picture of her in the day's outfit and what she wants to say; only on their day.
_LIYA_TODAY = (" AND t.kind = 'liya' AND c.shown_count = 0"
               " AND date(c.created, 'unixepoch', 'localtime') = date('now', 'localtime') ORDER BY c.created")


def _value(card: dict, now: float) -> float:
    age_days = (now - card["created"]) / 86400
    if card["evergreen"]:
        fresh = 1.0
    else:
        fresh = 1.5 if age_days < 2 else (1.0 if age_days < 14 else 0.4)
    return card["score"] / 10 * fresh * random.uniform(0.8, 1.2)


def next_cards(n: int, exclude: set[int]) -> list[dict]:
    now = db.now()
    pool = [c for c in db.rows(_CARD_SQL + _NOT_SPECIAL + " AND c.shown_count = 0") if c["id"] not in exclude]
    if len(pool) < n * 3:
        cutoff = now - RECYCLE_AFTER_DAYS * 86400
        pool += [c for c in db.rows(_CARD_SQL + _NOT_SPECIAL + " AND c.shown_count > 0 AND c.evergreen = 1 AND c.last_shown < ?"
                                    " ORDER BY c.last_shown LIMIT 200", (cutoff,)) if c["id"] not in exclude]
    for c in pool:
        c["value"] = _value(c, now) * (0.6 if c["shown_count"] else 1.0)
    # Pick a topic at random (each topic equally likely, skipping those among the last few cards seen), then that
    # topic's best card: its value only orders cards within a topic.
    by_topic = defaultdict(list)
    for c in pool:
        by_topic[c["topic_id"]].append(c)
    recent = [r["topic_id"] for r in db.rows(
        "SELECT c.topic_id FROM events e JOIN cards c ON c.id = e.card_id WHERE e.type = 'view'"
        " ORDER BY e.id DESC LIMIT ?", (RECENT_TOPICS,))]
    picked = []
    while by_topic and len(picked) < n:
        window = ([c["topic_id"] for c in picked[::-1]] + recent)[:RECENT_TOPICS]
        topic = random.choice([t for t in by_topic if t not in window] or list(by_topic))
        choice = max(by_topic[topic], key=lambda c: c["value"])
        by_topic[topic].remove(choice)
        if not by_topic[topic]:
            del by_topic[topic]
        picked.append(choice)

    review = _due_favorite(exclude | {c["id"] for c in picked}, now) if random.random() < n * REVIEW_SHARE else None
    if review:
        picked.insert(random.randrange(1, len(picked) + 1) if picked else 0, review)

    if random.random() < n / LIYA_EVERY:
        liya = [c for c in db.rows(_CARD_SQL + _LIYA_TODAY) if c["id"] not in exclude]
        if liya:
            picked.insert(random.randrange(1, len(picked) + 1) if picked else 0, liya[0])

    wid = weather.todays_card()   # today's weather, first, until it has been seen once
    w = db.one(_CARD_SQL + " AND c.id = ? AND c.shown_count = 0", (wid,)) if wid and wid not in exclude else None
    if w:
        picked.insert(0, w)

    return [{
        "id": c["id"], "title": c["title"], "points": json.loads(c["points"]), "takeaway": c["takeaway"],
        "topic": c["topic"], "theme": c["theme"], "reaction": c["reaction"], "image": c["image"],
        "liya": c["kind"] == "liya", "review": c is review,
    } for c in picked]


def _due_favorite(exclude: set[int], now: float) -> dict | None:
    """A favourite the reader has not seen for a while (spaced: 3, 6, 12... days), so favourites resurface in the feed
    instead of waiting in the favourites list."""
    due = [c for c in db.rows(_CARD_SQL.replace("WHERE c.reaction >= 0", "WHERE c.reaction = 1") + _NOT_SPECIAL)
           if c["id"] not in exclude and now - c["last_shown"] >
           min(REVIEW_MAX_GAP, REVIEW_FIRST_GAP * 2 ** max(0, c["shown_count"] - 1)) * 86400]
    return min(due, key=lambda c: c["last_shown"]) if due else None


def record(card_id: int, kind: str, dwell_ms: int = 0) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO events(card_id, type, dwell_ms, ts) VALUES(?,?,?,?)", (card_id, kind, dwell_ms, db.now()))
        row = c.execute("SELECT reaction FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            return
        if kind == "view":
            c.execute("UPDATE cards SET shown_count = shown_count + 1, last_shown = ? WHERE id = ?", (db.now(), card_id))
        elif kind in ("like", "dislike", "clear"):
            new = {"like": 1, "dislike": -1, "clear": 0}[kind]
            old = row["reaction"]
            if new == old:
                return
            c.execute("UPDATE cards SET reaction = ? WHERE id = ?", (new, card_id))
