"""Fetchers. Each returns a list of dicts: url, title, summary, published, text (optional)."""
import calendar
import html
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
import trafilatura
import yaml

from .config import COLLECTION_DIR, USER_AGENT

MAX_TEXT = 15000
_http = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", s or ""))).strip()


def norm_title(title: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]", "", title.lower())


# ---------- feeds ----------

def fetch_rss(url: str) -> list[dict]:
    r = _http.get(url)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries[:40]:
        link = e.get("link")
        if not link or not e.get("title"):
            continue
        body = ""
        if e.get("content"):
            body = e.content[0].get("value", "")
        body = strip_html(body or e.get("summary", ""))
        ts = e.get("published_parsed") or e.get("updated_parsed")
        out.append({
            "url": link,
            "title": strip_html(e.title),
            "summary": body[:MAX_TEXT],
            "published": calendar.timegm(ts) if ts else time.time(),
        })
    return out


def _hn_hits(endpoint: str, params: str) -> list[dict]:
    r = _http.get(f"https://hn.algolia.com/api/v1/{endpoint}?{params}")
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        title = h.get("title")
        if not title:
            continue
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append({
            "url": h.get("url") or hn_url,
            "title": title,
            "summary": strip_html(h.get("story_text") or ""),
            "published": h.get("created_at_i") or time.time(),
        })
    return out


def fetch_hn(which: str) -> list[dict]:
    if which == "hn:show_hn":
        since = int(time.time()) - 3 * 86400
        return _hn_hits("search_by_date", f"tags=show_hn&hitsPerPage=40&numericFilters=points>40,created_at_i>{since}")
    return _hn_hits("search", "tags=front_page&hitsPerPage=40")


def search_hn(query: str, days: int = 180, min_points: int = 30) -> list[dict]:
    since = int(time.time()) - days * 86400
    return _hn_hits("search", f"query={quote_plus(query)}&tags=story&hitsPerPage=15"
                              f"&numericFilters=points>{min_points},created_at_i>{since}")


# ---------- ai-browser: Google and web pages in a real Chrome ----------
# Google blocks headless and fresh automated browsers at once, but answers ai-browser: a long-lived, visible Chrome
# (its own profile, which can be signed in to Google) driven over HTTP on 127.0.0.1:8888. The news collector and
# the chat use it like a person would. When Google asks for a CAPTCHA the user solves it over remote desktop.

AI_BROWSER = "http://127.0.0.1:8888/api"
BROWSER_LOCK = threading.Lock()   # one browser window, shared by the Google News pass and the chat: one user at a time

_GOOGLE_JS = """(() => {
  const out = [];
  for (const a of document.querySelectorAll('#search a[href]')) {
    const h = a.querySelector('[role="heading"]');
    if (!h || out.some(o => o.url === a.href)) continue;
    const lines = a.innerText.split('\\n').map(s => s.trim()).filter(Boolean);
    out.push({ url: a.href, title: h.innerText.trim(), snippet: lines.slice(2).join(' ').slice(0, 400) });
  }
  return JSON.stringify({ blocked: /unusual traffic|not a robot/i.test(document.body.innerText), results: out });
})()"""


class GoogleBlocked(Exception):
    """Google answered with its 'unusual traffic' CAPTCHA page."""


def browser_running() -> bool:
    try:
        return _http.get(f"{AI_BROWSER}/health", timeout=5).status_code == 200
    except Exception:
        return False


_GOOGLE_WEB_JS = """(() => {
  const out = [];
  for (const h of document.querySelectorAll('#search a h3')) {
    const a = h.closest('a');
    if (!a || out.some(o => o.url === a.href)) continue;
    const box = a.closest('[data-hveid]') || a.parentElement;
    const snippet = (box ? box.innerText : '').split('\\n').slice(2).join(' ').slice(0, 300);
    out.push({ url: a.href, title: h.innerText.trim(), snippet });
  }
  return JSON.stringify({ blocked: /unusual traffic|not a robot/i.test(document.body.innerText), results: out });
})()"""


