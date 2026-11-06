"""Hand-off between the server and a Claude session, which does the AI part ("更新推流"). See UPDATE.md.

    python -m tuiliu.work status
    python -m tuiliu.work queue [--limit N]                 # id, origin, title of queued items, for screening
    python -m tuiliu.work reject "reason" ID [ID ...]       # drop items by title alone
    python -m tuiliu.work requeue ID [ID ...]               # give rejected items another pass
    python -m tuiliu.work batches OUT_DIR [--limit N] [--size 12]
    python -m tuiliu.work import RESULT.json [...]          # judgements + cards written by the session
    python -m tuiliu.work topics                            # topics waiting to be set up
    python -m tuiliu.work set-topic ID --theme NAME --name NAME --description TEXT --keywords JSON
    python -m tuiliu.work backfill ID                       # search HN/GitHub for a topic (no AI)
    python -m tuiliu.work add-url --topic ID URL [URL ...]  # queue pages found by web search
    python -m tuiliu.work add-topic --theme NAME --name NAME --description TEXT [--keywords JSON] [--kind series]
    python -m tuiliu.work set-kind ID news|series                # news: fed by sources; series: written once
    python -m tuiliu.work cards --topic ID FILE.json [...]  # cards the session wrote itself, no source article
    python -m tuiliu.work sources                           # list feeds
    python -m tuiliu.work add-source URL --name NAME [--topic ID]
    python -m tuiliu.work enable ID [ID ...] [--off]
"""
import argparse
import html
import json
import re
from pathlib import Path

from . import ai, db, pipeline, seed, sources

PENDING_THEME = "新话题"

_ORDER = (" ORDER BY CASE origin WHEN 'feed' THEN 0 WHEN 'collection' THEN 1 ELSE 2 END,"
          " published DESC")


def _queued(limit: int, origin: str | None = None) -> list[dict]:
    where = " AND origin=?" if origin else ""
    args = (origin, limit) if origin else (limit,)
    return db.rows("SELECT id, origin, title, text FROM items WHERE status='ready'" + where + _ORDER + " LIMIT ?", args)


def cmd_status(_):
    print(json.dumps(pipeline.stats(), ensure_ascii=False))
    print("pending topics:", len(_pending_topics()))


def cmd_queue(a):
    for r in _queued(a.limit, a.origin):
        print(f"{r['id']}\t{r['origin']}\t{r['title']}")


def cmd_reject(a):
    with db.tx() as c:
        for i in a.ids:
            c.execute("UPDATE items SET status='rejected', reason=? WHERE id=? AND status='ready'", (a.reason, i))
    print(f"rejected {len(a.ids)}")


def cmd_requeue(a):
    """Give rejected items another pass, e.g. after the rules change."""
    with db.tx() as c:
        for i in a.ids:
            c.execute("UPDATE items SET status='ready', reason='' WHERE id=? AND status='rejected' AND text!=''", (i,))
    print(f"requeued {len(a.ids)}")


def cmd_batches(a):
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    topics = pipeline.active_topics()
    brief = (ai.brief(topics, pipeline.taste_examples())
             + "\n\n## 输出\n对 batch 文件里的每一篇写一个 JSON 对象，全部放进一个 JSON 数组，写到对应的 result 文件。"
               "每个对象的字段：id（原样抄 batch 里的 id）、keep、score、topic_id、evergreen、reason、cards"
               "（cards 是数组，每张有 title、points、takeaway）。points 必须是字符串数组，每句一个元素，"
               "例如 [\"第一句。\", \"第二句。\"]，不要把几句拼成一个字符串。"
               "origin 为 collection 的是读者亲手收藏的，默认值得推。\n")
    (out / "brief.md").write_text(brief)
    items = _queued(a.limit, a.origin)
    files = []
    for n in range(0, len(items), a.size):
        chunk = [{"id": r["id"], "origin": r["origin"], "title": r["title"], "text": r["text"][:pipeline.TEXT_LIMIT]}
                 for r in items[n:n + a.size]]
        f = out / f"batch-{n // a.size + 1:02d}.json"
        f.write_text(json.dumps(chunk, ensure_ascii=False, indent=1))
        files.append(f)
    print(f"{len(items)} items in {len(files)} batches; rules in {out / 'brief.md'}")
    for f in files:
        print(f)


