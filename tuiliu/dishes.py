"""Dish cards from 御膳房 (menu-feed, the user's menu of dishes they can cook, on the same machine), mixed into the
feed (user, 2026-11-11).

御膳房 keeps the dishes: the feed borrows about one card in ten, for the meal of the hour as 御膳房 itself picks
(早餐 from 5:00 to 10:30, otherwise 正餐; 不限 dishes always, fruit never), and dishes eaten in the last few days
less often. A double tap is 就吃这个 (logged in 御膳房), a sideways swipe takes the dish off the menu, and 撤销
undoes either. If 御膳房 is not running, the feed simply has no dish cards.
"""
import datetime
import logging
import os
import random
import re
import time
import urllib.parse

import httpx

log = logging.getLogger("tuiliu.dishes")
MENU_URL = os.environ.get("TUILIU_MENU", "http://127.0.0.1:8093")
DISH_EVERY = 10   # about one card in this many is a dish (user, 2026-11-11)


def _meal_now() -> str:
    t = datetime.datetime.now()
    return "早餐" if 300 <= t.hour * 60 + t.minute < 630 else "正餐"


def _weight(d: dict) -> float:
    """As in 御膳房: eaten within a day = rarely, within three days = less often."""
    if not d.get("lastEaten"):
        return 1
    days = (time.time() * 1000 - d["lastEaten"]) / 864e5
    return 0.12 if days < 1 else 0.45 if days < 3 else 1


def next_cards(n: int) -> list[dict]:
    k = int(n / DISH_EVERY) + (random.random() < n / DISH_EVERY % 1)
    if not k:
        return []
    try:
        r = httpx.get(f"{MENU_URL}/api/dishes", timeout=3)
        r.raise_for_status()
        got = r.json()
    except Exception as e:
        log.info("no dish cards: %s", e)
        return []
    dishes = got if isinstance(got, list) else [{"id": i, **d} for i, d in got.items()]
    meal = _meal_now()
    pool = [d for d in dishes if not d.get("deleted")
            and {"中餐": "正餐", "晚餐": "正餐"}.get(d.get("meal"), d.get("meal") or "不限") in (meal, "不限")]
    pool.sort(key=lambda d: random.random() ** (1 / _weight(d)), reverse=True)
    return [_card(d) for d in pool[:k]]


def _eaten_text(ms) -> str:
    if not ms:
        return "还没记过"
    days = (datetime.date.today() - datetime.date.fromtimestamp(ms / 1000)).days
    return "今天吃过" if days <= 0 else "昨天吃过" if days == 1 else f"{days} 天前吃过"


def _card(d: dict) -> dict:
    m = re.search(r"(\d+(?:\s*[-–~]\s*\d+)?)\s*(分钟|秒)", d.get("note") or "")
    chips = [d.get("meal") or "不限"] + (["现成品"] if d.get("ready") else []) + ([f"约 {m.group(1)} {m.group(2)}"] if m else [])
    photo = d.get("photo") or ""
    return {"id": "d" + d["id"], "dish_id": d["id"], "dish": True, "title": d["name"],
            "points": [x for x in (d.get("note") or "", " · ".join(d.get("ingredients") or [])) if x],
            "takeaway": "", "topic": "", "theme": "", "reaction": 0, "liya": False, "review": False,
            "chips": chips, "eaten": _eaten_text(d.get("lastEaten")), "last_eaten": d.get("lastEaten"),
            "image": "/dish/img/" + urllib.parse.quote(photo.rsplit("/", 1)[-1]) if photo else ""}


def image(name: str) -> httpx.Response:
    return httpx.get(f"{MENU_URL}/images/{urllib.parse.quote(name)}", timeout=10)


def act(dish_id: str, what: str, body: dict) -> dict | None:
    """what = eaten | delete | restore, passed straight to 御膳房's own API; returns the dish."""
    r = httpx.post(f"{MENU_URL}/api/dishes/{urllib.parse.quote(dish_id)}/{what}", json=body, timeout=10)
    return r.json() if r.status_code == 200 else None
