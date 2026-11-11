"""Word cards from GlanceCards (词卡), the reader's English picture cards, mixed into the feed (user, 2026-11-11).

GlanceCards runs on the same machine and keeps its own cards, order and known pile; the feed only borrows its next
cards and passes "Got it" back. GlanceCards still works on its own for a words-only session.
If it is not running, the feed simply has no word cards.
"""
import logging
import random
import urllib.parse

import httpx

from .config import GLANCECARDS_URL

log = logging.getLogger("tuiliu.words")
WORD_EVERY = 5   # about one card in this many is a word card (user, 2026-11-11)


def next_cards(n: int) -> list[dict]:
    """About n / WORD_EVERY word cards from GlanceCards, shaped like feed cards (ids are strings: no events)."""
    k = int(n / WORD_EVERY) + (random.random() < n / WORD_EVERY % 1)
    if not k:
        return []
    try:
        r = httpx.get(f"{GLANCECARDS_URL}/api/next", params={"n": k}, timeout=3)
        r.raise_for_status()
        got = r.json()
    except Exception as e:
        log.info("no word cards: %s", e)
        return []
    return [{"id": "w" + c["id"], "file": c["id"], "title": c["word"], "points": [], "takeaway": "",
             "topic": "", "theme": "", "reaction": 0, "liya": False, "review": False, "word": True,
             "lookup": c.get("lookup", ""), "studying": c.get("featured", False), "image": "/word/img?f=" + urllib.parse.quote(c["id"], safe="")}
            for c in got]


def image(f: str) -> httpx.Response:
    return httpx.get(f"{GLANCECARDS_URL}/img", params={"f": f}, timeout=10)


def known(f: str) -> bool:
    """Tell GlanceCards the reader knows this card (it moves to the known pile)."""
    return httpx.post(f"{GLANCECARDS_URL}/api/known", params={"f": f}, timeout=10).status_code == 200


def study(f: str) -> bool:
    """不认识: GlanceCards puts the card on its study list, so it comes round more often (in both apps)."""
    return httpx.post(f"{GLANCECARDS_URL}/api/study", params={"f": f}, timeout=10).status_code == 200
