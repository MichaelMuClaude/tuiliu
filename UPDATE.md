# 更新推流 — procedure for a Claude session

There is no API key. The server only fetches and queues articles (every 45 minutes, no AI). Whenever the user says **"更新推流"** (or asks for new cards or topics), a Claude Code session does the AI part with the steps below. All commands run in `~/github/tuiliu` with `.venv/bin/python -m tuiliu.work ...`.

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

## 6. Report

Tell the user how many cards were added, how many articles were rejected, how many are still queued, and which new topics were set up.
