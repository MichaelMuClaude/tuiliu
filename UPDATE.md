# 更新推流 — procedure for a Claude session

Since 2026-11-06 the server has an API key and writes the cards for queued articles itself every 45 minutes (Haiku 5.5, see README). A session is still needed for what the server cannot do: setting up new topics and their feeds, web searches for weekend events and holidays, the gallery pages (a real browser), series of cards, and checking the cards the server wrote (duplicates, misfiled topics). New topics and series can also be set up by the user in the app's chat (swipe left), without a session. When the user says **"更新推流"**, do steps 1, 2 and 5 (spot-check the newest cards); steps 3 and 4 are only needed if the queue is long, the key has expired (`work status` shows hundreds of `ready` items), or there is no key. All commands run on the Surface (`ssh michael-surface-pro-7`, where the live data is) in `~/github/tuiliu` with `.venv/bin/python -m tuiliu.work ...`.

## 1. Refresh the queue

```bash
cd ~/github/tuiliu && .venv/bin/python -m tuiliu.pipeline fetch
.venv/bin/python -m tuiliu.work status
```

## 2. Set up new topics (if any)

`work topics` lists topics the user added in the app (held under the theme 新话题, keywords empty). For each one:

- choose a theme (an existing one, or a short new Chinese name), a short Chinese name (14 characters at most), a one-sentence description, and 4-6 English search keywords plus 1-2 Chinese ones;
- `work set-topic ID --theme ... --name ... --description ... --keywords '["...", ...]'`
- `work backfill ID` searches Hacker News and GitHub, so the topic has articles queued straight away. Keywords go into the GitHub search as written, so a qualifier such as `created:>2026-09-01 stars:>1500` works (good for "hot right now" topics). To write only these articles now, add `--origin backfill` to `work queue` and `work batches`.
- For a topic that is not about software (a company, a market), run `work backfill ID --no-github`; GitHub search only returns noise there. For a listed company, Yahoo Finance's per-ticker RSS (`https://feeds.finance.yahoo.com/rss/2.0/headline?s=TICKER&region=US&lang=en-US`) and the company's own blog feed work; Google News RSS links are redirects that cannot be read. Paywalled sites are useless too: thewest.com.au only returns its subscription page (feed 16 is paused), while WAtoday gives full text.
- Topics that HN and GitHub cannot serve (local news, everyday 谈资) need feeds instead: test the RSS URL with curl, then `work add-source URL --name NAME --topic ID`, so its cards are filed under that topic. Any language is fine; cards are always written in Chinese (user, 2026-11-03: sources can come from anywhere in the world). `work sources` lists feeds; `work enable ID ... --off` pauses feeds whose topics were deleted.
- Topics no feed covers (weekend events, a home-repair question) get pages from a web search: `work add-url --topic ID URL ...` fetches each page and queues it under that topic. Topics that go stale need this on every update: for 珀斯周末活动 (id 10), search for what's on in Perth this coming weekend and queue 3-5 listing pages. The brief tells the subagents today's date, so they only write events that are still ahead.
- 最近的节假日 (id 15) has no feed: write its cards yourself (`work cards`, with `"evergreen": false` on each card) from the official WA public holiday page (wa.gov.au, "Public holidays in Western Australia") plus checked dates for Chinese and global festivals. Top it up when the latest card's date is less than two months away.
- 打折和薅羊毛 (id 16) reads OzBargain's front page and its WA tag; AI 数据中心 (id 18) reads Data Center Knowledge, DCD and ServeTheHome. ITEM_RULES has a deals rule, so deals are not rejected as ads.
- 网络安全 (id 23) reads Krebs, BleepingComputer, ACSC alerts, the Microsoft Security Blog and The Hacker News; the same incident shows up in several of them, so reject the duplicates by title in step 3 and keep the most detailed one. The user is a security engineer: keep these cards technical.
- 珀斯免费展览 (id 24): the gallery pages (artgallery.wa.gov.au, visit.museum.wa.gov.au, slwa.wa.gov.au) build their lists with JavaScript, so `add-url` gets almost nothing. Read them in the built-in browser and write the cards yourself (`work cards`, `"evergreen": false`, with end dates and prices). Refresh when exhibitions close.
- Plain keywords sorted by stars bring up long-famous repos (TensorFlow, freeCodeCamp) and trackers of trending lists; screen those out in step 3.

## 3. Screen by title (cheap)

`work queue` prints `id, origin, title`. Read the titles yourself and drop the obvious misses with `work reject "标题判断：<reason>" ID ...`: clickbait, ads, hiring posts, event notices, and anything unrelated to every topic. Do not reject `collection` items (the user picked them by hand). When in doubt, keep it; the full read decides.

## 4. Write the cards with Haiku subagents

```bash
.venv/bin/python -m tuiliu.work batches <scratchpad>/tuiliu-<date> --limit 120 --size 12
```

This writes `brief.md` (rules, topics, taste) and `batch-NN.json`. Start one subagent per batch (Agent tool, `model: "haiku"`, several in parallel), with this prompt:

> Read `<dir>/brief.md` and follow it exactly. Then read `<dir>/batch-NN.json`, judge every item in it, and write the cards. Write the results as one JSON array to `<dir>/result-NN.json` with the Write tool, one object per item, in the format brief.md describes. Do nothing else and do not run any commands. Reply with one line: how many items you kept and how many you rejected.

`--limit` sets how many articles one update handles; raise it when the queue is long and the user wants more cards.

## 5. Import and check

```bash
.venv/bin/python -m tuiliu.work import <dir>/result-*.json
```

Spot-check three or four new cards (`sqlite3 data/tuiliu.db "select title, points from cards order by id desc limit 4"`): Chinese, self-contained, no source names, unconfirmed claims written as 据称. If one batch is bad, fix or delete its cards rather than importing more of the same.

## Series written from scratch

When the user asks for a series of cards on a subject rather than news (e.g. "100 张寒暄技巧"), there is no source article: `work add-topic --kind series --theme ... --name ... --description ... --keywords '[...]'` (series topics get no articles from feeds), write the cards yourself as JSON arrays of `{title, points, takeaway}` (same card rules as brief.md; outline the whole series first so the cards don't repeat), then `work cards --topic ID FILE.json ...`. Check any law or number before writing it into a card. A card may carry an `"image"` (a `/static/...` path); road sign pictures live in `static/signs/` with their Commons sources and licences in `static/signs/SOURCES.md`. Commons' API rejects Python's httpx (403), so fetch from it with curl, slowly (it rate-limits bursts). These cards are evergreen and are mixed into the feed with everything else.

## 6. Report

Tell the user how many cards were added, how many articles were rejected, how many are still queued, and which new topics were set up.
