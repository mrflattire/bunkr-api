import sqlite3
import time
from contextlib import closing, contextmanager

# Internal package imports
from ..config import DB_PATH
from ..utils.formatting import extract_expiry_from_url


class DatabaseManager:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    @contextmanager
    def connection(self):
        conn = self._get_connection()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initializes tables and handles schema migrations."""
        with closing(self._get_connection()) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS albums (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        album_id_str TEXT UNIQUE,
                        title TEXT NOT NULL,
                        search_term TEXT,
                        global_index INTEGER,
                        aggregate_size TEXT DEFAULT '0 MB',
                        file_count INTEGER DEFAULT 0,
                        is_staged INTEGER DEFAULT 0,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                """)
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
                        is_staged INTEGER DEFAULT 0,
                        download_status TEXT CHECK(
                            download_status IN ('PENDING', 'DOWNLOADING', 'COMPLETED', 'FAILED')
                        ) DEFAULT 'PENDING',
                        local_file_path TEXT,
                        error_message TEXT,
                        FOREIGN KEY (album_id) REFERENCES albums(id) ON DELETE CASCADE
                    );
                """)
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS system_config (config_key TEXT PRIMARY KEY, config_value TEXT NOT NULL, description TEXT);"
                )

                # Performance Indexing
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_assets_expiry ON assets(token_expiry_timestamp);"
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_assets_album_id ON assets(album_id);")

                # Seed Defaults
                conn.execute("""
                    INSERT OR IGNORE INTO system_config (config_key, config_value, description)
                    VALUES 
                        ('max_workers', '4', 'Default concurrency worker thread limit'),
                        ('token_buffer_seconds', '600', 'Force token refresh lookahead window'),
                        ('minter_poll_interval_seconds', '60', 'Polling interval for the background minter')
                """)

    def get_config_val(self, key: str, default: str) -> str:
        with closing(self._get_connection()) as conn:
            row = conn.execute(
                "SELECT config_value FROM system_config WHERE config_key = ?;", (key,)
            ).fetchone()
            return row["config_value"] if row else default

    def register_album_from_json(self, data: dict) -> tuple[int, int, int]:
        """
        Registers/syncs an album and its files.

        Identity is keyed on the album's stable Bunkr slug (album_slug), not on
        title + search-result position — result position shifts between scrapes
        (pagination/sort/new uploads), which previously caused re-scrapes of the
        same album to register as a brand new album with zero attached files.

        Legacy JSON imports that predate album_slug fall back to the old
        title+index identity, since there's nothing more stable to key on.

        Returns (album_id, new_file_count, updated_file_count) so callers can
        give the user real feedback instead of a blind "success" message.
        """
        album_meta = data.get("selected_album", {})
        search_term = data.get("search_term", "N/A")
        files_found = data.get("files_found", [])

        album_title = album_meta.get("title", "Unknown Album")
        global_idx = album_meta.get("album_index_number", 0)
        album_slug = album_meta.get("album_slug")

        if album_slug:
            album_id_str = f"slug_{album_slug}".lower()
        else:
            # Legacy fallback (old JSON exports without a slug) — best effort only.
            album_id_str = f"{album_title}_{global_idx}".lower().replace(" ", "_")

        now = int(time.time())

        with closing(self._get_connection()) as conn:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO albums (album_id_str, title, search_term, global_index, aggregate_size, file_count, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(album_id_str) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        aggregate_size = excluded.aggregate_size,
                        file_count = excluded.file_count
                    RETURNING id;
                """,
                    (
                        album_id_str,
                        album_title,
                        search_term,
                        global_idx,
                        album_meta.get("aggregate_size", 0),
                        len(files_found),
                        now,
                        now,
                    ),
                )
                album_id = cursor.fetchone()["id"]

                # Pre-compute rows and check which source_urls already exist so we
                # can report real new-vs-updated counts (INSERT...ON CONFLICT alone
                # can't tell you which branch it took).
                prepared_rows = []
                source_urls = []
                for idx, file_rec in enumerate(files_found, start=1):
                    cdn_url = file_rec.get("signed_cdn_url")
                    expiry_ts = extract_expiry_from_url(cdn_url)
                    true_file_id = file_rec.get("true_file_id") or file_rec.get("slug_id")
                    source_url = file_rec.get("href", cdn_url)
                    prepared_rows.append(
                        (idx, file_rec, cdn_url, expiry_ts, true_file_id, source_url)
                    )
                    source_urls.append(source_url)

                existing_urls = set()
                if source_urls:
                    placeholders = ",".join("?" for _ in source_urls)
                    existing_urls = {
                        row["source_url"]
                        for row in conn.execute(
                            f"SELECT source_url FROM assets WHERE source_url IN ({placeholders});",
                            source_urls,
                        ).fetchall()
                    }

                new_count = 0
                updated_count = 0
                for idx, file_rec, cdn_url, expiry_ts, true_file_id, source_url in prepared_rows:
                    if source_url in existing_urls:
                        updated_count += 1
                    else:
                        new_count += 1

                    conn.execute(
                        """
                        INSERT INTO assets (
                            album_id, track_number, true_file_id, title, original_filename, 
                            raw_size_bytes, source_url, signed_cdn_url, token_expiry_timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_url) DO UPDATE SET
                            album_id = excluded.album_id,
                            track_number = excluded.track_number,
                            true_file_id = excluded.true_file_id,
                            title = excluded.title,
                            original_filename = excluded.original_filename,
                            raw_size_bytes = excluded.raw_size_bytes,
                            signed_cdn_url = excluded.signed_cdn_url,
                            token_expiry_timestamp = excluded.token_expiry_timestamp
                    """,
                        (
                            album_id,
                            idx,
                            true_file_id,
                            file_rec.get("title", f"Track {idx}"),
                            file_rec.get("original", f"file_{idx}"),
                            file_rec.get("size", 0),
                            source_url,
                            cdn_url,
                            expiry_ts,
                        ),
                    )

            return album_id, new_count, updated_count

    def get_all_albums(self) -> list[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            return conn.execute("SELECT * FROM albums ORDER BY updated_at DESC;").fetchall()

    def get_album_assets(self, album_id: int) -> list[sqlite3.Row]:
        with closing(self._get_connection()) as conn:
            return conn.execute(
                "SELECT * FROM assets WHERE album_id = ? ORDER BY track_number ASC;", (album_id,)
            ).fetchall()

    def update_asset_url(self, asset_id: int, new_cdn_url: str):
        expiry_ts = extract_expiry_from_url(new_cdn_url)
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                "UPDATE assets SET signed_cdn_url = ?, token_expiry_timestamp = ? WHERE id = ?",
                (new_cdn_url, expiry_ts, asset_id),
            )

    def update_download_status(
        self, asset_id: int, status: str, local_path: str | None = None, error: str | None = None
    ):
        with closing(self._get_connection()) as conn, conn:
            conn.execute(
                "UPDATE assets SET download_status = ?, local_file_path = ?, error_message = ? WHERE id = ?",
                (status, local_path, error, asset_id),
            )

    def get_needs_refresh(self, album_id: int | None = None) -> list[sqlite3.Row]:
        lookahead = int(self.get_config_val("token_buffer_seconds", "600"))
        now_with_buffer = int(time.time()) + lookahead
        with closing(self._get_connection()) as conn:
            if album_id is not None:
                return conn.execute(
                    "SELECT * FROM assets WHERE album_id = ? AND (token_expiry_timestamp IS NULL OR token_expiry_timestamp <= ?);",
                    (album_id, now_with_buffer),
                ).fetchall()
            return conn.execute(
                "SELECT * FROM assets WHERE token_expiry_timestamp IS NULL OR token_expiry_timestamp <= ?;",
                (now_with_buffer,),
            ).fetchall()

    def get_valid_url(self, asset_id: int) -> str:
        with closing(self._get_connection()) as conn:
            asset = conn.execute("SELECT * FROM assets WHERE id = ?;", (asset_id,)).fetchone()
        if not asset:
            return ""
        lookahead = int(self.get_config_val("token_buffer_seconds", "600"))
        if (
            asset["signed_cdn_url"]
            and asset["token_expiry_timestamp"]
            and asset["token_expiry_timestamp"] > (time.time() + lookahead)
        ):
            return asset["signed_cdn_url"]
        from .tokens import mint_now

        file_id = str(asset["true_file_id"]) if asset["true_file_id"] else asset["source_url"]
        fresh_url = mint_now(file_id)
        self.update_asset_url(asset_id, fresh_url)
        return fresh_url
