"""The news collector: an AI that uses the browser like a person. For each news topic it searches Google (news and
web) with the topic's keywords, opens the results that look worth reading, and saves the articles it read into the
queue. Writing cards from the queue is a separate step (pipeline.process), with its own screening for repeats.
"""
import json
import logging
import time

from . import ai, browser_tools, db, pipeline, sources
from .config import MODEL

log = logging.getLogger("tuiliu.collector")
COLLECT_EVERY_HOURS = 6   # each news topic gets a collecting round this often
MAX_STEPS = 30            # API calls per topic round
MAX_SAVED = 8             # articles saved per topic round

RULES = """你是「推流」的新闻采集员。推流是一位读者的私人信息流，读者住在珀斯（西澳）。你用一个真实的 Chrome 浏览器上网，像人一样找新闻：

1. 用话题的关键词搜索：先用 google_search 做普通的 Google 搜索（recent 设成 week，只看最近一周的网页），结果不够新、不够多时再用 google_news 搜新闻，或者换个说法再搜。
2. 看搜索结果，挑出值得读的：一件具体的新事（新闻、优惠、活动、发布、研究），符合这个话题（包括地区），不是广告、招聘、直播页、列表页。
3. 用 browser_open 打开读一读。确认是完整、具体、免费能读到的文章（不是付费墙、不是只有一两句），就用 save_article 保存。需要时可以点「展开全文」「下一页」、滚动（browser_click、browser_scroll、browser_look），保存的是你最后读到的页面。
4. 和「已经有的」里讲同一件事的，不要再存；同一件事多家报道，只存最详细的一篇。
5. 存够 {max_saved} 篇，或者找不到更多值得存的，就停下，用一句话说你存了什么。

如果 Google 要求验证码（工具会报 captcha），立刻停下。

""" + browser_tools.SAFETY

TOOLS = browser_tools.definitions(["google_search", "google_news", "browser_open", "browser_look", "browser_click",
                                   "browser_scroll"]) + [
    {"name": "save_article", "description": "保存一篇已经读过的文章（用 browser_open 或 browser_look 返回的网址）。",
     "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"],
                      "additionalProperties": False}},
]


class _Round:
    """One topic's collecting round: the browser (its pages read so far) and the titles saved."""

    def __init__(self, topic: dict):
        self.topic, self.browser, self.saved = topic, browser_tools.Browser(), []

    def tool_result(self, block_id: str, name: str, a: dict) -> dict:
        if name != "save_article":
            return self.browser.tool_result(block_id, name, a)
        page = self.browser.pages.get(a["url"])
        if not page:
            out = {"error": "先用 browser_open 读这篇，再保存"}
        elif len(self.saved) >= MAX_SAVED:
            out = {"error": f"这一轮已经存了 {MAX_SAVED} 篇，停下"}
        elif not pipeline._insert_items([{"url": page["url"], "title": page["title"], "text": page["text"],
                                          "summary": "", "published": time.time()}], None, "feed", self.topic["id"]):
            out = {"error": "队列里已经有这篇（同一网址或同一标题）"}
        else:
            self.saved.append(page["title"])
            out = {"ok": True, "saved": len(self.saved)}
        return {"type": "tool_result", "tool_use_id": block_id, "content": json.dumps(out, ensure_ascii=False),
                "is_error": "error" in out}


def collect_topic(topic: dict) -> int:
    """Let the AI collect articles for one news topic. Returns how many were saved."""
    known = [r["title"] for r in db.rows(
        "SELECT title FROM cards WHERE topic_id=? ORDER BY created DESC LIMIT 30", (topic["id"],))]
    known += [r["title"] for r in db.rows(
        "SELECT title FROM items WHERE topic_hint=? AND status IN ('new','ready') ORDER BY id DESC LIMIT 30",
        (topic["id"],))]
    keywords = json.loads(topic["keywords"] or "[]")
    task = (f"今天是 {time.strftime('%Y-%m-%d %A')}。\n话题：{topic['name']}：{topic['description']}\n"
            f"关键词：{'、'.join(keywords) or '（没有，按话题自己想）'}\n\n已经有的（不要重复）：\n"
            + ("\n".join(f"- {t}" for t in known) or "（暂无）"))
    messages = [{"role": "user", "content": task}]
    rnd = _Round(topic)
    try:
        cost_in, cost_out = _run(rnd, messages)
    finally:
        rnd.browser.release()
    if rnd.browser.blocked:
        db.set_meta("google_blocked", db.now())
    log.info("collected %-16s %d saved, about $%.4f", topic["name"], len(rnd.saved),
             cost_in * 0.10 / 1e6 + cost_out * 0.50 / 1e6)
    return len(rnd.saved)


def _run(rnd: _Round, messages: list) -> tuple[float, int]:
    """The agent loop. Returns the input tokens (weighted as billed) and output tokens it used."""
    cost_in = cost_out = 0
    for _ in range(MAX_STEPS):
        resp = ai.client().messages.create(
            model=MODEL, max_tokens=4000, system=RULES.format(max_saved=MAX_SAVED), tools=TOOLS,
            messages=messages, output_config={"effort": "low"}, cache_control={"type": "ephemeral"})
        cost_in += resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0) * 0.1 \
            + (resp.usage.cache_creation_input_tokens or 0) * 1.25
        cost_out += resp.usage.output_tokens
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        messages.append({"role": "user", "content": [rnd.tool_result(b.id, b.name, b.input)
                                                     for b in resp.content if b.type == "tool_use"]})
        if rnd.browser.blocked:
            break
    return cost_in, cost_out


def collect(force: bool = False) -> int:
    """A collecting round for every news topic that is due. Skipped while Google wants a CAPTCHA solved
    (checked again once an hour) or ai-browser is down."""
    if not sources.browser_running():
        log.info("ai-browser is not running: no collecting this time")
        return 0
    blocked_at = db.get_meta("google_blocked", 0) or 0
    if blocked_at and db.now() - blocked_at < 3600 and not force:
        return 0
    db.set_meta("google_blocked", 0)
    total = 0
    for t in pipeline.active_topics():
        key = f"collected_{t['id']}"
        if not force and db.now() - (db.get_meta(key, 0) or 0) < COLLECT_EVERY_HOURS * 3600:
            continue
        db.set_meta(key, db.now())
        try:
            total += collect_topic(t)
        except Exception as e:
            log.warning("collecting failed for %s: %s", t["name"], e)
        if db.get_meta("google_blocked", 0):
            break
    return total
