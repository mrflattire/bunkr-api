import re
import requests

# Test the identical twin node and immediate shard fallbacks
TARGET_NODES = [
    "prxp-a.cdn.cr",  # The exact twin to prxp-b
    "prxp-c.cdn.cr",  # Alternative partition in same cluster if it exists
]

faulty_url = "https://prxp-b.cdn.cr/storage/media/VTS_01_5-DUzavUbJ.VOB?n=VTS_01_5.VOB&token=9fa0ce3d1573bc705645104402cc27834360fca6&ex=1784409616"

def test_twin_node(target_url, nodes):
    match = re.match(r"https://[^/]+(.*?)$", target_url)
    if not match: return
    path_and_tokens = match.group(1)
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://bunkr.ru"
    }
    
    print("[*] Probing shard twin nodes...\n")
    for node in nodes:
        test_url = f"https://{node}{path_and_tokens}"
        try:
            response = requests.head(test_url, headers=headers, allow_redirects=True, timeout=6)
            print(f"[-] Node {node:<15} returned status: {response.status_code}")
            if response.status_code == 200:
                print(f"\n[=== TWIN NODE BYPASS SUCCESS ===]\nLink: {test_url}\n")
                return test_url
        except:
            print(f"[-] Node {node:<15} connection failed.")
            
    print("\n[-] Shard is completely unreachable or file is hard-blocked on the backend server.")
    return None

test_twin_node(faulty_url, TARGET_NODES)
