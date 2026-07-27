import re
import time
import urllib.parse


def format_bytes(num_bytes) -> str:
    """Original byte formatter."""
    if not isinstance(num_bytes, (int, float)): return str(num_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0: return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"

def clean_dragged_path(raw: str) -> str:
    """Original path cleaner."""
    if not raw: return ""
    text = raw.strip().replace("\\ ", " ")
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text

def slugify_filename(idx: int, title: str) -> str:
    """Standard slugifier for filenames."""
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', title)
    clean = re.sub(r'\s+', "_", clean).strip("_")
    return f"{idx:02d}_{clean if clean else 'output'}"

def get_album_folder_name(album_id, album_title: str) -> str:
    """Restored: The specific #ID_Title folder naming logic."""
    clean = re.sub(r'[\\/*?:"<>|]', "", album_title or "unknown_album")
    clean = re.sub(r'\s+', "_", clean).strip("_") or "unknown_album"
    return f"#{album_id}_{clean}"

def extract_expiry_from_url(url_str: str | None) -> int | None:
    if not url_str: return None
    try:
        parsed = urllib.parse.urlparse(url_str)
        q = dict(urllib.parse.parse_qsl(parsed.query))
        return int(q['ex']) if 'ex' in q else None
    except Exception:
        return None

def parse_and_check_expiry(expiry: int | None) -> str:
    """Restored: The detailed colored expiry status from read.py."""
    if not expiry: return "[dim white]No token found[/dim white]"
    current = int(time.time())
    if current > expiry: return "[bold red]Expired ❌[/bold red]"
    rem = expiry - current
    hours = rem // 3600
    mins = (rem % 3600) // 60
    if hours > 0:
        return f"[bold green]Valid ({hours}h {mins}m left) ✅[/bold green]"
    return f"[bold yellow]Valid ({mins}m left) ⚠️[/bold yellow]"

def parse_selection(spec: str, total_items: int) -> list[int]:
    """Restored: The advanced selection parser (1,3,5-10)."""
    if not spec or spec.strip().lower() == "all":
        return list(range(1, total_items + 1))

    selected = set()
    for chunk in (c.strip() for c in spec.split(",") if c.strip()):
        if "-" in chunk:
            try:
                start_str, end_str = chunk.split("-", 1)
                start, end = int(start_str), int(end_str)
                if start > end:
                    start, end = end, start
                selected.update(range(start, end + 1))
            except ValueError as e:
                raise ValueError(f"Invalid range format: '{chunk}'") from e
        else:
            try:
                selected.add(int(chunk))
            except ValueError as e:
                raise ValueError(f"Invalid numeric index: '{chunk}'") from e

    valid = [i for i in selected if 1 <= i <= total_items]
    if not valid and spec.strip():
        raise ValueError(
            f"Selection '{spec}' out of range (1-{total_items})"
        )

    return sorted(valid)

def sanitize_filename_simple(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in "._- ()").strip()