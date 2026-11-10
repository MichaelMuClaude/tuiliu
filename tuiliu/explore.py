"""📌 on a card: turn it into a card on 探索清单 (the boredom-cards app on the same machine), dated when it should come up.

Haiku rewrites the card as one line for a to-do list, starting with the day it should appear (a few days before an
event, earlier when it needs booking). 探索清单 reads that date out of the text itself, so the line is all it needs.
"""
import datetime
import json
import logging
import os

import httpx

from . import ai, db
from .config import MODEL

log = logging.getLogger("tuiliu.explore")

EXPLORE_URL = os.environ.get("TUILIU_EXPLORE_URL", "http://127.0.0.1:8091")

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}

RULES = """读者在信息流里看到一张卡片，想把它放进自己的「探索清单」：一个随手翻的小卡片清单，记着想去的地方、想做的事、要办的小事。你把这张卡片改写成清单上的一句话。

写法（简体中文，一句话，最多 110 个字）：
- 如果这件事有日子（活动、展览截止、节日、要提前预约的），句子开头写清单该在哪天提醒他，格式只用「M月D日 」（例如「11月28日 」），后面不写年份。
  - 一天的活动：提前 2-3 天提醒，让他有时间安排；需要订票、报名、约人的提前 1-3 周。
  - 要订机票、长途车票或出远门的：提前 1 个月以上；离现在已经不到 1 个月，就写今天的日期。
  - 有截止日的展览、优惠：挑截止前一个周六，或者离今天最近的合适日子。
  - 提醒日不能早于今天，也不能晚于活动当天。还没开始的展览、活动，提醒日不能早于开展日：挑开展后的第一个周六。
- 没有具体日子的（一个随时能做的点子、一个地方），开头不写日期，句子里也不要出现任何日期：清单会把句子里第一个日期当成提醒日。所以只要句子里写了日期（比如「展到12月20日」），开头就必须先写提醒日。「周末」「周六」「明天」「下周」这类词也会被当成日期，没有日子的句子里用「找个空闲的时候」。
- 活动今年已经过去了（日期早于今天）：不要写今年的日期；每年都有的，开头写明年活动前两周左右的日子，并说明要先查明年的具体日期。
- 不要用「每天」「每周」「每月」开头：清单会把它当成重复提醒。
- 后面直接写这件事：做什么、哪天（活动当天的日期和星期一定要写）、在哪、几点、多少钱、要注意什么（截止日、要预约、免费）。只用卡片里有的信息，不编造。卡片里有几个选择的，挑最方便的一个写，别的可以一笔带过。
- 不要解释这件事有没有日子、为什么挑这天，也不要写「正合适」「值得一去」这类空话。
- 卡片不是活动（新闻、知识、技术文章、工具）的，也照样写成一件他能做的小事，比如「找时间试试……」「下次……时用上……」「读一读……」。读者点了就是想记下来，不要评论这张卡片适不适合放进清单。
- 读者没有车：提到交通只写火车、公交、步行，不要写开车、停车。
- 不提来源，不加表情符号。

例子（今天是 11月10日周二）：
- 展览展到 12月20日 → 「11月28日 去 PICA 看 Shannon Lyons 回顾展「Cafe coming soon」（免费，展到12月20日），坐火车到 Perth 站走几分钟」
- 12月5日周六的游行 → 「12月2日 12月5日周六晚上市中心 St Georges Terrace 有免费的圣诞大游行，坐火车去，早点到占位」
- 一条公司新闻 → 「读一读英伟达对黄仁勋传闻的独立调查，看看结果会不会影响 AI 算力采购」（开头直接写要做的事）"""


def add(card_id: int, today: str) -> dict:
    """Make the 探索清单 card for a feed card once; returns {id, due?, text, already}."""
    card = db.one("SELECT title, points, takeaway FROM cards WHERE id=?", (card_id,))
    if not card:
        raise LookupError("card not found")
    done = db.get_meta(f"explore:{card_id}")
    if done:
        return {**done, "already": True}
    try:
        day = datetime.date.fromisoformat(today)
    except ValueError:
        day = datetime.date.today()
    points = "\n".join(f"- {p}" for p in json.loads(card["points"]))
    user = (f"今天是 {day.isoformat()}（{'一二三四五六日'[day.weekday()]}）。\n\n## 卡片\n{card['title']}\n{points}\n"
            f"💡 {card['takeaway']}")
    resp = ai.client().messages.create(
        model=MODEL, max_tokens=1000, system=RULES, messages=[{"role": "user", "content": user}],
        thinking={"type": "disabled"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError("AI 没写出来")
    text = json.loads(next(b.text for b in resp.content if b.type == "text"))["text"].strip()
    if text.startswith("每"):      # 探索清单 would make it a repeating card ("每周六…"); one reminder is what was asked
        text = "提醒：" + text
    r = httpx.post(f"{EXPLORE_URL}/api/ideas", json={"text": text, "today": day.isoformat()}, timeout=20)
    r.raise_for_status()
    made = {**r.json(), "text": text}
    db.set_meta(f"explore:{card_id}", made)
    log.info("explore card %s from card %d: %s", made.get("id"), card_id, text)
    return {**made, "already": False}
