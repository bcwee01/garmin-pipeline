#!/usr/bin/env python3
"""Pull activity + wellness data from intervals.icu and export intervals_data.json."""

import json
import os
from datetime import date, timedelta, datetime

import requests

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(path=None):
    if path is None:
        path = os.path.join(OUTPUT_DIR, ".env")
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def main():
    env = load_env()
    api_key    = env.get("INTERVALS_API_KEY") or os.environ.get("INTERVALS_API_KEY", "")
    athlete_id = env.get("INTERVALS_ATHLETE_ID") or os.environ.get("INTERVALS_ATHLETE_ID", "0")
    if not api_key:
        print("ERROR: INTERVALS_API_KEY not set in .env or environment."); return

    session = requests.Session()
    session.auth = ("API_KEY", api_key)
    session.headers["User-Agent"] = "AthleteCoach/1.0"

    newest = date.today()
    oldest = newest - timedelta(days=90)
    params = {"oldest": oldest.isoformat(), "newest": newest.isoformat()}
    base   = "https://intervals.icu/api/v1"

    resp = session.get(f"{base}/athlete/{athlete_id}/activities", params=params)
    resp.raise_for_status()
    raw_activities = resp.json()

    resp = session.get(f"{base}/athlete/{athlete_id}/wellness", params=params)
    resp.raise_for_status()
    raw_wellness = resp.json()

    activities = []
    for a in raw_activities:
        dist_m    = a.get("distance") or 0
        move_s    = a.get("moving_time") or 0
        avg_speed = (dist_m / move_s) if move_s and dist_m else None
        activities.append({
            "date":              (a.get("start_date_local") or "")[:10],
            "type":              a.get("type"),
            "name":              a.get("name"),
            "distance_km":       round(dist_m / 1000, 3) if dist_m else None,
            "duration_seconds":  a.get("elapsed_time"),
            "moving_time_seconds": move_s or None,
            "elevation_m":       a.get("total_elevation_gain"),
            "avg_hr":            a.get("average_heartrate"),
            "max_hr":            a.get("max_heartrate"),
            "training_load":     a.get("icu_training_load"),
            "intensity":         a.get("icu_intensity"),
            "avg_pace_mps":      avg_speed,
        })

    wellness = []
    for w in raw_wellness:
        wellness.append({
            "date":          w.get("id"),
            "ctl":           w.get("ctl"),
            "atl":           w.get("atl"),
            "hrv":           w.get("hrv"),
            "hrv_baseline":  w.get("hrvBaseline") or w.get("hrv_baseline"),
            "rhr":           w.get("restingHR"),
            "weight_kg":     w.get("weight"),
            "sleep_seconds": w.get("sleepSecs"),
            "sleep_score":   w.get("sleepScore"),
            "body_fat_pct":  w.get("bodyFat"),
            "readiness":     w.get("icu_training_readiness") or w.get("readiness"),
        })

    data = {
        "generated_at": datetime.now().isoformat(),
        "range": {"oldest": oldest.isoformat(), "newest": newest.isoformat()},
        "activities": activities,
        "wellness": wellness,
    }

    out_path = os.path.join(OUTPUT_DIR, "intervals_data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"intervals_data.json: {len(activities)} activities, {len(wellness)} wellness days → {out_path}")


if __name__ == "__main__":
    main()
