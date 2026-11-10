"""莉娅's photos: one card per picture in her folder, so they come up at random in the feed like any other topic.

The folder (~/Pictures/Liya, top level only) keeps growing; a scan every few minutes adds a card for each new picture
and removes the card of a picture that was deleted. The cards are evergreen, so a photo comes back after a few weeks.
Removing or pausing the topic stops the scan.
"""
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from . import db

log = logging.getLogger("tuiliu.photos")

PHOTO_DIR = Path(os.environ.get("TUILIU_PHOTOS", Path.home() / "Pictures" / "Liya"))
SCAN_EVERY_SEC = 600
THEME, TOPIC = "莉娅", "莉娅的美照"
EXTS = {".png", ".jpg", ".jpeg", ".webp"}
URL_PREFIX = "/liya/"


def topic_id() -> int | None:
    """The photo topic, created the first time. None once the reader has removed or paused it."""
    tid = db.get_meta("photo_topic")
    if tid is not None:
        t = db.one("SELECT id, paused FROM topics WHERE id=?", (tid,))
        return t["id"] if t and not t["paused"] else None
    with db.tx() as c:
        th = c.execute("SELECT id FROM themes WHERE name=?", (THEME,)).fetchone()
        theme_id = th[0] if th else c.execute("INSERT INTO themes(name, created) VALUES(?,?)", (THEME, db.now())).lastrowid
        tid = c.execute("INSERT INTO topics(theme_id, name, description, kind, created) VALUES(?,?,?,'photo',?)",
                        (theme_id, TOPIC, f"{PHOTO_DIR} 里的照片，每张一张卡", db.now())).lastrowid
    db.set_meta("photo_topic", tid)
    return tid


def files() -> list[str]:
    """Picture names in the folder; raises OSError if it cannot be read (e.g. the NFS share is down)."""
    return sorted(f.name for f in PHOTO_DIR.iterdir() if f.is_file() and f.suffix.lower() in EXTS)


def _title(name: str) -> str:
    """"2026-11-10 白色汉服.png" -> "莉娅 📸 白色汉服"; a bare timestamp or file number gives just "莉娅 📸"."""
    stem = re.sub(r"^[\d\s._-]+", "", Path(name).stem)
    return f"莉娅 📸 {stem}" if stem and not stem.isascii() else "莉娅 📸"


def scan() -> None:
    tid = topic_id()
    if tid is None:
        return
    try:
        names = files()
    except OSError as e:
        log.warning("photo folder unreadable, skipping: %s", e)
        return
    if not names:   # an empty listing is more likely a missing mount than a deleted folder: change nothing
        return
    urls = {URL_PREFIX + quote(n): n for n in names}
    have = {r["image"]: r["id"] for r in db.rows("SELECT id, image FROM cards WHERE topic_id=?", (tid,))}
    new = [u for u in urls if u not in have]
    gone = [cid for u, cid in have.items() if u not in urls]
    with db.tx() as c:
        for n, u in enumerate(new):
            c.execute("INSERT INTO cards(topic_id, title, points, takeaway, score, evergreen, created, image)"
                      " VALUES(?,?,'[]','',10,1,?,?)", (tid, _title(urls[u]), db.now() + n * 1e-3, u))
        for cid in gone:
            c.execute("DELETE FROM cards WHERE id=?", (cid,))
    if new or gone:
        log.info("photos: %d added, %d removed", len(new), len(gone))


def run_if_due() -> None:
    """Called every minute by the scheduler."""
    now = time.time()
    if now - (db.get_meta("photo_scanned") or 0) < SCAN_EVERY_SEC:
        return
    db.set_meta("photo_scanned", now)
    scan()


def path(name: str) -> Path | None:
    """The file for a /liya/<name> request, only if it is a picture directly in the folder."""
    p = PHOTO_DIR / name
    return p if "/" not in name and p.suffix.lower() in EXTS and p.is_file() else None