def cmd_import(a):
    valid = {t["id"] for t in db.rows("SELECT id FROM topics")}
    counts = {"carded": 0, "rejected": 0, "skipped": 0}
    for path in a.files:
        for res in json.loads(Path(path).read_text()):
            item = db.one("SELECT id, status FROM items WHERE id=?", (res.get("id"),))
            if not item or item["status"] != "ready":
                counts["skipped"] += 1
                continue
            counts[pipeline.apply_result(item["id"], res, valid)] += 1
    print(json.dumps(counts), json.dumps(pipeline.stats(), ensure_ascii=False))


def _pending_topics() -> list[dict]:
    return db.rows("SELECT t.id, t.name, t.description, th.name AS theme FROM topics t JOIN themes th ON th.id=t.theme_id"
                   " WHERE t.kind='news' AND (t.keywords='[]' OR th.name=?)", (PENDING_THEME,))


def cmd_topics(_):
    themes = [r["name"] for r in db.rows("SELECT name FROM themes WHERE name!=?", (PENDING_THEME,))]
    print("existing themes:", "、".join(themes))
    for t in _pending_topics():
        print(f"{t['id']}\t[{t['theme']}] {t['name']}\t{t['description']}")


def cmd_set_topic(a):
    with db.tx() as c:
        th = c.execute("SELECT id FROM themes WHERE name=?", (a.theme,)).fetchone()
        theme_id = th["id"] if th else c.execute("INSERT INTO themes(name, created) VALUES(?,?)",
                                                 (a.theme, db.now())).lastrowid
        c.execute("UPDATE topics SET theme_id=?, name=?, description=?, keywords=? WHERE id=?",
                  (theme_id, a.name, a.description, json.dumps(json.loads(a.keywords), ensure_ascii=False), a.id))
        # Remove the holding theme once it is empty.
        c.execute("DELETE FROM themes WHERE name=? AND id NOT IN (SELECT theme_id FROM topics)", (PENDING_THEME,))
    print(db.one("SELECT * FROM topics WHERE id=?", (a.id,)))


def cmd_add_topic(a):
    with db.tx() as c:
        th = c.execute("SELECT id FROM themes WHERE name=?", (a.theme,)).fetchone()
        theme_id = th["id"] if th else c.execute("INSERT INTO themes(name, created) VALUES(?,?)",
                                                 (a.theme, db.now())).lastrowid
        tid = c.execute("INSERT INTO topics(theme_id, name, description, keywords, kind, created) VALUES(?,?,?,?,?,?)",
                        (theme_id, a.name, a.description, a.keywords, a.kind, db.now())).lastrowid
    print(db.one("SELECT * FROM topics WHERE id=?", (tid,)))


def cmd_set_kind(a):
    with db.tx() as c:
        c.execute("UPDATE topics SET kind=? WHERE id=?", (a.kind, a.id))
    print(db.one("SELECT id, name, kind FROM topics WHERE id=?", (a.id,)))


def cmd_cards(a):
    """A series of cards written from the session's own knowledge (e.g. 100 small-talk tips)."""
    n = sum(pipeline.add_written_cards(a.topic, json.loads(Path(path).read_text())) for path in a.files)
    print(f"added {n} cards", json.dumps(pipeline.stats(), ensure_ascii=False))


def cmd_sources(_):
    for s in db.rows("SELECT s.id, s.enabled, s.name, s.url, t.name AS topic FROM sources s"
                     " LEFT JOIN topics t ON t.id = s.topic_id ORDER BY s.id"):
        print(f"{s['id']}\t{'on ' if s['enabled'] else 'off'}\t{s['name']}\t{s['topic'] or ''}\t{s['url']}")


def cmd_add_source(a):
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO sources(kind, url, name, topic_id) VALUES('rss', ?, ?, ?)",
                  (a.url, a.name, a.topic))
    print(db.one("SELECT * FROM sources WHERE url=?", (a.url,)))


