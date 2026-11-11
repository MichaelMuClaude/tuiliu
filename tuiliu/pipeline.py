"""Background pipeline: fetch -> full text -> dedupe -> judge and write cards -> card pool.

    python -m tuiliu.pipeline run          # one full pass
    python -m tuiliu.pipeline backfill ID  # search sources for one topic now
    python -m tuiliu.pipeline stats
"""
import json
import logging
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import ai, db, seed, sources
from .config import PIPELINE_INTERVAL_MIN, RAW_TEXT_KEEP_DAYS, has_api_key

log = logging.getLogger("tuiliu.pipeline")
_run_lock = threading.Lock()
WORKERS = 6
AI_WORKERS = 3     # few, so each card sees the ones written just before it (duplicate check)
TEXT_LIMIT = 5000  # characters of an article sent to the model, same as the session batches
MAX_PER_RUN = 200
SCREEN_BATCH = 100   # titles compared with each other in one screening call
STALE_NEWS_DAYS = 10


def _insert_items(found: list[dict], source_id: int | None, origin: str, topic_hint: int | None = None) -> list[int]:
    """Insert new items, skipping known URLs and near-identical titles. Returns new ids."""
    new_ids = []
    with db.tx() as c:
        for f in found:
            nt = sources.norm_title(f["title"])
            if len(nt) < 4:
                continue
            if c.execute("SELECT 1 FROM items WHERE url=? OR norm_title=?", (f["url"], nt)).fetchone():
                continue
            has_text = bool(f.get("text"))
            cur = c.execute(
                "INSERT INTO items(url, title, norm_title, summary, text, published, fetched, source_id, origin, status,"
                " topic_hint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f["url"], f["title"], nt, f.get("summary", ""), f.get("text", ""), f.get("published"),
                 db.now(), source_id, origin, "ready" if has_text else "new", topic_hint))
            new_ids.append(cur.lastrowid)
    return new_ids


def fetch_sources(force: bool = False) -> int:
    due = db.now() - PIPELINE_INTERVAL_MIN * 60
    total = 0
    for s in db.rows("SELECT * FROM sources WHERE enabled=1"):
        if not force and s["last_fetched"] > due:
            continue
        if "reddit.com" in s["url"]:
            time.sleep(3)  # Reddit answers 429 to back-to-back requests
        try:
            found = sources.fetch_source(s["kind"], s["url"])
            origin = "collection" if s["kind"] == "collection" else "feed"
            # A source added for one topic files its cards under that topic.
            n = len(_insert_items(found, s["id"], origin, s["topic_id"]))
            total += n
            log.info("fetched %-20s %3d found, %3d new", s["name"], len(found), n)
        except Exception as e:  # one broken feed must not stop the rest
            log.warning("fetch failed %s: %s", s["name"], e)
        with db.tx() as c:
            c.execute("UPDATE sources SET last_fetched=? WHERE id=?", (db.now(), s["id"]))
    return total


def _fill_one(item: dict) -> None:
    text = item["summary"] if len(item["summary"]) >= 1200 else ""
    if not text:
        try:
            text = sources.fetch_text(item["url"])
        except Exception as e:
            log.debug("text fetch failed %s: %s", item["url"], e)
    if len(text) < 300:
        text = (item["summary"] + "\n" + text).strip()
    with db.tx() as c:
        if len(text) + len(item["title"]) < 200:
            c.execute("UPDATE items SET status='rejected', reason='正文太少' WHERE id=?", (item["id"],))
        else:
            c.execute("UPDATE items SET text=?, status='ready' WHERE id=?", (text, item["id"]))


def fill_text(ids: list[int] | None = None) -> None:
    if ids is None:
        todo = db.rows("SELECT id, url, title, summary FROM items WHERE status='new' ORDER BY published DESC LIMIT ?",
                       (MAX_PER_RUN,))
    else:
        todo = [db.one("SELECT id, url, title, summary FROM items WHERE id=?", (i,)) for i in ids]
        todo = [t for t in todo if t]
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(_fill_one, todo))


def active_topics() -> list[dict]:
    """News topics that articles can be filed under. Series topics get no articles: their cards are written once."""
    return db.rows("SELECT t.id, t.name, t.description, t.keywords, th.name AS theme FROM topics t"
                   " JOIN themes th ON th.id=t.theme_id WHERE t.paused=0 AND th.paused=0 AND t.kind='news'")


def recent_card_titles(days: int = 3) -> list[str]:
    """Titles of cards written lately, so the same story is not carded twice."""
    return [r["title"] for r in db.rows("SELECT title FROM cards WHERE item_id IS NOT NULL AND created>?"
                                        " ORDER BY created DESC LIMIT 300", (db.now() - days * 86400,))]


