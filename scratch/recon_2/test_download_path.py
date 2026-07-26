import urllib.parse

from curl_cffi import requests

FILE_ID = "59975730"

HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://dl.bunkr.cr",
    "Referer": f"https://dl.bunkr.cr/file/{FILE_ID}",
}

def generate_cdn_url():
    # impersonate="chrome" handles the TLS fingerprint and user-agent perfectly
    session = requests.Session(impersonate="chrome")
    
    print(f"[*] Step 1: Querying metadata API for file ID: {FILE_ID}...")
    meta_url = "https://dl.bunkr.cr/api/_001_v2"
    payload = {"id": FILE_ID}
    
    try:
        meta_res = session.post(meta_url, json=payload, headers=HEADERS, verify=False)
        meta_res.raise_for_status()
        meta_data = meta_res.json()
        
        cdn_host = meta_data["mediafiles"]
        storage_path = meta_data["path"]
        original_name = meta_data["original"]
        
        print("    [+] Successfully pulled metadata.")
        print(f"    [+] Target CDN Host: {cdn_host}")
        print(f"    [+] Storage Path:    {storage_path}")
        print(f"    [+] File Name:       {original_name}\n")
        
    except Exception as e:
        print(f"    [-] Metadata lookup failed: {e}")
        return

    print("[*] Step 2: Requesting dynamic validation token from sign server...")
    encoded_path = urllib.parse.quote(storage_path)
    sign_url = f"https://glb-apisign.cdn.cr/sign?path={encoded_path}"
    
    try:
        sign_res = session.get(sign_url, headers=HEADERS)
        sign_res.raise_for_status()
        sign_data = sign_res.json()
        
        token = sign_data["token"]
        ex = sign_data["ex"]
        
        print(f"    [+] Token Acquired:  {token}")
        print(f"    [+] Expires Epoch:   {ex}\n")
        
    except Exception as e:
        print(f"    [-] Token signature generation failed: {e}")
        return

    print("[*] Step 3: Stitching parameters together into the final payload url...")
    encoded_name = urllib.parse.quote(original_name)
    final_cdn_url = f"{cdn_host}{storage_path}?n={encoded_name}&token={token}&ex={ex}"
    
    print("-" * 80)
    print("FINAL CDN ASSET URL:")
    print(final_cdn_url)
    print("-" * 80)

if __name__ == "__main__":
    generate_cdn_url()