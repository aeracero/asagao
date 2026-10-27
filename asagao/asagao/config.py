"""環境変数から読み込む設定と、各種の上限値。"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: Path = Path(".env")) -> None:
    """ローカル開発用: .env があれば読み込む（依存ライブラリなし）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# --- Discord ---------------------------------------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0") or 0) or None
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "1") != "0"

# --- Web -------------------------------------------------------------------
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
REDIRECT_URI = f"{PUBLIC_URL}/callback"
COOKIE_SECURE = PUBLIC_URL.startswith("https://")
SESSION_DAYS = 30

# --- Storage ---------------------------------------------------------------
# Railway で Volume をつなぐと RAILWAY_VOLUME_MOUNT_PATH が自動で入る
DATA_DIR = Path(
    os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or "./data"
).resolve()

# --- Time ------------------------------------------------------------------
TZ = ZoneInfo(os.getenv("ALARM_TZ", "Asia/Tokyo"))

# --- Audio -----------------------------------------------------------------
VOLUME_LEVELS = (20, 40, 60, 80, 100)
ALLOWED_EXTS = {".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".flac", ".webm"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_SOUND_SECONDS = 180
MAX_SOUNDS_PER_USER = 5
UPLOADS_PER_HOUR = 10

# --- Alarms ----------------------------------------------------------------
MAX_ALARMS_PER_USER = 50
PREP_LEAD = 60          # アラーム何秒前から準備を始めるか（内部の固定値）
CATCHUP_WINDOW = 300    # 再起動などで逃したアラームを、何秒後まで鳴らし直すか
MAINTENANCE_HOUR = 15   # 掃除・バックアップを行う時刻（朝を避ける）


def missing_env() -> list[str]:
    pairs = (
        ("DISCORD_TOKEN", DISCORD_TOKEN),
        ("DISCORD_CLIENT_ID", CLIENT_ID),
        ("DISCORD_CLIENT_SECRET", CLIENT_SECRET),
    )
    return [name for name, value in pairs if not value]