def taste_examples() -> list[str]:
    liked = [r["title"] for r in db.rows("SELECT title FROM cards WHERE reaction=1 ORDER BY last_shown DESC LIMIT 20")]
    picked = [r["title"] for r in db.rows("SELECT title FROM items WHERE origin='collection'")]
    random.shuffle(picked)
    return liked + picked[:30]


def _as_points(points) -> list[str]:
    """Cards from a Claude session sometimes give points as one string; split it into sentences."""
    if isinstance(points, list):
        return [str(p).strip() for p in points if str(p).strip()]
    text = str(points or "").strip()
    parts = [p.strip() for p in text.splitlines() if p.strip()]
    if len(parts) <= 1:
        parts = [s.strip() for s in re.findall(r"[^。！？!?]+[。！？!?]?", text) if s.strip()]
    return [re.sub(r"^[-•\d.、)\s]+", "", p) for p in parts]


def add_written_cards(topic_id: int, cards: list[dict]) -> int:
    """Cards written without a source article (a series from a Claude session). Returns how many were added."""
    n = 0
    with db.tx() as c:
        for k in cards:
            points = _as_points(k.get("points"))
            if not k.get("title", "").strip() or not points:
                continue
            c.execute("INSERT INTO cards(item_id, topic_id, title, points, takeaway, score, evergreen, created, image)"
                      " VALUES(NULL,?,?,?,?,?,?,?,?)",
                      (topic_id, k["title"].strip(), json.dumps(points[:4], ensure_ascii=False),
                       k.get("takeaway", "").strip(), k.get("score", 7), int(k.get("evergreen", True)),
                       db.now() + n * 1e-3, k.get("image", "")))
            n += 1
    return n


def apply_result(item_id: int, res: dict | None, valid_topics: set[int]) -> str:
    """Store one judgement (from the API or from a Claude session). Returns the new item status."""
    with db.tx() as c:
        if not res:
            c.execute("UPDATE items SET status='rejected', reason='模型拒绝或输出不完整' WHERE id=?", (item_id,))
            return "rejected"
        cards = []
        for k in res.get("cards", []):
            k["points"] = _as_points(k.get("points"))
            if k.get("title", "").strip() and k["points"]:
                cards.append(k)
        if not res.get("keep") or not cards:
            c.execute("UPDATE items SET status='rejected', score=?, reason=? WHERE id=?",
                      (res.get("score"), res.get("reason", ""), item_id))
            return "rejected"
        hint = c.execute("SELECT topic_hint FROM items WHERE id=?", (item_id,)).fetchone()["topic_hint"]
        if hint in valid_topics:
            topic_id = hint  # found by this topic's own search: keep it there so the topic shows up in the feed
        else:
            topic_id = res.get("topic_id") if res.get("topic_id") in valid_topics else None
        for k in cards[:3]:
            c.execute("INSERT INTO cards(item_id, topic_id, title, points, takeaway, score, evergreen, created)"
                      " VALUES(?,?,?,?,?,?,?,?)",
                      (item_id, topic_id, k["title"].strip(), json.dumps(k["points"][:4], ensure_ascii=False),
                       k.get("takeaway", "").strip(), res.get("score", 6), int(bool(res.get("evergreen"))), db.now()))
        c.execute("UPDATE items SET status='carded', score=?, reason=? WHERE id=?",
                  (res.get("score"), res.get("reason", ""), item_id))
        return "carded"


def _process_one(item: dict, system: str, topics: dict[int, dict]) -> None:
    try:
        # Read the recent titles per item, so a story carded a moment ago by another worker counts too.
        hint = topics.get(item["topic_hint"])
        res = ai.judge_and_write(item["title"], item["text"][:TEXT_LIMIT], system, recent_card_titles(),
                                 item["origin"] == "collection", hint)
    except Exception as e:
        log.warning("AI failed for %s: %s", item["title"][:40], e)
        with db.tx() as c:
            c.execute("UPDATE items SET attempts=attempts+1,"
                      " status=CASE WHEN attempts>=2 THEN 'failed' ELSE status END WHERE id=?", (item["id"],))
        return
    apply_result(item["id"], res, set(topics))


