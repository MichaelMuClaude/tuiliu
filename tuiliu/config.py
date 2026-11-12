"""Paths and settings. Secrets come from ~/.config/tuiliu/env (KEY=value lines)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TUILIU_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "tuiliu.db"
STATIC_DIR = ROOT / "static"
CONFIG_DIR = Path.home() / ".config" / "tuiliu"
LIYA_CARD_DIR = Path(os.environ.get("TUILIU_LIYA", Path.home() / "Pictures" / "Liya"))  # 莉娅's pictures (her folder)
GLANCECARDS_URL = os.environ.get("TUILIU_GLANCECARDS", "http://127.0.0.1:8090")  # 词卡 server, for word cards
COLLECTION_DIR = Path(os.environ.get("TUILIU_COLLECTION", Path.home() / "pCloudDrive" / "Collection"))

MODEL = "claude-haiku-5-5"          # judges articles and writes their cards
HOST = "127.0.0.1"
PORT = int(os.environ.get("TUILIU_PORT", "8095"))
PIPELINE_INTERVAL_MIN = 45
SCHEDULER = os.environ.get("TUILIU_SCHEDULER", "1") == "1"  # 0 for UI tests on a scratch database
RAW_TEXT_KEEP_DAYS = 30
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) tuiliu/0.1"


def load_env() -> None:
    env_file = CONFIG_DIR / "env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


load_env()
DATA_DIR.mkdir(parents=True, exist_ok=True)
