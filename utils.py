# utils.py
import re
import urllib.parse
from typing import Optional

def format_bytes(num_bytes) -> str:
    """Converts raw integer bytes into a clean, human-readable string format."""
    if not isinstance(num_bytes, (int, float)):
        return str(num_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"


def clean_dragged_path(raw: str) -> str:
    """
    Cleans and normalizes file paths dragged-and-dropped or typed 
    manually into the terminal (handling escaping and wrapping quotes).
    """
    if not raw:
        return ""
    text = raw.strip()
    # Strip enclosing quotes if present
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    # Repair escaped spaces common in some shells
    text = text.replace("\\ ", " ")
    return text


def slugify_filename(idx: int, title: str) -> str:
    """
    Sanitizes a title to make it safe for filesystems and 
    prepends a 1-based index selection number.
    """
    # Strip characters invalid on Windows/Unix filesystems
    clean_title = re.sub(r'[\\/*?:"<>|]', "", title)
    # Replace whitespace clusters with single underscores
    clean_title = re.sub(r'\s+', "_", clean_title).strip("_")
    base_name = clean_title if clean_title else "album_output"
    return f"{idx:02d}_{base_name}"


def extract_expiry_from_url(url_str: Optional[str]) -> Optional[int]:
    """
    Parses a URL query string to locate and extract the 'ex' expiration timestamp.
    """
    if not url_str:
        return None
    try:
        parsed = urllib.parse.urlparse(url_str)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        if 'ex' in query:
            return int(query['ex'])
    except Exception:
        pass
    return None