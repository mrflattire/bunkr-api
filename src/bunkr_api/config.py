from pathlib import Path

VERSION = "0.1.0-beta.1"

APP_DIR = Path.home() / ".bunkr_api"
APP_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(APP_DIR / "media_tracker.db")

LOG_DIR = APP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "bunkr_downloads"
DEFAULT_JSON_DIR = DEFAULT_OUTPUT_DIR / "jsons"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://bunkr.cr",
    "Referer": "https://bunkr.cr/",
}

SEARCH_MODES = {"broad": "broad", "strict": "strict", "fuzzy": "fuzzy", "substring": "substring", "whole": "whole"}
SORT_TYPES = {"latest": "latest", "oldest": "oldest", "most files": "mostfiles"}
TOP_CATEGORIES = {"albums": "topalbums", "videos": "topvideos", "files": "topfiles", "images": "topimages"}
VALID_COUNTS = [20, 40, 60, 100]

