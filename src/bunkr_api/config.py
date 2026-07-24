import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent.parent
DB_PATH = "media_tracker.db"
DEFAULT_OUTPUT_DIR = Path.home() / "Downloads" / "bunkr_downloads"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://bunkr.cr",
    "Referer": "https://bunkr.cr/",
}

SEARCH_MODES = {"broad": "broad", "strict": "strict", "fuzzy": "fuzzy", "substring": "substring", "whole": "whole"}
SORT_TYPES = {"latest": "latest", "oldest": "oldest", "most files": "mostfiles"}
TOP_CATEGORIES = {"albums": "topalbums", "videos": "topvideos", "files": "topfiles", "images": "topimages"}
VALID_COUNTS = [20, 40, 60, 100]