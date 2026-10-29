#!/usr/bin/env python3
"""Non-interactive garmy health.db sync — reads credentials from .env."""
import os, sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "health.db"
LAST_DAYS = int(os.environ.get("GARMY_SYNC_DAYS", "3"))


def load_env(path=None):
    env = {}
    p = Path(path or SCRIPT_DIR / ".env")
    if not p.exists():
        return env
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def main():
    env = load_env()
    email    = env.get("GARMIN_EMAIL") or os.environ.get("GARMIN_EMAIL", "")
    password = env.get("GARMIN_PASSWORD") or os.environ.get("GARMIN_PASSWORD", "")

    if not email or not password:
        print("ERROR: GARMIN_EMAIL / GARMIN_PASSWORD not found in .env or environment.")
        sys.exit(1)

    from garmy import AuthClient, APIClient
    from garmy.localdb.sync import SyncManager
    from garmy.localdb.config import LocalDBConfig
    from garmy.localdb.progress import ProgressReporter
    from garmy.localdb.models import MetricType
    from garmy.localdb.activities_iterator import ActivitiesIterator

    end_date   = date.today()
    start_date = end_date - timedelta(days=LAST_DAYS - 1)
    print(f"Syncing garmy health.db: {start_date} → {end_date}")

    # Try reusing saved tokens first — avoids MFA prompt on automated runs
    auth_client = AuthClient()
    authenticated = False
    try:
        auth_client.load_tokens()
        if auth_client.needs_refresh:
            print("Tokens expired — refreshing silently...")
            auth_client.refresh_tokens()
        if auth_client.is_authenticated:
            print("Reused saved OAuth tokens (no login needed).")
            authenticated = True
    except Exception as e:
        print(f"Token reuse failed ({e}), falling back to full login...")

    if not authenticated:
        if not email or not password:
            print("ERROR: GARMIN_EMAIL / GARMIN_PASSWORD not in .env and no valid saved tokens.")
            sys.exit(1)
        auth_client.login(
            email, password,
            prompt_mfa=lambda: input("MFA code: "),
        )
        print("Logged in with email/password.")

    api_client = APIClient(auth_client=auth_client)

    config = LocalDBConfig()
    progress = ProgressReporter(use_tqdm=False)
    manager = SyncManager(db_path=DB_PATH, config=config, progress_reporter=progress)
    manager.api_client = api_client
    manager.activities_iterator = ActivitiesIterator(api_client, config.sync, progress)
    manager.activities_iterator.initialize()

    stats = manager.sync_range(
        user_id=1,
        start_date=start_date,
        end_date=end_date,
        metrics=list(MetricType),
    )
    print(f"Sync done — completed:{stats['completed']} skipped:{stats['skipped']} failed:{stats['failed']}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
