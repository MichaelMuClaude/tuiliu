# 推流 (tuiliu)

A private phone web app for scrolling news, topics and knowledge the way you scroll a short-video app (swipe up and down), but every card is worth reading. It replaces WeChat Official Accounts and Xiaohongshu.

## Design decisions (agreed 2026-11-03)

- **Infinite scroll, no junk.** Never runs out: fresh items first, then evergreen items. Junk is filtered out before it ever reaches the pool.
- **One card = one complete item.** AI writes a self-contained card (about 150-250 Chinese characters) so you never need to open the original. Only the substance: no source label on the card. The original URL is kept in the database for checking, never shown. Unconfirmed claims are written as claims ("据称").
- **Prepare ahead, don't fetch live.** A background job every 45 minutes: fetch sources -> store raw text locally -> dedupe -> queue. Cards are written ahead of time (see "API key" below). Swiping only reads ready-made cards (no AI call), so it is instant. Unprocessed news expires after 10 days; Collection and topic backfill items stay queued.
- **API key (since 2026-11-06).** From 2026-11-03 the server only fetched and queued, and a Claude session wrote the cards ("更新推流", see `UPDATE.md`). Since 2026-11-06 `~/.config/tuiliu/env` holds a key paid by the Max plan's monthly API credits (it expires every 30 days), so every run writes the queued cards itself with Haiku 5.5 (`tuiliu/ai.py`): the same brief the session subagents get, the brief cached, and the titles of the last three days' cards sent along so repeats are rejected. About 9k input tokens per article, well under a cent. Without the key it falls back to the session workflow. No WeChat or Xiaohongshu reading.
- **Two kinds of topic.** `news` topics are fed by sources and are time-sensitive (网络安全, 珀斯周末活动); articles are only filed under news topics. `series` topics are a one-off set of cards written in a session (交朋友, 人间词话): no feed, no backfill, cards evergreen. `work add-topic --kind series`, `work set-kind ID news|series`.
- **Gestures (2026-11-03):** one card at a time. Swipe up = next card (no going back). Swipe down = the topic list, where topics are only added (one sentence) or deleted; swipe up on its bottom bar to return. No like / dislike buttons (the user found them pointless). Double tap = favourite (收藏) the card, with a heart; double tap again to take it back. The topic sheet has two tabs, 话题 and 收藏; favourites are listed in full there. A favourite is stored as `cards.reaction = 1`, nudges its topic's weight up, and feeds the taste examples used when writing cards.
- **Anti-boredom:** rotate topics, about 15% exploration, and learn from dwell time only (long stay raises a topic's weight, an instant skip lowers it a little).
- **Custom themes and topics.** Two levels: a theme (主题, broad, e.g. "open-source tools") groups topics (话题, specific, e.g. "self-hosted music players"). In the app you add a topic by describing it in plain words, or delete one; the next "更新推流" session files it under a theme and turns it into search keywords. A theme disappears when its last topic is deleted. (The API still supports weights and pausing; the app no longer shows them.) A new topic is backfilled straight away by searching sources (Hacker News search, GitHub search, Reddit search and so on), so its cards appear within minutes instead of waiting for the feeds. The topics seen in Collection are only the starting set.
- **Storage:** raw articles kept about 30 days; cards kept for good (no repeats, slowly builds a knowledge base).

## Inputs

- `~/pCloudDrive/Collection/` (read only): about 265 hand-collected articles. Used as the taste sample, the first batch of evergreen cards, and a live input (new collections become cards).
- Starting topics (from Collection; the user edits them in the app): open-source / self-hosted tools, going global (出海) / indie development / side businesses / one-person companies, AI agents / Claude Code / MCP, ComfyUI / AI video / OCR.
- Upstream sources to try: GitHub Trending, Hacker News (Show HN), Starter Story, Indie Hackers, r/selfhosted, selfh.st, plus good RSS feeds.

## Code layout

- `tuiliu/sources.py` fetchers (RSS, Hacker News, GitHub search, Collection) and full-text extraction
- `tuiliu/pipeline.py` fetch -> full text -> dedupe -> `ai.judge_and_write` -> cards; `python -m tuiliu.pipeline run|fetch|backfill <topic id>`
- `tuiliu/ai.py` Haiku prompts and JSON schemas; `tuiliu/feed.py` ranking, topic rotation, feedback weights
- `tuiliu/server.py` FastAPI app on 127.0.0.1:8095, runs the pipeline every 45 minutes; `static/` the swipe page
- Secrets: `~/.config/tuiliu/env` with `ANTHROPIC_API_KEY=...`. Data: `data/tuiliu.db` (not in git)

```bash
cd ~/github/tuiliu && uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m tuiliu.server
```

## Where it runs

1. **Prototype on CoreMint** in this folder: Python + SQLite + a full-screen scroll-snap web front end (PWA). Live since 2026-11-03: user service `~/.config/systemd/user/tuiliu.service` (127.0.0.1:8095), published with `tailscale serve --bg --https=8443 http://127.0.0.1:8095` at **the tailnet HTTPS address on port 8443** (tailnet only; turn off with `tailscale serve --https=8443 off`). Restart after code changes: `systemctl --user restart tuiliu`.
2. **Then move to the Surface Pro 7** (`~/github/tuiliu/`, systemd user service on 127.0.0.1, published with `tailscale serve` on a new port). The Surface has no pCloud, so CoreMint must sync new Collection Markdown files to it (rsync).