def _google_page(url: str, script: str, query: str) -> list[dict]:
    """Open a Google results page in ai-browser and read its results (caller holds BROWSER_LOCK)."""
    _http.post(f"{AI_BROWSER}/navigate", json={"url": url, "return_look": False}, timeout=60).raise_for_status()
    time.sleep(3)   # let the results render
    r = _http.post(f"{AI_BROWSER}/execute_js", json={"script": script}, timeout=30)
    r.raise_for_status()
    page = json.loads(r.json()["result"])
    if page["blocked"]:
        raise GoogleBlocked(query)
    return page["results"]


def google_web(query: str) -> list[dict]:
    """An ordinary Google search in ai-browser (for the chat): [{title, url, snippet}]."""
    with BROWSER_LOCK:
        found = _google_page(f"https://www.google.com/search?q={quote_plus(query)}&hl=en", _GOOGLE_WEB_JS, query)
    return [f for f in found if "google." not in (urlparse(f["url"]).hostname or "")]


def browser_read(url: str) -> dict:
    """Open a page in ai-browser (a real Chrome, so pages built with JavaScript work) and return its main text."""
    with BROWSER_LOCK:
        nav = _http.post(f"{AI_BROWSER}/navigate", json={"url": url, "return_look": False}, timeout=60)
        nav.raise_for_status()
        time.sleep(2)
        text = _http.get(f"{AI_BROWSER}/text", params={"main_content_only": "true"}, timeout=30).json().get("text", "")
    n = nav.json()
    return {"title": n.get("title", ""), "url": n.get("url", url), "blocked": n.get("blocked", False), "text": text}


def google_news(query: str) -> list[dict]:
    """Google News results from the past two days for one query: [{title, url, snippet}]. The links are Google
    redirects; opening one in the browser lands on the article."""
    url = f"https://www.google.com/search?q={quote_plus(query)}&tbm=nws&hl=en&tbs=qdr:d2"
    with BROWSER_LOCK:
        return _google_page(url, _GOOGLE_JS, query)


def search_github(query: str, days: int = 365, min_stars: int = 200) -> list[dict]:
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    q = quote_plus(f"{query} pushed:>{since} stars:>{min_stars}")
    r = _http.get(f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=10",
                  headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    out = []
    for repo in r.json().get("items", []):
        out.append({
            "url": repo["html_url"],
            "title": f"{repo['full_name']}: {repo.get('description') or ''}".strip(": "),
            "summary": f"{repo.get('description') or ''} (stars: {repo['stargazers_count']}, language: {repo.get('language')})",
            "published": time.time(),
        })
    return out


def fetch_source(kind: str, url: str) -> list[dict]:
    if kind == "rss":
        return fetch_rss(url)
    if kind == "hn":
        return fetch_hn(url)
    if kind == "collection":
        return read_collection()
    return []


# ---------- full text ----------

def _github_readme(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    if len(parts) < 2:
        return ""
    owner, repo = parts[0], parts[1]
    for name in ("README.md", "readme.md", "README.MD", "README"):
        r = _http.get(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{name}")
        if r.status_code == 200:
            return r.text
    return ""


def fetch_text(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.endswith("reddit.com") or host.endswith("news.ycombinator.com"):
        return ""  # rate limited / no article body; the feed summary is used instead
    if host == "github.com":
        return _github_readme(url)[:MAX_TEXT]
    r = _http.get(url)
    if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
        return ""
    text = trafilatura.extract(r.text, include_comments=False, include_tables=True) or ""
    return text[:MAX_TEXT]


# ---------- Collection (read only) ----------

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def read_collection() -> list[dict]:
    out = []
    for path in sorted(Path(COLLECTION_DIR).glob("*.md")):
        if path.name == "CLAUDE.md":
            continue
        raw = path.read_text(errors="ignore")
        meta, body = {}, raw
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end > 0:
                try:
                    meta = yaml.safe_load(raw[3:end]) or {}
                except yaml.YAMLError:
                    meta = {}
                body = raw[end + 4:]
        body = _IMG_RE.sub("", body).strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})_", path.name)
        published = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%d")) if m else path.stat().st_mtime
        out.append({
            "url": str(meta.get("url") or f"collection:{path.name}"),
            "title": str(meta.get("title") or path.stem),
            "summary": "",
            "text": body[:MAX_TEXT],
            "published": published,
        })
    return out
