"""Photo cards from 相框 (frame-web, the photo viewer on the same machine), mixed into the feed (user, 2026-11-11).

相框 keeps the photos and its own rules: the feed borrows random photos from the folders it plays (the Liya folder
stays excluded there, so here too) about one card in eight, and passes swipes back: a sideways swipe moves the
photo to 相框's trash, a double tap moves it to 收藏, and 撤销 is 相框's own undo. Swiping up leaves the photo
where it is, so it can come round again. Sorting into folders stays in 相框 itself.
If 相框 is not running (or the photos are not mounted), the feed simply has no photo cards.
"""
import logging
import os
import random
import urllib.parse

import httpx

log = logging.getLogger("tuiliu.frame")
FRAME_URL = os.environ.get("TUILIU_FRAME", "http://127.0.0.1:8096")
PHOTO_EVERY = 8   # about one card in this many is a photo (user, 2026-11-11)
FAVOURITES = "收藏"
_JSON = {"Content-Type": "application/json"}   # 相框 takes JSON posts only


def next_cards(n: int) -> list[dict]:
    """About n / PHOTO_EVERY random photos, shaped like feed cards (ids are strings: no feed events)."""
    k = int(n / PHOTO_EVERY) + (random.random() < n / PHOTO_EVERY % 1)
    if not k:
        return []
    try:
        r = httpx.get(f"{FRAME_URL}/api/photos", timeout=5)
        r.raise_for_status()
        photos = [p for p in r.json()["photos"] if not p.get("excluded")]
    except Exception as e:
        log.info("no photo cards: %s", e)
        return []
    return [{"id": "p" + p["path"], "path": p["path"], "photo": True, "title": "", "points": [], "takeaway": "",
             "topic": "", "theme": "", "reaction": 1 if p["folder"].strip() == FAVOURITES else 0,
             "liya": False, "review": False, "folder": p["folder"].strip(),
             "image": "/photo/img?" + urllib.parse.urlencode({"p": p["path"], "m": int(p.get("mtime", 0))})}
            for p in random.sample(photos, min(k, len(photos)))]


def image(path: str) -> httpx.Response:
    return httpx.get(f"{FRAME_URL}/img", params={"s": "1600", "p": path}, timeout=30)


def act(what: str, path: str = "") -> dict | None:
    """what = trash | like | undo, passed straight to 相框's own API. Returns its answer (a like or an undo says
    where the photo is now), or None if it refused."""
    r = httpx.post(f"{FRAME_URL}/api/{what}", json={"path": path} if path else {}, headers=_JSON, timeout=30)
    return r.json() if r.status_code == 200 else None
