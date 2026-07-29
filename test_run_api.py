from bunkr_api import BunkrAPI
import asyncio

# Initialize
api = BunkrAPI()

# 1. Search and Resolve
async def main():
    results = await api.search("Natalie Roush")
    if results:
        # Resolve the first result into our database
        album_id = await api.resolve_album(results[0]['url'])
        print(f"Registered album with ID: {album_id}")
        
        # 2. Download it
        await api.download_album(album_id, workers=5)

asyncio.run(main())