# core.py
import sqlite3
import time
import os
import urllib.parse
from contextlib import closing
from pathlib import Path
from typing import List, Optional, Dict


class DatabaseManager:
    def __init__(self, db_path: str = "media_tracker.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self):
        """Initializes tables, indexes, and default system configurations."""
        with closing(self._get_connection()) as conn:
            # WAL mode: lets downloader.py / streamer.py / auto_minter.py run as
            # independent processes against the same DB without "database is locked".
            conn.execute("PRAGMA journal_mode = WAL;")

            with conn:
                # 1. ALBUMS Table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS albums (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        album_id_str TEXT UNIQUE,
                        title TEXT NOT NULL,
                        search_term TEXT,
                        global_index INTEGER,
                        aggregate_size INTEGER DEFAULT 0,
                        file_count INTEGER DEFAULT 0,
                        local_target_dir TEXT,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                """)

                # 2. ASSETS Table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS assets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        album_id INTEGER NOT NULL,
                        track_number INTEGER,
                        title TEXT NOT NULL,
                        original_filename TEXT,
                        raw_size_bytes INTEGER DEFAULT 0,
                        source_url TEXT UNIQUE,
                        signed_cdn_url TEXT,
                        token_expiry_timestamp INTEGER,
                        download_status TEXT CHECK(
                            download_status IN ('PENDING', 'DOWNLOADING', 'COMPLETED', 'FAILED')
                        ) DEFAULT 'PENDING',
                        local_file_path TEXT,
                        error_message TEXT,
                        FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE
                    );
                """)

                # 3. SYSTEM CONFIG Table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS system_config (
                        config_key TEXT PRIMARY KEY,
                        config_value TEXT NOT NULL,
                        description TEXT
                    );
                """)

                # Indexes — these are what make "instantly locate" actually instant.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_assets_expiry
                    ON assets(token_expiry_timestamp);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_assets_download_status
                    ON assets(download_status);
                """)

                # Seed Defaults
                conn.execute("""
                    INSERT OR IGNORE INTO system_config (config_key, config_value, description)
                    VALUES 
                        ('default_player', 'mpv', 'Primary media player fallback engine'),
                        ('max_workers', '4', 'Default concurrency worker thread limit'),
                        ('token_buffer_seconds', '600', 'Force token refresh if token expires within this window (10 min lookahead)'),
                        ('minter_poll_interval_seconds', '60', 'Polling loop interval for the background minter daemon')
                """)

    # --- CONFIG RETRIEVAL ---

    def get_config_val(self, key: str, default: str) -> str:
        with closing(self._get_connection()) as conn:
            row = conn.execute(
                "SELECT config_value FROM system_config WHERE config_key = ?;", (key,)
            ).fetchone()
            return row["config_value"] if row else default

    # --- DATABASE CORE API ---

    def register_album_from_json(self, data: dict) -> int:
        """Imports or syncs a classic JSON payload directly into SQLite."""
        album_meta = data.get("selected_album", {})
        search_term = data.get("search_term", "N/A")
        files_found = data.get("files_found", [])

        album_title = album_meta.get("title", "Unknown Album")
        global_idx = album_meta.get("album_index_number", 0)
        album_id_str = f"{album_title}_{global_idx}".lower().replace(" ", "_")
        now = int(time.time())

        with closing(self._get_connection()) as conn:
            with conn:
                cursor = conn.execute("""
                    INSERT INTO albums (album_id_str, title, search_term, global_index, aggregate_size, file_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(album_id_str) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        aggregate_size = excluded.aggregate_size,
                        file_count = excluded.file_count
                    RETURNING id;
                """, (
                    album_id_str, album_title, search_term, global_idx,
                    album_meta.get("aggregate_size", 0), len(files_found), now, now
                ))
                album_id = cursor.fetchone()["id"]

                for idx, file_rec in enumerate(files_found, start=1):
                    cdn_url = file_rec.get("signed_cdn_url")
                    expiry_ts = extract_expiry_from_url(cdn_url)

                    conn.execute("""
                        INSERT INTO assets (
                            album_id, track_number, title, original_filename, 
                            raw_size_bytes, source_url, signed_cdn_url, token_expiry_timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_url) DO UPDATE SET
                            signed_cdn_url = excluded.signed_cdn_url,
                            token_expiry_timestamp = excluded.token_expiry_timestamp
                    """, (
                        album_id, idx,
                        file_rec.get("title", f"Track {idx}"),
                        file_rec.get("original", f"track_{idx}.mp3"),
                        file_rec.get("size", 0),
                        file_rec.get("href", cdn_url),
                        cdn_url, expiry_ts
                    ))

            return album_id

    def get_all_albums(self) -> List[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            return conn.execute("SELECT * FROM albums ORDER BY updated_at DESC;").fetchall()

    def get_album_assets(self, album_id: int) -> List[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            return conn.execute(
                "SELECT * FROM assets WHERE album_id = ? ORDER BY track_number ASC;", (album_id,)
            ).fetchall()

    def update_asset_url(self, asset_id: int, new_cdn_url: str):
        expiry_ts = extract_expiry_from_url(new_cdn_url)
        with closing(self._get_connection()) as conn:
            with conn:
                conn.execute(
                    "UPDATE assets SET signed_cdn_url = ?, token_expiry_timestamp = ? WHERE id = ?",
                    (new_cdn_url, expiry_ts, asset_id)
                )

    def update_download_status(self, asset_id: int, status: str, local_path: Optional[str] = None, error: Optional[str] = None):
        with closing(self._get_connection()) as conn:
            with conn:
                conn.execute(
                    "UPDATE assets SET download_status = ?, local_file_path = ?, error_message = ? WHERE id = ?",
                    (status, local_path, error, asset_id)
                )

    # --- THE HYBRID MINTER CORE API ---

    def get_needs_refresh(self) -> List[sqlite3.Row]:
        """
        Retrieves assets whose signatures expire within our lookahead window.
        Backed by idx_assets_expiry, so this stays fast as the library grows.
        """
        lookahead = int(self.get_config_val("token_buffer_seconds", "600"))
        now_with_buffer = int(time.time()) + lookahead

        with closing(self._get_connection()) as conn:
            cursor = conn.execute("""
                SELECT * FROM assets 
                WHERE token_expiry_timestamp IS NULL 
                   OR token_expiry_timestamp <= ?;
            """, (now_with_buffer,))
            return cursor.fetchall()

    def get_valid_url(self, asset_id: int) -> str:
        """
        The Synchronous Escape Hatch.
        Pulls cached token if valid; otherwise, blocks momentarily to 
        mint, write to database, and return a working live link.
        """
        lookahead = int(self.get_config_val("token_buffer_seconds", "600"))
        now_with_buffer = int(time.time()) + lookahead

        with closing(self._get_connection()) as conn:
            asset = conn.execute("SELECT * FROM assets WHERE id = ?;", (asset_id,)).fetchone()

        if not asset:
            raise ValueError(f"Asset with ID {asset_id} does not exist.")

        # If token is still fresh, skip the expensive network call and return from cache
        if asset["signed_cdn_url"] and asset["token_expiry_timestamp"] and asset["token_expiry_timestamp"] > now_with_buffer:
            return asset["signed_cdn_url"]

        # Escape hatch activated: Sync-mint now!
        print(f"[Escape Hatch] Synchronous refresh triggered for Asset #{asset_id} ('{asset['title']}')")
        fresh_url = mint_now(asset["source_url"])
        self.update_asset_url(asset_id, fresh_url)
        return fresh_url


# --- Shared Helpers & Utilities ---

def format_bytes(num_bytes) -> str:
    if not isinstance(num_bytes, (int, float)):
        return str(num_bytes)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} PB"

def extract_expiry_from_url(url_str: Optional[str]) -> Optional[int]:
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

def clean_dragged_path(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    text = text.replace("\\ ", " ")
    return text

def mint_now(source_url: str) -> str:
    """
    Hook your real token minting logic here.
    This receives the static source fallback url (href) and contacts 
    your web scraper/token generator to retrieve a fresh CDN stream endpoint.
    """
    # TODO: Integrate your real custom web automation/minter code here
    # For now, it mimics a successful signature refresh for demonstration:
    parsed = urllib.parse.urlparse(source_url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    # Extend expiry timestamp out +2 hours from current moment
    query['ex'] = str(int(time.time()) + 7200)

    new_query = urllib.parse.urlencode(query)
    fresh_url = urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, parsed.path, 
        parsed.params, new_query, parsed.fragment
    ))
    return fresh_url