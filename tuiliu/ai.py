"""Claude calls: judge an item and write its cards, and turn a topic description into search keywords."""
import json
import logging
import time

import anthropic

from .config import MODEL

log = logging.getLogger("tuiliu.ai")
_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4)
    return _client


CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "takeaway": {"type": "string"},
    },
    "required": ["title", "points", "takeaway"],
    "additionalProperties": False,
}

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "score": {"type": "integer"},
        "topic_id": {"type": "integer"},
        "evergreen": {"type": "boolean"},
        "reason": {"type": "string"},
        "cards": {"type": "array", "items": CARD_SCHEMA},
    },
    "required": ["keep", "score", "topic_id", "evergreen", "reason", "cards"],
    "additionalProperties": False,
}

TOPIC_SCHEMA = {
    "type": "object",
    "properties": {
        "theme": {"type": "string"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["theme", "name", "description", "keywords"],
    "additionalProperties": False,
}

ITEM_RULES = """你是「推流」的编辑。推流是一个只给一位读者用的私人信息流：像刷短视频一样上下滑，但每一张卡片都必须是干货。你的工作是判断一篇内容值不值得推，值得的话把它写成卡片。

## 判断（keep / score）
- score 0-10：信息密度、可信度、和读者话题的相关度、对读者有没有实际用处。
- 以下情况 keep=false：标题党、软文广告、纯情绪或口水、空洞的通稿、招聘、活动预告、正文太少看不出内容、和所有话题都无关。
- score 低于 6 的一律 keep=false。宁缺毋滥：读者宁可少刷一张，也不要刷到垃圾。
- 投资、公司类话题：只写事实（数字、合同、财报、事件）和有出处的观点，观点要写明是谁说的（某分析师、某基金认为），不写「值得买」「该卖」这类建议；荐股文、目标价喊单只提取其中的事实，没有事实就 keep=false。
- 「谈资」类话题（话题描述里有谈资、闲聊、本地新鲜事）换一把尺子：不要求能照做，看的是有没有意思、身边的人会不会聊、能不能让读者跟上周围正在发生的事。本地趣闻、奇怪的研究发现、生活常识、大家都在讨论的新闻，只要讲得清楚就值得推；同一件事只推写得最好的一篇。犯罪细节、庭审过程、赛马赛况流水账、纯政治口水仍然不推。
- 「打折、薅羊毛」类话题：优惠信息本身就是干货，不当软文处理。只推真正划算的（明显低于平时价、免费、限时福利），写清价格、原价或折扣、怎么拿（优惠码、门店、条件）、截止时间和限制（限州、限会员、需订阅）；只对某个州有效的，写明州名。贵价奢侈品、普通的小折扣、需要先花大钱才能返现的，一律 keep=false。evergreen 一律 false。
- topic_id：选最贴切的一个话题 id；keep=false 时如果都不相关就填 0。
- evergreen：一年后读仍然有价值（工具介绍、方法、案例、知识）为 true；时效性新闻为 false。
- reason：一句话说明判断理由。

## 写卡片（只在 keep=true 时写，否则 cards 为空数组）
- 用简体中文写，英文内容翻译成自然的中文，专有名词保留英文。
- 一张卡片要完整讲完一件事，读者不需要再看原文。正文合计 150-250 个汉字。
- title：不超过 28 个字，直接说清楚是什么，不当标题党，不用感叹号。
- points：2-4 句。依次讲：发生了什么 / 核心观点；关键数字或事实；怎么用或怎么做到的。每句都要有具体信息，不说空话。
- takeaway：一句话，说这件事对读者意味着什么、可以怎么用（不超过 40 字）。
- 不要提来源、作者、公众号、网站名，也不要写「本文」「作者认为」。读者只要干货。
- 文章里没有证实的说法（某公司号称、自称、预计），要写成「据称」「号称」，不能写成事实。
- 只有内容确实一张讲不完（例如一篇长文里有几个互相独立的工具）才写 2-3 张，每张独立成立；否则只写 1 张。
"""


def brief(topics: list[dict], taste: list[str]) -> str:
    """The rules, today's date, the reader's topics and taste. The same text guides the API and session subagents."""
    return (ITEM_RULES
            + f"\n今天是 {time.strftime('%Y-%m-%d %A')}。活动类内容只写还没过去的场次，并写清日期。\n"
            + "\n## 读者的话题\n"
            + "\n".join(f"- id={t['id']}：{t['theme']} / {t['name']}：{t['description']}" for t in topics)
            + "\n\n## 读者收藏或点过喜欢的内容（代表他的口味）\n"
            + ("\n".join(f"- {t}" for t in taste) or "（暂无）"))


def _call(system: str, user: str, schema: dict, max_tokens: int = 4000) -> dict | None:
    resp = client().messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        # The brief is the same for every article in a run, so cache it.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        thinking={"type": "disabled"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    )
    u = resp.usage
    log.info("tokens: %d in, %d cache read, %d cache write, %d out", u.input_tokens,
             u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0, u.output_tokens)
    if resp.stop_reason in ("refusal", "max_tokens"):
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)


def judge_and_write(title: str, text: str, system: str, recent: list[str], hand_picked: bool) -> dict | None:
    """system comes from brief(); recent lists titles of cards already written, so repeats are rejected."""
    note = "这篇是读者自己亲手收藏的文章，默认值得推，除非内容实在太空。\n\n" if hand_picked else ""
    seen = ("## 最近已经写过的卡片\n如果这篇讲的是下面某张卡片已经讲过的同一件事，又没有重要的新信息，就 keep=false，"
            "reason 写「重复」。\n" + "\n".join(f"- {t}" for t in recent) + "\n\n") if recent else ""
    user = f"{seen}{note}## 这篇内容\n标题：{title}\n\n正文：\n{text}"
    return _call(system, user, ITEM_SCHEMA)


def expand_topic(description: str, themes: list[str]) -> dict | None:
    system = ("把读者用一句话描述的兴趣，整理成推流里的一个话题。"
              "theme：从已有主题里选最合适的一个，原样返回它的名字；都不合适就新起一个简短的中文主题名。"
              "name：话题名，简短中文，不超过 14 个字。description：一句话说明这个话题要推什么。"
              "keywords：4-6 个用于在 Hacker News 和 GitHub 搜索的英文关键词或短语，再加 1-2 个中文关键词。"
              "\n已有主题：" + "、".join(themes))
    return _call(system, description, TOPIC_SCHEMA, max_tokens=1000)
