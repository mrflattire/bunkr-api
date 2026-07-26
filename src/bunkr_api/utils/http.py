import asyncio

import urllib3
from curl_cffi.curl import CurlError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

async def execute_request_with_retry_async(session, url, method="GET", json_payload=None, headers=None, retries=3, delay=1, timeout=30):
    for attempt in range(1, retries + 1):
        try:
            if method.upper() == "POST":
                res = await session.post(url, json=json_payload, headers=headers, verify=False, timeout=timeout)
            else:
                res = await session.get(url, headers=headers, verify=False, timeout=timeout)
            res.raise_for_status()
            return res
        except CurlError:
            if attempt == retries: raise
            await asyncio.sleep(delay)