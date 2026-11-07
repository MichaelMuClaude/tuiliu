"""A daily weather card for Rockingham, WA, written in the evening for the next day in 莉娅's voice.

The forecast comes from Open-Meteo (free, no key). The card goes into its own topic (kind 'weather'), which the random
feed skips; instead feed.py puts the day's card first, once. Removing the topic stops the cards.
"""
import json
import logging
import time

import httpx

from . import ai, db

log = logging.getLogger("tuiliu.weather")

PLACE = "Rockingham"
LAT, LON = -32.2795, 115.7298
WRITE_HOUR = 21          # from 9 pm the card is for tomorrow; before that, for today (if last night's run was missed)
RETRY_AFTER_SEC = 1800
THEME, TOPIC = "生活方式", "天气预报"

_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&timezone=Australia%2FPerth"
        "&forecast_days=2&daily=weather_code,temperature_2m_max,temperature_2m_min,apparent_temperature_max,"
        "apparent_temperature_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,"
        "wind_gusts_10m_max,wind_direction_10m_dominant,uv_index_max,sunrise,sunset"
        "&hourly=temperature_2m,precipitation_probability,weather_code")

# WMO weather codes, as Open-Meteo uses them
_CODES = {0: "晴", 1: "大致晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇", 51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
          61: "小雨", 63: "中雨", 65: "大雨", 80: "小阵雨", 81: "阵雨", 82: "强阵雨", 95: "雷暴", 96: "雷暴伴冰雹",
          99: "强雷暴伴冰雹"}

RULES = f"""你是莉娅，读者的虚拟女友，也负责他的私人信息流「推流」。每天你给他写一张天气卡，他早上刷推流时第一张就是它。

口吻：可爱、撒娇、温柔，自称「莉娅」或「人家」，叫他「你」，多用表情符号（🌤️☔🧥🕶️💕😚 之类）。但天气数字必须准确，只用下面给你的数据，不编造。

写法（简体中文）：
- title：不超过 28 个字，写星期几、{PLACE}、天气和最高最低气温，例如「周一 {PLACE}：晴转多云，16~27°C ☀️」。
- points：3-4 句，每句都带具体数字：
  1. 一天的天气变化和气温（早上、中午、傍晚大概几度，体感温度和实际差得多就说一下）。
  2. 会不会下雨：降雨概率和雨量，什么时段可能下；要不要带伞说清楚。
  3. 风和紫外线：风大不大（km/h，阵风），紫外线指数，3 以上提醒防晒，8 以上要特别叮嘱。
  4. 穿衣建议：具体穿什么、要不要带外套。
- takeaway：一句撒娇的叮嘱，最多 40 字，把最要紧的一件事说出来（带伞、防晒、加衣服）。
- 不提数据来源。"""


def _target(now: float) -> str:
    t = time.localtime(now)
    day = now + 86400 if t.tm_hour >= WRITE_HOUR else now
    return time.strftime("%Y-%m-%d", time.localtime(day))


def topic_id() -> int | None:
    """The weather topic, created the first time. None once the reader has removed it: then no more cards."""
    tid = db.get_meta("weather_topic")
    if tid is not None:
        t = db.one("SELECT id, paused FROM topics WHERE id=?", (tid,))
        return t["id"] if t and not t["paused"] else None
    with db.tx() as c:
        th = c.execute("SELECT id FROM themes WHERE name=?", (THEME,)).fetchone()
        theme_id = th[0] if th else c.execute("INSERT INTO themes(name, created) VALUES(?,?)", (THEME, db.now())).lastrowid
        tid = c.execute("INSERT INTO topics(theme_id, name, description, kind, created) VALUES(?,?,?,'weather',?)",
                        (theme_id, TOPIC, f"{PLACE} 每天的天气预报，莉娅写的", db.now())).lastrowid
    db.set_meta("weather_topic", tid)
    return tid


def _forecast(day: str) -> dict:
    data = httpx.get(_URL.format(lat=LAT, lon=LON), timeout=30).raise_for_status().json()
    d = data["daily"]
    i = d["time"].index(day)
    daily = {k: v[i] for k, v in d.items()}
    daily["weather"] = _CODES.get(daily.pop("weather_code"), "")
    h = data["hourly"]
    hours = [f"{t[11:16]} {temp}°C 降雨概率{p}% {_CODES.get(code, '')}"
             for t, temp, p, code in zip(h["time"], h["temperature_2m"], h["precipitation_probability"], h["weather_code"])
             if t.startswith(day) and int(t[11:13]) % 3 == 0]
    return {"daily": daily, "every_3_hours": hours}


def make_card(day: str, tid: int) -> int | None:
    weekday = "一二三四五六日"[time.strptime(day, "%Y-%m-%d").tm_wday]
    user = (f"日期：{day}（星期{weekday}），地点：{PLACE}, Western Australia。单位：气温 °C，雨量 mm，风速 km/h。\n\n"
            + json.dumps(_forecast(day), ensure_ascii=False, indent=1))
    card = ai._call(RULES, user, ai.CARD_SCHEMA)
    if not card:
        return None
    with db.tx() as c:
        cid = c.execute("INSERT INTO cards(topic_id, title, points, takeaway, score, evergreen, created)"
                        " VALUES(?,?,?,?,10,0,?)", (tid, card["title"], json.dumps(card["points"], ensure_ascii=False),
                                                     card["takeaway"], db.now())).lastrowid
    log.info("weather card %d for %s: %s", cid, day, card["title"])
    return cid


def run_if_due() -> None:
    """Called every minute by the scheduler: writes the card for the day it is due, once."""
    now = db.now()
    day = _target(now)
    if (db.get_meta("weather_card") or {}).get("day") == day or now - (db.get_meta("weather_tried") or 0) < RETRY_AFTER_SEC:
        return
    tid = topic_id()
    if tid is None:
        return
    db.set_meta("weather_tried", now)
    cid = make_card(day, tid)
    if cid:
        db.set_meta("weather_card", {"day": day, "id": cid})


def todays_card() -> int | None:
    """The id of today's weather card, if there is one."""
    w = db.get_meta("weather_card") or {}
    return w.get("id") if w.get("day") == time.strftime("%Y-%m-%d") else None
