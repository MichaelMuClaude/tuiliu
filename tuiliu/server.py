"""Web server: the swipe page, the feed API, feedback and topic management. Runs the pipeline on a timer."""
import json
import logging
import re
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, explore, feed, pipeline, seed, weather, words
from .config import HOST, LIYA_CARD_DIR, PIPELINE_INTERVAL_MIN, PORT, SCHEDULER, STATIC_DIR, has_api_key

log = logging.getLogger("tuiliu.server")
app = FastAPI(title="推流")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
_last_low_pool_run = 0.0


def _run_in_background(fn, *args) -> None:
    def wrapper():
        try:
            fn(*args)
        except Exception:
            log.exception("background job failed")
    threading.Thread(target=wrapper, daemon=True).start()


def _scheduler() -> None:
    while True:
        last = db.get_meta("last_run", 0) or 0
        if time.time() - last > PIPELINE_INTERVAL_MIN * 60:
            try:
                pipeline.run_once()
            except Exception:
                log.exception("pipeline run failed")
        try:
            weather.run_if_due()
        except Exception:
            log.exception("weather card failed")
        time.sleep(60)


@app.on_event("startup")
def startup() -> None:
    db.init()
    seed.apply()
    db.set_meta("pipeline_running", False)
    if SCHEDULER:
        threading.Thread(target=_scheduler, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/liya/{name}")
def liya_picture(name: str):
    """A picture for 莉娅's cards, from her folder (never in the repo, which is public)."""
    p = LIYA_CARD_DIR / name
    if "/" in name or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp") or not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "max-age=86400"})


@app.get("/word/img")
def word_picture(f: str):
    """A word card's picture, passed through from GlanceCards (which only serves its own cards)."""
    try:
        r = words.image(f)
    except Exception:
        raise HTTPException(502)
    if r.status_code != 200:
        raise HTTPException(404)
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400"})


class WordKnown(BaseModel):
    file: str


@app.post("/api/word/known")
def word_known(body: WordKnown):
    """Got it: GlanceCards moves the card to its known pile, as if it had been swiped there."""
    try:
        ok = words.known(body.file)
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(502, "GlanceCards did not take it")
    return {"ok": True}


@app.post("/api/word/study")
def word_study(body: WordKnown):
    """不认识: GlanceCards adds the card to its study list, so it comes round more often."""
    try:
        ok = words.study(body.file)
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(502, "GlanceCards did not take it")
    return {"ok": True}


@app.post("/api/word/undo")
def word_undo():
    """Takes back the last 认识了 (a sideways swipe), as GlanceCards' own undo does."""
    try:
        ok = words.undo()
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(502, "GlanceCards did not take it")
    return {"ok": True}


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/api/feed")
def get_feed(n: int = 8, exclude: str = ""):
    global _last_low_pool_run
    ids = {int(x) for x in exclude.split(",") if x.isdigit()}
    cards = feed.next_cards(min(n, 20), ids)
    unseen = db.one("SELECT COUNT(*) n FROM cards WHERE shown_count=0")["n"]
    if SCHEDULER and unseen < 30 and time.time() - _last_low_pool_run > 600:
        _last_low_pool_run = time.time()
        _run_in_background(pipeline.run_once, True)
    return {"cards": cards, "preparing": not cards and bool(db.get_meta("pipeline_running", False))}


class Event(BaseModel):
    card_id: int
    type: str
    dwell_ms: int = 0


@app.post("/api/event")
def post_event(e: Event):
    if e.type not in ("view", "like", "dislike", "clear"):
        raise HTTPException(400, "unknown event type")
    feed.record(e.card_id, e.type, e.dwell_ms)
    return {"ok": True}


class Explore(BaseModel):
    today: str = ""


@app.post("/api/cards/{card_id}/explore")
def post_explore(card_id: int, body: Explore):
    """📌: put the card on 探索清单, dated for when it should come up. A second tap returns the card already made."""
    if not has_api_key():
        raise HTTPException(503, "没有 API key，加不了")
    try:
        return explore.add(card_id, body.today)
    except LookupError:
        raise HTTPException(404, "找不到这张卡")
    except Exception as e:
        log.exception("explore failed")
        raise HTTPException(502, f"没加上：{e}")


@app.get("/api/favorites")
def get_favorites():
    """Cards the user double-tapped (reaction = 1), newest favourite first."""
    rows = db.rows("""
        SELECT c.id, c.title, c.points, c.takeaway, c.image, COALESCE(t.name, '') AS topic, MAX(e.ts) AS saved
        FROM cards c
        LEFT JOIN topics t ON t.id = c.topic_id
        LEFT JOIN events e ON e.card_id = c.id AND e.type = 'like'
        WHERE c.reaction = 1
        GROUP BY c.id ORDER BY saved DESC""")
    for r in rows:
        r["points"] = json.loads(r["points"])
    return {"cards": rows}


@app.get("/api/topics")
def get_topics():
    themes = db.rows("SELECT * FROM themes ORDER BY id")
    counts = {r["topic_id"]: r["n"] for r in db.rows("SELECT topic_id, COUNT(*) n FROM cards GROUP BY topic_id")}
    for th in themes:
        th["topics"] = db.rows("SELECT * FROM topics WHERE theme_id=? ORDER BY id", (th["id"],))
        for t in th["topics"]:
            t["keywords"] = json.loads(t["keywords"] or "[]")
            t["cards"] = counts.get(t["id"], 0)
    return {"themes": themes}


class NewTopic(BaseModel):
    description: str


KEYWORD_THEME = "关键词"


@app.post("/api/topics")
def add_topic(body: NewTopic):
    """The reader types search keywords (several separated by commas); they become a news topic whose keywords are
    searched in the news straight away and every few hours after. No AI step: the words are used as typed."""
    keywords = [k.strip() for k in re.split(r"[,，、;；\n]+", body.description) if k.strip()]
    if not keywords:
        raise HTTPException(400, "empty keywords")
    with db.tx() as c:
        th = c.execute("SELECT id FROM themes WHERE name=?", (KEYWORD_THEME,)).fetchone()
        theme_id = th["id"] if th else c.execute("INSERT INTO themes(name, created) VALUES(?,?)",
                                                 (KEYWORD_THEME, db.now())).lastrowid
        topic_id = c.execute(
            "INSERT INTO topics(theme_id, name, description, keywords, kind, created) VALUES(?,?,?,?,'news',?)",
            (theme_id, keywords[0][:30], "关于「" + "、".join(keywords) + "」的最新新闻",
             json.dumps(keywords, ensure_ascii=False), db.now())).lastrowid
    _run_in_background(pipeline.backfill_topic, topic_id, False)
    return {**db.one("SELECT * FROM topics WHERE id=?", (topic_id,)),
            "message": f"已添加，正在搜索「{'、'.join(keywords)}」的最新新闻"}


class Patch(BaseModel):
    name: str | None = None
    weight: float | None = None
    paused: bool | None = None


def _patch(table: str, row_id: int, p: Patch) -> dict:
    fields = {k: v for k, v in p.model_dump().items() if v is not None}
    if "weight" in fields:
        fields["weight"] = min(feed.WEIGHT_MAX, max(feed.WEIGHT_MIN, fields["weight"]))
    if "paused" in fields:
        fields["paused"] = int(fields["paused"])
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        with db.tx() as c:
            c.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), row_id))
    row = db.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
    if not row:
        raise HTTPException(404)
    return row


