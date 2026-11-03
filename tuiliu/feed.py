"""Choose the next cards to show and learn from feedback. No AI calls here, so swiping stays instant."""
import json
import random

from . import db

EXPLORE = 0.15          # share of picks taken at random, so the feed does not narrow over time
RECYCLE_AFTER_DAYS = 21  # evergreen cards may come back after this long
WEIGHT_MIN, WEIGHT_MAX = 0.2, 3.0

_CARD_SQL = """
SELECT c.id, c.title, c.points, c.takeaway, c.score, c.evergreen, c.created, c.topic_id,
       c.shown_count, c.reaction,
       COALESCE(t.name, '') AS topic, COALESCE(th.name, '') AS theme, th.id AS theme_id,
       COALESCE(t.weight, 1) * COALESCE(th.weight, 1) AS weight
FROM cards c
LEFT JOIN topics t ON t.id = c.topic_id
LEFT JOIN themes th ON th.id = t.theme_id
WHERE c.reaction >= 0
  AND COALESCE(t.paused, 0) = 0 AND COALESCE(th.paused, 0) = 0
"""


def _value(card: dict, now: float) -> float:
    age_days = (now - card["created"]) / 86400
    if card["evergreen"]:
        fresh = 1.0
    else:
        fresh = 1.5 if age_days < 2 else (1.0 if age_days < 14 else 0.4)
    return card["score"] / 10 * card["weight"] * fresh * random.uniform(0.8, 1.2)


def next_cards(n: int, exclude: set[int]) -> list[dict]:
    now = db.now()
    pool = [c for c in db.rows(_CARD_SQL + " AND c.shown_count = 0") if c["id"] not in exclude]
    if len(pool) < n * 3:
        cutoff = now - RECYCLE_AFTER_DAYS * 86400
        pool += [c for c in db.rows(_CARD_SQL + " AND c.shown_count > 0 AND c.evergreen = 1 AND c.last_shown < ?"
                                    " ORDER BY c.last_shown LIMIT 200", (cutoff,)) if c["id"] not in exclude]
    for c in pool:
        c["value"] = _value(c, now) * (0.6 if c["shown_count"] else 1.0)
    pool.sort(key=lambda c: c["value"], reverse=True)

    picked, last_topic, last_theme = [], None, None
    while pool and len(picked) < n:
        if random.random() < EXPLORE:
            choice = random.choice(pool)
        else:
            # Topic rotation: the best card whose topic (ideally theme) differs from the previous one.
            choice = next((c for c in pool if c["theme_id"] != last_theme), None) \
                or next((c for c in pool if c["topic_id"] != last_topic), pool[0])
        pool.remove(choice)
        picked.append(choice)
        last_topic, last_theme = choice["topic_id"], choice["theme_id"]

    return [{
        "id": c["id"], "title": c["title"], "points": json.loads(c["points"]), "takeaway": c["takeaway"],
        "topic": c["topic"], "theme": c["theme"], "reaction": c["reaction"],
    } for c in picked]


def _nudge_topic(c, card_id: int, delta: float) -> None:
    c.execute("UPDATE topics SET weight = MIN(?, MAX(?, weight + ?))"
              " WHERE id = (SELECT topic_id FROM cards WHERE id = ?)", (WEIGHT_MAX, WEIGHT_MIN, delta, card_id))


def record(card_id: int, kind: str, dwell_ms: int = 0) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO events(card_id, type, dwell_ms, ts) VALUES(?,?,?,?)", (card_id, kind, dwell_ms, db.now()))
        row = c.execute("SELECT reaction FROM cards WHERE id=?", (card_id,)).fetchone()
        if not row:
            return
        if kind == "view":
            c.execute("UPDATE cards SET shown_count = shown_count + 1, last_shown = ? WHERE id = ?", (db.now(), card_id))
            if dwell_ms > 15000:
                _nudge_topic(c, card_id, 0.03)
            elif 0 < dwell_ms < 1500:
                _nudge_topic(c, card_id, -0.02)
        elif kind in ("like", "dislike", "clear"):
            new = {"like": 1, "dislike": -1, "clear": 0}[kind]
            old = row["reaction"]
            if new == old:
                return
            # Undo the old reaction's nudge, then apply the new one.
            _nudge_topic(c, card_id, -0.15 * old + 0.15 * new)
            c.execute("UPDATE cards SET reaction = ? WHERE id = ?", (new, card_id))
