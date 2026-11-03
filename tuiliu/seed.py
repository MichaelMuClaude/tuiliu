"""Starting themes, topics and sources, taken from what the user collects in Collection.
Only applied to an empty database; after that the user edits everything in the app."""
import json

from . import db

THEMES = [
    ("开源与自托管工具", "好用的开源软件、能在自家服务器/NAS 上部署的服务、实用小工具", [
        ("自托管与 NAS 服务", "能在家里服务器或 NAS 上用 Docker 部署的开源服务",
         ["self-hosted", "selfhosted", "homelab", "NAS docker"]),
        ("实用开源小工具", "桌面、手机、命令行上解决具体问题的开源工具",
         ["open source tool", "open-source app", "CLI tool"]),
    ]),
    ("出海·独立开发·副业", "独立开发者和小团队的产品、收入案例、一人公司的打法", [
        ("独立开发者收入案例", "个人或小团队做产品赚钱的真实案例和数字",
         ["indie hacker", "MRR", "bootstrapped", "solo founder"]),
        ("一人公司与小生意", "小而美的生意模式、获客和定价思路",
         ["one-person business", "side project revenue", "micro SaaS"]),
    ]),
    ("AI Agent 与编程", "AI agent、Claude Code、MCP、Skill 和 AI 编程的新进展", [
        ("Claude Code / MCP / Skill", "Claude Code 的用法、MCP 服务器、agent skill",
         ["Claude Code", "MCP server", "agent skills", "Anthropic"]),
        ("AI agent 与新模型", "新发布的模型、agent 框架和真正好用的 AI 工具",
         ["AI agent", "LLM release", "coding agent"]),
    ]),
    ("AI 创作与文档工具", "AI 图像/视频生成、OCR、PDF 和文档处理", [
        ("ComfyUI 与 AI 视频图像", "开源图像/视频生成模型和工作流",
         ["ComfyUI", "video generation model", "image generation open source"]),
        ("OCR 与文档处理", "PDF 转 Markdown、OCR、文档解析工具",
         ["OCR", "PDF to markdown", "document parsing"]),
    ]),
]

SOURCES = [
    ("hn", "hn:front_page", "Hacker News 首页"),
    ("hn", "hn:show_hn", "Show HN"),
    ("rss", "https://selfh.st/rss/", "selfh.st"),
    ("rss", "https://www.reddit.com/r/selfhosted/top/.rss?t=week", "r/selfhosted"),
    ("rss", "https://www.ruanyifeng.com/blog/atom.xml", "科技爱好者周刊"),
    ("rss", "https://hellogithub.com/rss", "HelloGitHub"),
    ("rss", "https://sspai.com/feed", "少数派"),
    ("rss", "https://www.qbitai.com/feed", "量子位"),
    ("rss", "https://simonwillison.net/atom/everything/", "Simon Willison"),
    ("rss", "https://www.producthunt.com/feed", "Product Hunt"),
    ("rss", "https://mshibanami.github.io/GitHubTrendingRSS/daily/all.xml", "GitHub Trending"),
    ("collection", "collection:local", "Collection"),
]


def apply() -> None:
    if db.one("SELECT id FROM themes LIMIT 1"):
        return
    with db.tx() as c:
        for name, desc, topics in THEMES:
            cur = c.execute("INSERT INTO themes(name, description, created) VALUES(?,?,?)",
                            (name, desc, db.now()))
            for tname, tdesc, kws in topics:
                c.execute("INSERT INTO topics(theme_id, name, description, keywords, created) VALUES(?,?,?,?,?)",
                          (cur.lastrowid, tname, tdesc, json.dumps(kws, ensure_ascii=False), db.now()))
        for kind, url, name in SOURCES:
            c.execute("INSERT OR IGNORE INTO sources(kind, url, name) VALUES(?,?,?)", (kind, url, name))
