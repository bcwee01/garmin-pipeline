#!/usr/bin/env python3
"""Fetch pipeline secrets from Supabase and write to .env — used by cloud runs."""
import json
import os
import urllib.request
from pathlib import Path


def main():
    url_base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")

    if not url_base or not anon_key:
        print("SUPABASE_URL or SUPABASE_ANON_KEY not set — skipping secret fetch")
        return

    url = f"{url_base}/rest/v1/pipeline_secrets?select=key,value"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {anon_key}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        rows = json.loads(resp.read())

    env_path = Path(__file__).parent / ".env"
    with open(env_path, "w") as f:
        for row in rows:
            f.write(f"{row['key']}={row['value']}\n")

    for row in rows:
        os.environ.setdefault(row["key"], row["value"])

    print(f"Fetched {len(rows)} secrets from Supabase -> .env")


if __name__ == "__main__":
    main()
