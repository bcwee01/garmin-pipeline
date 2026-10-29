#!/usr/bin/env python3
"""Fetch pipeline secrets from an endpoint and write to .env — used by cloud runs."""
import json
import os
import sys
import urllib.request
from pathlib import Path


def main():
    # Accept --url <endpoint> or fall back to env vars
    url = None
    args = sys.argv[1:]
    if "--url" in args:
        idx = args.index("--url")
        if idx + 1 < len(args):
            url = args[idx + 1]

    if not url:
        url_base = os.environ.get("SUPABASE_URL", "").rstrip("/")
        anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
        if url_base and anon_key:
            url = f"{url_base}/rest/v1/pipeline_secrets?select=key,value"
        else:
            print("No --url and no SUPABASE_URL/SUPABASE_ANON_KEY set — skipping")
            return

    print(f"Fetching secrets from {url.split('?')[0]} ...")
    req = urllib.request.Request(url)

    # Attach anon key only for REST API path (not edge function)
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    if anon_key:
        req.add_header("apikey", anon_key)
        req.add_header("Authorization", f"Bearer {anon_key}")

    with urllib.request.urlopen(req) as resp:
        payload = json.loads(resp.read())

    # Edge function returns {KEY: value}, REST API returns [{key, value}]
    if isinstance(payload, dict):
        rows = [{"key": k, "value": v} for k, v in payload.items()]
    else:
        rows = payload

    env_path = Path(__file__).parent / ".env"
    with open(env_path, "w") as f:
        for row in rows:
            f.write(f"{row['key']}={row['value']}\n")

    for row in rows:
        os.environ.setdefault(row["key"], row["value"])

    print(f"Wrote {len(rows)} secrets to .env")


if __name__ == "__main__":
    main()