def process(ids: list[int] | None = None, limit: int = MAX_PER_RUN) -> int:
    if not has_api_key():
        log.info("no API key: cards are written in a Claude session (see UPDATE.md)")
        return 0
    if ids is None:
        # Fresh feed items first, then hand-picked Collection items, then backfill.
        todo = db.rows("SELECT id, title, text, origin, topic_hint FROM items WHERE status='ready'"
                       " ORDER BY CASE origin WHEN 'feed' THEN 0 WHEN 'collection' THEN 1 ELSE 2 END,"
                       " published DESC LIMIT ?", (limit,))
    else:
        marks = ",".join("?" * len(ids)) or "NULL"
        todo = db.rows(f"SELECT id, title, text, origin, topic_hint FROM items WHERE status='ready' AND id IN ({marks})", ids)
    topics = active_topics()
    if not topics:
        return 0
    system, by_id = ai.brief(topics, taste_examples()), {t["id"]: t for t in topics}
    # Collecting and carding are separate steps: articles pile up in the queue as they are found, good or bad.
    # Here each batch is first screened by title together (repeats and junk dropped, one article per story),
    # then the rest are read in full and carded. The next batch is screened against the cards just written.
    for n in range(0, len(todo), SCREEN_BATCH):
        batch = _screen(todo[n:n + SCREEN_BATCH], by_id)
        with ThreadPoolExecutor(AI_WORKERS) as pool:
            list(pool.map(lambda it: _process_one(it, system, by_id), batch))
    return len(todo)


def _screen(items: list[dict], topics: dict[int, dict]) -> list[dict]:
    """Drop repeats and junk by title before the full read. Returns the items still worth reading."""
    picked = [it for it in items if it["origin"] != "collection"]   # hand-picked items always get a full read
    try:
        drops = ai.screen_titles(picked, recent_card_titles(), topics) if picked else []
    except Exception as e:
        log.warning("title screening failed, carding the batch unscreened: %s", e)
        return items
    gone = {d["id"]: d["reason"] for d in drops if d["id"] in {it["id"] for it in picked}}
    with db.tx() as c:
        for i, reason in gone.items():
            c.execute("UPDATE items SET status='rejected', reason=? WHERE id=? AND status='ready'", ("筛选：" + reason, i))
    log.info("screened %d titles, dropped %d", len(items), len(gone))
    return [it for it in items if it["id"] not in gone]


def prune() -> None:
    cutoff = db.now() - RAW_TEXT_KEEP_DAYS * 86400
    stale = db.now() - STALE_NEWS_DAYS * 86400
    with db.tx() as c:
        c.execute("UPDATE items SET text='', summary='' WHERE fetched<? AND origin!='collection' AND text!=''", (cutoff,))
        # Feed items nobody got to in time are old news; Collection and backfill items stay queued.
        c.execute("UPDATE items SET status='expired' WHERE status IN ('new','ready') AND origin='feed'"
                  " AND fetched < ?", (stale,))


def backfill_topic(topic_id: int, github: bool = True) -> int:
    """Fill a new news topic straight away: the AI collector searches and reads for it now, and HN and GitHub are
    searched for its English keywords. github=False for topics that are not about software."""
    from . import collector
    t = db.one("SELECT * FROM topics WHERE id=?", (topic_id,))
    if not t or t["kind"] != "news":
        return 0
    if has_api_key() and sources.browser_running():
        db.set_meta(f"collected_{topic_id}", db.now())
        collector.collect_topic(t)
    found = []
    for kw in json.loads(t["keywords"] or "[]")[:6]:
        if not kw.isascii():
            continue  # HN and GitHub search work best with English keywords
        for fn in (sources.search_hn, sources.search_github) if github else (sources.search_hn,):
            try:
                found += fn(kw)
            except Exception as e:
                log.warning("backfill search failed %s %s: %s", fn.__name__, kw, e)
    ids = _insert_items(found, None, "backfill", topic_id)
    log.info("backfill topic %s: %d found, %d new", t["name"], len(found), len(ids))
    fill_text(ids)
    # Cards for this topic's queue, the collector's articles included (no-op without an API key).
    process([r["id"] for r in db.rows("SELECT id FROM items WHERE topic_hint=? AND status='ready'", (topic_id,))])
    return len(ids)


def run_once(force: bool = False) -> bool:
    if not _run_lock.acquire(blocking=False):
        return False
    try:
        db.set_meta("pipeline_running", True)
        fetch_sources(force)
        from . import collector   # imported here: the collector uses this module
        collector.collect()
        fill_text()
        process()
        prune()
        db.set_meta("last_run", db.now())
        return True
    finally:
        db.set_meta("pipeline_running", False)
        _run_lock.release()


def stats() -> dict:
    s = {r["status"]: r["n"] for r in db.rows("SELECT status, COUNT(*) n FROM items GROUP BY status")}
    s["cards"] = db.one("SELECT COUNT(*) n FROM cards")["n"]
    s["unseen_cards"] = db.one("SELECT COUNT(*) n FROM cards WHERE shown_count=0")["n"]
    return s


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    seed.apply()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_once(force="--force" in sys.argv)
    elif cmd == "backfill":
        backfill_topic(int(sys.argv[2]))
    elif cmd == "fetch":
        fetch_sources(force=True)
        fill_text()
    print(json.dumps(stats(), ensure_ascii=False))


if __name__ == "__main__":
    main()