@app.patch("/api/topics/{topic_id}")
def patch_topic(topic_id: int, p: Patch):
    return _patch("topics", topic_id, p)


@app.patch("/api/themes/{theme_id}")
def patch_theme(theme_id: int, p: Patch):
    return _patch("themes", theme_id, p)


@app.delete("/api/topics/{topic_id}")
def delete_topic(topic_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM topics WHERE id=?", (topic_id,))
        # SQLite reuses the highest id, so a later topic could inherit this one's queued items: drop them.
        c.execute("UPDATE items SET status=CASE WHEN status IN ('new','ready') THEN 'rejected' ELSE status END,"
                  " reason=CASE WHEN status IN ('new','ready') THEN '话题已删除' ELSE reason END,"
                  " topic_hint=NULL WHERE topic_hint=?", (topic_id,))
        # The app only shows topics, so a theme with none left has no way back; drop it.
        c.execute("DELETE FROM themes WHERE id NOT IN (SELECT theme_id FROM topics)")
    return {"ok": True}


@app.delete("/api/themes/{theme_id}")
def delete_theme(theme_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM themes WHERE id=?", (theme_id,))
    return {"ok": True}


@app.get("/api/status")
def status():
    return {
        "has_key": has_api_key(),
        "running": bool(db.get_meta("pipeline_running", False)),
        "last_run": db.get_meta("last_run", 0),
        "last_card": db.one("SELECT MAX(created) t FROM cards")["t"] or 0,
        "stats": pipeline.stats(),
        "google_blocked": bool(db.get_meta("google_blocked", 0)),
    }


@app.post("/api/refresh")
def refresh():
    _run_in_background(pipeline.run_once, True)
    return {"ok": True}


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
