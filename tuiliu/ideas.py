"""Idea cards from 探索清单 (the boredom-cards app on the same machine), mixed into the feed (user, 2026-11-11).

探索清单 keeps its own cards: the feed borrows them about one card in six and passes swipes back (a sideways swipe
deletes the idea there, with undo; a double tap hearts it). The deck works like 探索清单's own page: ideas dated
today or earlier come first, then a shuffled round of the rest, hearted ones weighted up; swiping up leaves an idea
in the deck for a later round. 莉娅's photo cards are left out (the user moved her photos out of the feed).
If 探索清单 is not running, the feed simply has no idea cards.
"""
import datetime
import logging
import random
import urllib.parse

import httpx

from .explore import EXPLORE_URL

log = logging.getLogger("tuiliu.ideas")
IDEA_EVERY = 6   # about one card in this many is an idea card (user, 2026-11-11)

_deck: list[str] = []   # the rest of this round, ids in order


def _round(ideas: dict, today: str) -> list[str]:
    due = sorted((i for i, d in ideas.items() if d.get("due") and d["due"] <= today), key=lambda i: ideas[i]["due"])
    rest = [i for i, d in ideas.items() if not d.get("due")]
    rest.sort(key=lambda i: random.random() ** (1 / (3 if ideas[i].get("fav") else 1)), reverse=True)
    return due + rest


def _label(d: dict, today: str) -> str:
    if not d.get("due"):
        return ""
    late = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(d["due"])).days
    return "📅 今天" if late <= 0 else f"📅 过了 {late} 天"


def next_cards(n: int) -> list[dict]:
    """About n / IDEA_EVERY idea cards, shaped like feed cards (ids are strings: no feed events)."""
    k = int(n / IDEA_EVERY) + (random.random() < n / IDEA_EVERY % 1)
    if not k:
        return []
    try:
        r = httpx.get(f"{EXPLORE_URL}/api/ideas", timeout=3)
        r.raise_for_status()
        everything = r.json()
    except Exception as e:
        log.info("no idea cards: %s", e)
        return []
    today = datetime.date.today().isoformat()
    ideas = {i: d for i, d in everything.items() if not d.get("deleted") and not d.get("photo")
             and d.get("text") and (not d.get("due") or d["due"] <= today)}
    out = []
    for _ in range(2):
        while _deck and len(out) < k:
            i = _deck.pop(0)
            if i in ideas and all(c["idea_id"] != i for c in out):
                out.append(_card(i, ideas[i], today))
        if len(out) >= k:
            break
        _deck[:] = _round(ideas, today)
    return out


def _card(i: str, d: dict, today: str) -> dict:
    return {"id": "i" + i, "idea_id": i, "idea": True, "title": d["text"], "points": [], "takeaway": "",
            "topic": "", "theme": "", "reaction": 1 if d.get("fav") else 0, "liya": False, "review": False,
            "when": _label(d, today),
            "image": "/idea/img/" + urllib.parse.quote(d["image"]) if d.get("image") else ""}


def image(name: str) -> httpx.Response:
    return httpx.get(f"{EXPLORE_URL}/images/{urllib.parse.quote(name)}", timeout=10)


def act(idea_id: str, what: str, body: dict) -> bool:
    """what = delete | restore | fav, passed straight to 探索清单's own API."""
    r = httpx.post(f"{EXPLORE_URL}/api/ideas/{urllib.parse.quote(idea_id)}/{what}", json=body, timeout=10)
    return r.status_code == 200
