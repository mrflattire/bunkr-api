import urllib.parse
from pathlib import Path

from curl_cffi.requests import AsyncSession

from .config import DB_PATH, DEFAULT_OUTPUT_DIR, SEARCH_MODES, SORT_TYPES

# Internal Package Imports
from .core.db import DatabaseManager
from .core.scraper import ScraperEngine
from .core.tokens import refresh_all_tokens_async
from .media.downloader import DownloadEngine
from .media.player import PlayerEngine


class BunkrAPI:
    def __init__(self, db_path: str = DB_PATH):
        """
        Initializes the Bunkr API Facade.
        :param db_path: Path to the SQLite database file.
        """
        self.db = DatabaseManager(db_path)
        self.scraper = ScraperEngine(self.db)
        self.downloader = DownloadEngine(self.db)
        self.player = PlayerEngine(self.db)

    # ============================================================
    # DATA RETRIEVAL (Catalog)
    # ============================================================

    def get_albums(self) -> list:
        """Returns all cataloged albums in the database."""
        return [dict(r) for r in self.db.get_all_albums()]

    def get_assets(self, album_id: int) -> list:
        """Returns all assets for a specific album ID."""
        return [dict(r) for r in self.db.get_album_assets(album_id)]

    # ============================================================
    # SCRAPING & RESOLUTION
    # ============================================================

    async def search(self, term: str, mode: str = "broad", per: int = 20, sort: str = "latest") -> list:
        """
        Programmatic search. Returns a list of album dictionaries.
        """
        url_mode = SEARCH_MODES.get(mode, "broad")
        url_sort = SORT_TYPES.get(sort, "latest")

        async with AsyncSession(impersonate="chrome") as session:
            query = {'search': term, 'mode': url_mode, 'per': str(per), 'sort': url_sort}
            search_url = f"https://balbums.st/?{urllib.parse.urlencode(query)}" if term else "https://balbums.st/"

            res = await session.get(search_url, verify=False, timeout=30)
            return self.scraper.parse_albums(res.text)

    async def resolve_album(self, album_url: str, search_context: str = "API_User", save_json: bool = False) -> int:
        """
        Scrapes a specific bunkr URL and registers it in the database.
        :return: The new Database ID of the registered album.
        """
        async with AsyncSession(impersonate="chrome") as session:
            return await self.scraper.scrape_album(
                session=session,
                url=album_url,
                search_term=search_context,
                save_json=save_json
            )

    # ============================================================
    # MEDIA EXECUTION
    # ============================================================

    async def download_album(self, album_id: int, workers: int = 3, output_dir: Path = DEFAULT_OUTPUT_DIR):
        """
        Trigger a multi-threaded download for an album.
        """
        # 1. Fetch metadata
        with self.db.connection() as conn:
            album = conn.execute("SELECT title FROM albums WHERE id=?", (album_id,)).fetchone()

        if not album:
            raise ValueError(f"Album ID {album_id} not found.")

        # 2. Fetch assets and format for engine
        assets = self.db.get_album_assets(album_id)
        dl_list = []
        for a in assets:
            d = dict(a)
            d['db_asset_id'] = d['id']
            d['album_title'] = album['title']
            d['album_id'] = album_id
            dl_list.append(d)

        # 3. Await the downloader run
        await self.downloader.run(dl_list, workers=workers, output_dir=output_dir)

    async def stream_album(self, album_id: int, indices_spec: str = "all", player: str = "mpv"):
        """
        Resolves tokens and launches the media player.
        """
        assets = self.db.get_album_assets(album_id)
        if not assets:
            raise ValueError(f"No assets to stream for ID {album_id}")

        from .utils.formatting import parse_selection
        indices = parse_selection(indices_spec, total_items=len(assets))

        # 1. Standardize selected assets
        selected_assets = [dict(assets[i-1]) for i in indices]

        # 2. Await the token refresh
        await self.player.resolve_tokens_async(selected_assets)

        # 3. Build final queue
        queue = []
        for i in indices:
            a = dict(assets[i-1])
            url = self.db.get_valid_url(a['id'])
            queue.append((i, a['title'], url))

        # 4. Launch player
        if player == "vlc":
            await self.player.play_vlc(queue)
        else:
            await self.player.play_mpv(queue)

    # ============================================================
    # MAINTENANCE
    # ============================================================

    async def refresh_tokens(self, album_id: int | None = None):
        """
        Refresh signed CDN tokens asynchronously.
        """
        raw_assets = self.db.get_needs_refresh(album_id=album_id)
        expiring_assets = [dict(row) for row in raw_assets]
        
        if expiring_assets:
            max_workers = int(self.db.get_config_val("max_workers", "4"))
            await refresh_all_tokens_async(self.db, expiring_assets, max_workers)