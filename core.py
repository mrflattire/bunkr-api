# core.py
import sqlite3
import time
from contextlib import closing, contextmanager
from typing import List, Optional

# Decoupled utilities are imported from utils.py to maintain thin DB context
from utils import clean_dragged_path, slugify_filename, extract_expiry_from_url


class DatabaseManager:
    def __init__(self, db_path: str = "media_tracker.db"):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        # Adding timeout=5.0 configures PRAGMA busy_timeout = 5000 natively on initialization
        conn = sqlite3.connect(self.db_path, timeout=5.0) 
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    @contextmanager
    def connection(self):
        """
        Public, always-closing connection for external callers (download.py,
        stream.py, mint.py, etc). `with db.connection() as conn:` both
        commits/rolls back (like a bare `with conn:`) AND closes the
        connection on exit — a bare `with db._get_connection() as conn:`
        only does the former, leaking a connection/fd every call. Prefer
        this over touching _get_connection() directly from outside core.py.
        """
        conn = self._get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initializes tables, indexes, and default system configurations."""
        with closing(self._get_connection()) as conn:
            # WAL mode allows concurrent read/write across background processes
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
                # Added true_file_id INTEGER to capture the numeric ID required for handshakes
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS assets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        album_id INTEGER NOT NULL,
                        track_number INTEGER,
                        true_file_id INTEGER,
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

                # Performance Indexing
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_assets_expiry
                    ON assets(token_expiry_timestamp);
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_assets_download_status
                    ON assets(download_status);
                """)
                # SQLite does NOT auto-index FK columns — every album-scoped
                # query (get_album_assets, targeted mint refresh) was doing
                # a full table scan on assets despite the FK constraint.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_assets_album_id
                    ON assets(album_id);
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
        """Imports or syncs a legacy JSON payload directly into SQLite."""
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
                    
                    # Extract the correct numeric true_file_id safely
                    true_file_id = file_rec.get("true_file_id") or file_rec.get("slug_id")

                    conn.execute("""
                        INSERT INTO assets (
                            album_id, track_number, true_file_id, title, original_filename, 
                            raw_size_bytes, source_url, signed_cdn_url, token_expiry_timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_url) DO UPDATE SET
                            true_file_id = excluded.true_file_id,
                            signed_cdn_url = excluded.signed_cdn_url,
                            token_expiry_timestamp = excluded.token_expiry_timestamp
                    """, (
                        album_id, idx, true_file_id,
                        file_rec.get("title", f"Track {idx}"),
                        # No hardcoded .mp3 — these are mostly video files (m4v/mp4),
                        # and 'original' is now always populated with the real
                        # filename+extension from the source (see scrape.py fix).
                        # This fallback only fires if 'original' is genuinely missing.
                        file_rec.get("original", f"file_{idx}"),
                        file_rec.get("size", 0),
                        file_rec.get("href", cdn_url),
                        cdn_url, expiry_ts
                        # cdnEndpoint intentionally NOT stored — it's a raw path
                        # fragment (no host, no token), superseded by minting.
                        # Kept in the JSON payload (files_found) only, if
                        # --save-json is used; core.py never persists it to SQLite.
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

    def get_needs_refresh(self, album_id: Optional[int] = None) -> List[sqlite3.Row]:
        """
        Retrieves assets whose signatures expire within our lookahead window.
        Pass album_id to scope the query to one album at the SQL level —
        e.g. daemon_loop's targeted --album-id run — instead of fetching
        every stale asset across the whole DB and discarding most of it in
        Python. Backed by idx_assets_expiry (+ idx_assets_album_id when
        album_id is given), so this stays fast as the library grows.
        """
        lookahead = int(self.get_config_val("token_buffer_seconds", "600"))
        now_with_buffer = int(time.time()) + lookahead

        with closing(self._get_connection()) as conn:
            if album_id is not None:
                cursor = conn.execute("""
                    SELECT * FROM assets 
                    WHERE album_id = ?
                      AND (token_expiry_timestamp IS NULL 
                           OR token_expiry_timestamp <= ?);
                """, (album_id, now_with_buffer))
            else:
                cursor = conn.execute("""
                    SELECT * FROM assets 
                    WHERE token_expiry_timestamp IS NULL 
                       OR token_expiry_timestamp <= ?;
                """, (now_with_buffer,))
            return cursor.fetchall()

    def get_valid_url(self, asset_id: int) -> str:
        """The Synchronous Escape Hatch. Serves cached URLs or mints live as a fallback."""
        lookahead = int(self.get_config_val("token_buffer_seconds", "600"))
        now_with_buffer = int(time.time()) + lookahead

        with closing(self._get_connection()) as conn:
            asset = conn.execute("SELECT * FROM assets WHERE id = ?;", (asset_id,)).fetchone()

        if not asset:
            raise ValueError(f"Asset with ID {asset_id} does not exist.")

        # Cache Hit: return URL instantly without network calls
        if asset["signed_cdn_url"] and asset["token_expiry_timestamp"] and asset["token_expiry_timestamp"] > now_with_buffer:
            return asset["signed_cdn_url"]

        # Cache Miss: Trigger synchronous escape hatch refresh
        print(f"[Escape Hatch] Synchronous refresh triggered for Asset #{asset_id} ('{asset['title']}')")
        
        # Deferred dynamic import breaks the circular loop with mint.py at module compilation time
        from mint import mint_now
        
        # Pass the newly mapped true_file_id or fallback to source_url if somehow empty
        file_id_to_mint = str(asset["true_file_id"]) if asset["true_file_id"] is not None else asset["source_url"]
        fresh_url = mint_now(file_id_to_mint)
        self.update_asset_url(asset_id, fresh_url)
        return fresh_url