def cmd_enable(a):
    with db.tx() as c:
        for i in a.ids:
            c.execute("UPDATE sources SET enabled=? WHERE id=?", (int(a.on), i))
    cmd_sources(a)


def cmd_backfill(a):
    print("new items:", pipeline.backfill_topic(a.id, github=a.github))


def cmd_add_url(a):
    """Queue pages the session found itself (web search), for topics no feed covers."""
    found = [{"url": u, "title": u} for u in a.urls]
    for f in found:
        try:
            m = re.search(r"<title[^>]*>(.*?)</title>", sources._http.get(f["url"]).text, re.S | re.I)
            f["title"] = html.unescape(m.group(1)).strip() if m else f["url"]
        except Exception as e:
            print("title fetch failed", f["url"], e)
    ids = pipeline._insert_items(found, None, "backfill", a.topic)
    pipeline.fill_text(ids)
    for r in db.rows(f"SELECT id, status, length(text) n, title FROM items WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids):
        print(f"{r['id']}\t{r['status']}\t{r['n']}\t{r['title']}")


def main() -> None:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init()
    seed.apply()
    p = argparse.ArgumentParser(prog="tuiliu.work")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("queue"); s.add_argument("--limit", type=int, default=500)
    s.add_argument("--origin", choices=["feed", "collection", "backfill"]); s.set_defaults(fn=cmd_queue)
    s = sub.add_parser("reject"); s.add_argument("reason"); s.add_argument("ids", type=int, nargs="+"); s.set_defaults(fn=cmd_reject)
    s = sub.add_parser("requeue"); s.add_argument("ids", type=int, nargs="+"); s.set_defaults(fn=cmd_requeue)
    s = sub.add_parser("batches"); s.add_argument("out_dir"); s.add_argument("--limit", type=int, default=120)
    s.add_argument("--size", type=int, default=12)
    s.add_argument("--origin", choices=["feed", "collection", "backfill"]); s.set_defaults(fn=cmd_batches)
    s = sub.add_parser("import"); s.add_argument("files", nargs="+"); s.set_defaults(fn=cmd_import)
    sub.add_parser("topics").set_defaults(fn=cmd_topics)
    s = sub.add_parser("set-topic"); s.add_argument("id", type=int); s.add_argument("--theme", required=True)
    s.add_argument("--name", required=True); s.add_argument("--description", required=True)
    s.add_argument("--keywords", required=True); s.set_defaults(fn=cmd_set_topic)
    s = sub.add_parser("backfill"); s.add_argument("id", type=int)
    s.add_argument("--no-github", dest="github", action="store_false"); s.set_defaults(fn=cmd_backfill)
    s = sub.add_parser("add-url"); s.add_argument("--topic", type=int, required=True); s.add_argument("urls", nargs="+")
    s.set_defaults(fn=cmd_add_url)
    s = sub.add_parser("add-topic"); s.add_argument("--theme", required=True); s.add_argument("--name", required=True)
    s.add_argument("--description", required=True); s.add_argument("--keywords", default="[]")
    s.add_argument("--kind", choices=["news", "series"], default="news"); s.set_defaults(fn=cmd_add_topic)
    s = sub.add_parser("set-kind"); s.add_argument("id", type=int); s.add_argument("kind", choices=["news", "series"])
    s.set_defaults(fn=cmd_set_kind)
    s = sub.add_parser("cards"); s.add_argument("--topic", type=int, required=True); s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_cards)
    sub.add_parser("sources").set_defaults(fn=cmd_sources)
    s = sub.add_parser("add-source"); s.add_argument("url"); s.add_argument("--name", required=True)
    s.add_argument("--topic", type=int, help="file this feed's cards under that topic"); s.set_defaults(fn=cmd_add_source)
    s = sub.add_parser("enable"); s.add_argument("ids", type=int, nargs="+")
    s.add_argument("--off", dest="on", action="store_false"); s.set_defaults(fn=cmd_enable)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
