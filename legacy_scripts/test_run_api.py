import asyncio

from bunkr_api import BunkrAPI

api = BunkrAPI()


async def main():
    # ------------------------------------------------------------
    # 1. Search -> Resolve -> Download (the classic three-step flow)
    # ------------------------------------------------------------
    results = await api.search("Natalie Roush")
    if not results:
        print("[1] No search results found — stopping here.")
        return

    album_id = await api.resolve_album(results[0]["url"])
    print(f"[1] Registered album with ID: {album_id}")

    await api.download_album(album_id, workers=5)
    print(f"[1] Download complete for album {album_id}")

    # ------------------------------------------------------------
    # 2. Catalog inspection (new sync convenience getters)
    # ------------------------------------------------------------
    album = api.get_album(album_id)
    print(f"[2] Album metadata: {album}")

    assets = api.get_assets(album_id)
    print(f"[2] Album has {len(assets)} asset(s)")

    if assets:
        fresh_url = api.get_valid_url(assets[0]["id"])
        print(f"[2] Fresh URL for first asset: {fresh_url}")

    # ------------------------------------------------------------
    # 3. One-shot resolve_and_download (new)
    #    Same result as section 1, collapsed into a single call.
    #    Uses a different search term so it isn't just re-hitting the
    #    same album already registered above.
    # ------------------------------------------------------------
    more_results = await api.search("Natalie Roush")
    if more_results:
        second_album_id = await api.resolve_and_download(more_results[0]["url"], workers=3)
        print(f"[3] resolve_and_download registered + downloaded album {second_album_id}")
    else:
        print("[3] No results for the second search term — skipping.")

    # ------------------------------------------------------------
    # 4. Staged / failed maintenance workflows (new)
    #    Staging itself is set via `bunkr-inspect stage ...` or the
    #    interactive `bunkr-api` dashboard — these just consume whatever
    #    is currently flagged, and are safe no-ops if nothing is.
    # ------------------------------------------------------------
    await api.download_staged(workers=3)
    print("[4] download_staged() finished (no-op if nothing was staged)")

    await api.retry_failed(workers=3)
    print("[4] retry_failed() finished (no-op if nothing had failed)")

    # ------------------------------------------------------------
    # 5. Token maintenance (existing) + delete_album (new, sync)
    # ------------------------------------------------------------
    await api.refresh_tokens(album_id=album_id)
    print(f"[5] Refreshed any expiring tokens for album {album_id}")

    # Destructive — commented out by default so running this script
    # repeatedly doesn't quietly wipe the album you just registered.
    # deleted = api.delete_album(album_id)
    # print(f"[5] delete_album({album_id}) -> {deleted}")


asyncio.run(main())