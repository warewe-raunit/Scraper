"""
api/test_rate_limits.py — Test script to exhaust rate limits on account_08 and find constraints.
"""

from __future__ import annotations

import sys
import asyncio
import time
from pathlib import Path

# Ensure root directory is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.dependencies import create_stealth_client

async def run_stress_test():
    print("=== Testing Rate Limits on acc_08 ===")
    
    # 1. Initialize client for acc_08
    try:
        session = create_stealth_client("acc_08")
        print(f"Stealth client created for account: {session.account_id}")
        print(f"Proxy: {session.proxy_display}")
    except Exception as e:
        print(f"Failed to initialize client: {e}")
        return

    # Let's perform a series of requests as fast as possible to see when Reddit rate-limits or blocks.
    # We will query a lightweight API endpoint: oauth.reddit.com/user/PaceNormal6940/about
    url = "https://oauth.reddit.com/user/PaceNormal6940/about"
    
    success_count = 0
    failure_count = 0
    start_time = time.time()
    
    # Send 30 requests as fast as possible (synchronous calls run sequentially)
    total_to_send = 30
    print(f"\nSending {total_to_send} requests sequentially as fast as possible...")
    
    for i in range(1, total_to_send + 1):
        req_start = time.time()
        try:
            # Execute request in a thread pool to avoid blocking the loop
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: session.get(url, impersonate="chrome120", timeout=15)
            )
            elapsed = time.time() - req_start
            
            # Read ratelimit headers if present
            rl_remaining = response.headers.get("x-ratelimit-remaining", "N/A")
            rl_reset = response.headers.get("x-ratelimit-reset", "N/A")
            rl_used = response.headers.get("x-ratelimit-used", "N/A")
            
            print(
                f"[{i:02d}] Status: {response.status_code} | "
                f"Time: {round(elapsed, 2)}s | "
                f"RateLimit Used/Remaining/Reset: {rl_used}/{rl_remaining}/{rl_reset}"
            )
            
            if response.status_code == 200:
                success_count += 1
            else:
                failure_count += 1
                if response.status_code == 429 or response.status_code == 403:
                    print(f"\n⚠️ Encountered block/rate limit at request {i}!")
                    break
        except Exception as e:
            elapsed = time.time() - req_start
            print(f"[{i:02d}] Request Failed: {e} | Time: {round(elapsed, 2)}s")
            failure_count += 1
            
        # Optional: tiny pause to avoid instant IP blocking if we want to be safe,
        # but the user requested "exhausting" to find the limit, so no pause.
    
    total_time = time.time() - start_time
    print("\n=== Test Summary ===")
    print(f"Total Requests Sent: {success_count + failure_count}")
    print(f"Successful Requests (200): {success_count}")
    print(f"Failed/Blocked Requests: {failure_count}")
    print(f"Total Time Elapsed: {round(total_time, 2)} seconds")
    if success_count + failure_count > 0:
        print(f"Average Speed: {round((success_count + failure_count) / total_time, 2)} requests per second")

if __name__ == "__main__":
    asyncio.run(run_stress_test())
