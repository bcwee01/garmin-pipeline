#!/usr/bin/env python3
"""Rebuild _gdata from garmy DB + intervals.icu + today's dashboard_data.json."""
import json, re, sqlite3
from datetime import datetime, date, timedelta
from collections import defaultdict

_BASE     = __import__('pathlib').Path(__file__).parent
HEALTH_DB = str(_BASE / "health.db")
GARMIN    = str(_BASE / "dashboard_data.json")
IVLS      = str(_BASE / "intervals_data.json")
ARTIFACT  = str(_BASE / "artifact_base.html")
OUT       = str(_BASE / "artifact_updated.html")

with open(GARMIN) as f: g = json.load(f)
with open(IVLS)   as f: iv = json.load(f)

# ── intervals CTL/ATL lookup by date ─────────────────────────────────────────
ctl_atl = {w["date"]: w for w in iv.get("wellness", []) if w.get("date")}

# ── Pull 90 days from garmy DB ────────────────────────────────────────────────
cutoff = (date.today() - timedelta(days=90)).isoformat()
conn = sqlite3.connect(HEALTH_DB)
conn.row_factory = sqlite3.Row
rows = conn.execute("""
    SELECT * FROM daily_health_metrics
    WHERE metric_date >= ?
    ORDER BY metric_date ASC
""", (cutoff,)).fetchall()
conn.close()

def secs_to_h(s): return round(s / 3600, 4) if s else None

# ── Build daily ───────────────────────────────────────────────────────────────
daily = []
for r in rows:
    d = r["metric_date"]
    w = ctl_atl.get(d, {})
    daily.append({
        "metric_date":              d,
        "resting_heart_rate":       r["resting_heart_rate"],
        "max_heart_rate":           r["max_heart_rate"],
        "average_heart_rate":       r["average_heart_rate"],
        "hrv_last_night_avg":       r["hrv_last_night_avg"],
        "hrv_weekly_avg":           r["hrv_weekly_avg"],
        "hrv_status":               r["hrv_status"] or "",
        "sleep_duration_hours":     r["sleep_duration_hours"],
        "deep_sleep_hours":         r["deep_sleep_hours"],
        "rem_sleep_hours":          r["rem_sleep_hours"],
        "body_battery_high":        r["body_battery_high"],
        "body_battery_low":         r["body_battery_low"],
        "total_steps":              r["total_steps"],
        "avg_stress_level":         r["avg_stress_level"],
        "training_readiness_score": r["training_readiness_score"],
        "training_readiness_level": r["training_readiness_level"] or "",
        "average_spo2":             r["average_spo2"],
        "total_calories":           r["total_calories"],
        "active_calories":          r["active_calories"],
        # from intervals.icu
        "ctl":                      w.get("ctl"),
        "atl":                      w.get("atl"),
        "sleep_score":              w.get("sleep_score"),
    })

# ── Today — prefer fresh dashboard_data.json, fall back to DB last row ────────
sl  = g.get("sleep", {})
rd  = g.get("readiness", {})
proj = g.get("projection", {})
acts_today = g.get("activities", [])

# find today in DB too for fields not in dashboard_data
today_str = g.get("date") or date.today().isoformat()
db_today  = next((dict(r) for r in rows if r["metric_date"] == today_str), {})
iv_today  = ctl_atl.get(today_str, {})

run_act = next((a for a in acts_today if "run" in (a.get("type") or "").lower()), None)
any_act = acts_today[0] if acts_today else None

today = {
    "metric_date":                  today_str,
    # sleep — prefer fresh JSON, fallback DB
    "sleep_duration_hours":         secs_to_h(sl.get("total_seconds")) or db_today.get("sleep_duration_hours"),
    "deep_sleep_hours":             secs_to_h(sl.get("deep_seconds")) or db_today.get("deep_sleep_hours"),
    "rem_sleep_hours":              secs_to_h(sl.get("rem_seconds")) or db_today.get("rem_sleep_hours"),
    "light_sleep_hours":            secs_to_h(sl.get("light_seconds")) or db_today.get("light_sleep_hours"),
    "awake_hours":                  secs_to_h(sl.get("awake_seconds")) or db_today.get("awake_hours"),
    "deep_sleep_percentage":        db_today.get("deep_sleep_percentage"),
    "rem_sleep_percentage":         db_today.get("rem_sleep_percentage"),
    "light_sleep_percentage":       db_today.get("light_sleep_percentage"),
    "awake_percentage":             db_today.get("awake_percentage"),
    "average_spo2":                 sl.get("spo2_avg") or db_today.get("average_spo2"),
    "avg_sleep_respiration_value":  sl.get("respiration_avg") or db_today.get("avg_sleep_respiration_value"),
    "avg_waking_respiration_value": db_today.get("avg_waking_respiration_value"),
    "lowest_respiration_value":     db_today.get("lowest_respiration_value"),
    "highest_respiration_value":    db_today.get("highest_respiration_value"),
    # body battery
    "body_battery_high":            sl.get("body_battery_start") or db_today.get("body_battery_high"),
    "body_battery_low":             sl.get("body_battery_end") or db_today.get("body_battery_low"),
    "bodyBatteryMostRecentValue":   rd.get("body_battery_current") or db_today.get("body_battery_high"),
    # HRV / readiness
    "hrv_last_night_avg":           rd.get("hrv_5min_high") or rd.get("hrv_nightly_avg") or db_today.get("hrv_last_night_avg"),
    "hrv_weekly_avg":               rd.get("hrv_7day_avg") or db_today.get("hrv_weekly_avg"),
    "hrv_status":                   rd.get("hrv_status") or db_today.get("hrv_status") or "",
    "training_readiness_score":     rd.get("score") or db_today.get("training_readiness_score"),
    "training_readiness_level":     rd.get("level") or db_today.get("training_readiness_level") or "",
    "training_readiness_feedback":  g.get("verdict") or db_today.get("training_readiness_feedback") or "",
    # HR
    "resting_heart_rate":           rd.get("rhr_today") or db_today.get("resting_heart_rate"),
    "restingHeartRateInBeatsPerMinute": rd.get("rhr_today") or db_today.get("resting_heart_rate"),
    "min_heart_rate":               db_today.get("min_heart_rate") or rd.get("rhr_today"),
    "max_heart_rate":               (run_act or any_act or {}).get("max_hr") or db_today.get("max_heart_rate"),
    "average_heart_rate":           (run_act or any_act or {}).get("avg_hr") or db_today.get("average_heart_rate"),
    "averageHeartRateInBeatsPerMinute": (run_act or any_act or {}).get("avg_hr") or db_today.get("average_heart_rate"),
    # activity/steps
    "total_steps":                  db_today.get("total_steps"),
    "totalSteps":                   db_today.get("total_steps"),
    "dailyStepGoal":                db_today.get("step_goal"),
    "step_goal":                    db_today.get("step_goal"),
    "total_calories":               db_today.get("total_calories"),
    "totalKilocalories":            db_today.get("total_calories"),
    "active_calories":              db_today.get("active_calories"),
    "activeKilocalories":           db_today.get("active_calories"),
    "bmr_calories":                 db_today.get("bmr_calories"),
    "total_distance_meters":        db_today.get("total_distance_meters"),
    # stress
    "avg_stress_level":             db_today.get("avg_stress_level"),
    "averageStressLevel":           db_today.get("avg_stress_level"),
    "max_stress_level":             db_today.get("max_stress_level"),
    # projection
    "tomorrow_readiness":           proj.get("tomorrow_readiness"),
    "recommended_session":          proj.get("recommended_session"),
    # CTL/ATL/ACWR from dashboard load block
    "ctl":        g.get("load", {}).get("ctl") or iv_today.get("ctl"),
    "atl":        g.get("load", {}).get("atl") or iv_today.get("atl"),
    "acwr":       g.get("load", {}).get("acwr"),
    "acwr_label": g.get("load", {}).get("acwr_label") or "",
    # Sleep coaching fields
    "sleep_score":         sl.get("score"),
    "spo2_lowest":         sl.get("spo2_lowest"),
    "body_battery_delta":  sl.get("body_battery_delta"),
    # Readiness coaching fields
    "hrv_7day_avg":        rd.get("hrv_7day_avg"),
    "hrv_delta_7day":      rd.get("hrv_delta_7day"),
    "rhr_7day_avg":        rd.get("rhr_7day_avg"),
    "rhr_delta":           rd.get("rhr_delta"),
    "body_battery_current": rd.get("body_battery_current"),
    # Flags
    "flags": g.get("flags", []),
    "verdict": g.get("verdict") or "",
}

# ── Monthly aggregates from garmy DB ─────────────────────────────────────────
buckets = defaultdict(list)
for r in rows:
    m = r["metric_date"][:7]
    buckets[m].append(dict(r))

monthly = []
for m, mrows in sorted(buckets.items()):
    def avg(key):
        vals = [r.get(key) for r in mrows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None
    monthly.append({
        "month": m, "days": len(mrows),
        "avg_rhr":       avg("resting_heart_rate"),
        "avg_hrv":       avg("hrv_last_night_avg"),
        "avg_readiness": avg("training_readiness_score"),
        "avg_sleep":     avg("sleep_duration_hours"),
        "avg_stress":    avg("avg_stress_level"),
        "avg_ctl":       avg("ctl"),
        "avg_atl":       avg("atl"),
    })

# ── Helper: map one dashboard_data activity to acts format ───────────────────
def fmt_dur_text(s):
    s = int(s or 0)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h: return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"

def fmt_pace_text(sec_per_km):
    if not sec_per_km: return "--"
    m, s = divmod(int(sec_per_km), 60)
    return f"{m}:{s:02d}/km"

def map_today_act(a, date_str):
    dk = a.get("distance_km") or 0
    dur = a.get("duration_seconds") or 0
    typ = a.get("type") or ""
    is_run = "run" in typ.lower()
    is_str = "strength" in typ.lower() or "weight" in typ.lower()

    # HR zones
    hz = {z["zone"]: z["seconds"] for z in (a.get("hr_zones") or [])}

    # Per-minute chart
    chart = []
    for pm in (a.get("per_minute") or []):
        pmps = pm.get("pace_mps") or 0
        chart.append({
            "t":      pm["minute"] * 60,
            "hr":     round(pm["hr"]) if pm.get("hr") else None,
            "pace":   round(1000 / pmps) if pmps > 0.5 else None,
            "cad":    round(pm["cadence"]) if pm.get("cadence") else None,
            "pwr":    round(pm["power_w"]) if pm.get("power_w") else None,
            "gct":    round(pm["gct_ms"]) if pm.get("gct_ms") else None,
            "stride": round(pm["stride_cm"]) if pm.get("stride_cm") else None,
            "vo":     round(pm["vosc_cm"] * 10) if pm.get("vosc_cm") else None,
            "vr":     round(pm["vrat_pct"] * 10) / 10 if pm.get("vrat_pct") else None,
            "elev":   round(pm.get("elevation_m") or 0),
        })

    # Laps → kmSplits
    km_splits = []
    for lap in (a.get("laps") or []):
        pmps = lap.get("avg_pace_mps") or 0
        ldist = (lap.get("distance_km") or 0) * 1000
        ldur = round(ldist / pmps) if pmps > 0 else 0
        km_splits.append({
            "km":    lap["lap_index"],
            "dur":   ldur,
            "pace":  round(1000 / pmps) if pmps > 0.5 else None,
            "avgHR": lap.get("avg_hr"),
            "cad":   lap.get("cadence_spm") or None,
            "dist":  round(ldist),
        })

    # Strength sets
    ex_sets = []
    set_num = 1
    for ex in (a.get("strength_sets") or []):
        for s in (ex.get("sets") or []):
            kg = s.get("kg")
            reps = s.get("reps")
            dur_s = s.get("duration_s")
            ex_sets.append({
                "set":  set_num,
                "ex":   ex.get("exercise") or "Exercise",
                "kg":   kg,
                "reps": reps,
                "dur":  round(dur_s) if dur_s else None,
                "vol":  round(kg * reps) if kg and reps else None,
                "rest": None,
            })
            set_num += 1

    # Drift
    dr = a.get("drift") or {}

    # Power curve
    pc = a.get("power_curve") or {}

    # Avg pace
    avg_pace_sec = (dur / dk) if dk > 0 else None

    # Compute drift half-values from per_minute
    pm_rows = a.get("per_minute") or []
    drift_hr_h1 = drift_hr_h2 = drift_pace_h1 = drift_pace_h2 = None
    if pm_rows and len(pm_rows) >= 4:
        half = len(pm_rows) // 2
        h1 = pm_rows[:half]; h2 = pm_rows[half:]
        def avg_field(rows, key):
            vals = [r[key] for r in rows if r.get(key)]
            return sum(vals)/len(vals) if vals else None
        drift_hr_h1 = avg_field(h1, "hr"); drift_hr_h2 = avg_field(h2, "hr")
        p1 = avg_field(h1, "pace_mps"); p2 = avg_field(h2, "pace_mps")
        if p1: drift_pace_h1 = 1000/p1
        if p2: drift_pace_h2 = 1000/p2

    # Total zone time for pct calculation
    total_zone_secs = sum(hz.values()) if hz else 0

    # Build copyText
    lines = [
        f"[{date_str}  {a.get('name','Activity')}  [{typ.replace('_',' ')}]]",
    ]
    if dk: lines.append(f"    Distance                         {dk:.2f} km")
    if dur: lines.append(f"    Duration                         {fmt_dur_text(dur)}")
    if avg_pace_sec and is_run: lines.append(f"    Avg pace                         {fmt_pace_text(avg_pace_sec)}")
    if a.get("avg_hr"): lines.append(f"    Avg / Max HR                     {a['avg_hr']:.0f} / {a.get('max_hr',0):.0f} bpm")
    if a.get("aerobic_te"): lines.append(f"    Training effect                  {a['aerobic_te']:.1f} aerobic / {a.get('anaerobic_te',0):.1f} anaerobic")
    if a.get("cadence_spm"):
        lines.append(f"    Running Dynamics:")
        lines.append(f"        Cadence              {a['cadence_spm']:.0f} spm")
        if a.get("stride_cm"): lines.append(f"        Stride length        {a['stride_cm']/100:.2f} m")
        if a.get("vert_osc_cm"): lines.append(f"        Vertical oscillation {a['vert_osc_cm']:.1f} cm")
        if a.get("gct_ms"): lines.append(f"        Ground contact time  {a['gct_ms']:.0f} ms")
        if a.get("vert_ratio"): lines.append(f"        Vertical ratio       {a['vert_ratio']:.1f}%")
    if hz:
        lines.append(f"    HR Zones:")
        for z in sorted(hz):
            pct = f" ({round(hz[z]/total_zone_secs*100)}%)" if total_zone_secs else ""
            lines.append(f"        Zone {z}: {fmt_dur_text(hz[z])}{pct}")
    if km_splits:
        lines.append(f"    Per-km splits ({len(km_splits)} laps):")
        for sp in km_splits:
            lines.append(f"        Lap {sp['km']}: {sp['dist']/1000:.2f}km | {fmt_pace_text(sp['pace'])} | {int(sp['avgHR']) if sp['avgHR'] else '--'}bpm")
    if dr:
        lines.append(f"    Drift analysis (1st half → 2nd half):")
        if drift_hr_h1 and drift_hr_h2:
            pct = dr.get("hr_drift_pct") or 0
            lines.append(f"        HR drift:    {drift_hr_h1:.0f} → {drift_hr_h2:.0f} bpm  ({pct:+.1f}%)")
        elif dr.get("hr_drift_pct"):
            lines.append(f"        HR drift: {dr['hr_drift_pct']:+.1f}%")
        if drift_pace_h1 and drift_pace_h2:
            pct = dr.get("pace_drift_pct") or 0
            spd_chg = (drift_pace_h1 - drift_pace_h2) / drift_pace_h1 * 100 if drift_pace_h1 else 0
            note = "slowing" if drift_pace_h2 > drift_pace_h1 else "speeding up"
            lines.append(f"        Pace drift:  {fmt_pace_text(drift_pace_h1)} → {fmt_pace_text(drift_pace_h2)}  ({spd_chg:+.1f}% speed, {note})")
        elif dr.get("pace_drift_pct"):
            lines.append(f"        Pace drift: {dr['pace_drift_pct']:+.1f}%")
        if dr.get("aerobic_decoupling_pct"):
            dec = dr["aerobic_decoupling_pct"]
            quality = "poor — significant decoupling" if dec > 10 else "moderate" if dec > 5 else "good"
            lines.append(f"        Aerobic decoupling (Pa:Hr): {dec:+.1f}%  ({quality})")
    if pm_rows:
        lines.append(f"    Per-minute summary ({len(pm_rows)} min):")
        lines.append(f"        {'Min':>4}  {'HR':>4}  {'Pace':<10}  {'Cad':>4}  {'Pwr':>6}  {'Stride':<7}  {'GCT':>4}  {'VOsc':>5}  {'VRat':>5}  {'Elev':>5}")
        for pm in pm_rows:
            mn   = pm.get("minute", 0)
            hr   = int(round(pm.get("hr") or 0))
            pmps = pm.get("pace_mps") or 0
            pace_str = fmt_pace_text(1000/pmps) if pmps > 0.1 else "--      "
            cad  = int(round(pm.get("cadence") or 0))
            pwr  = int(round(pm.get("power_w") or 0))
            st   = pm.get("stride_cm") or 0
            gct  = int(round(pm.get("gct_ms") or 0))
            vo   = pm.get("vosc_cm") or 0
            vr   = pm.get("vrat_pct") or 0
            elev = pm.get("elevation_m") or 0
            lines.append(f"        {mn:>4}  {hr:>4}  {pace_str:<10}  {cad:>4}  {pwr:>4}W  {st/100:.2f}m  {gct:>4}  {vo:>4.1f}  {vr:>4.1f}  {elev:>4.0f}m")
    if pc:
        lines.append(f"    Power curve (best avg for duration):")
        for k, v in pc.items(): lines.append(f"        {k:<6} {v:.0f} W")

    return {
        "dt":      date_str,
        "type":    typ,
        "name":    a.get("name") or "",
        "dist":    round(dk * 1000),
        "dur":     round(dur),
        "movDur":  round(dur),
        "avgHR":   round(a["avg_hr"]) if a.get("avg_hr") else None,
        "maxHR":   round(a["max_hr"]) if a.get("max_hr") else None,
        "elev":    None,
        "cal":     None,
        "load":    None,
        # running dynamics
        "cad":    round(a["cadence_spm"]) if a.get("cadence_spm") else None,
        "gct":    round(a["gct_ms"]) if a.get("gct_ms") else None,
        "stride": round(a["stride_cm"]) if a.get("stride_cm") else None,
        "vo":     round(a["vert_osc_cm"] * 10) if a.get("vert_osc_cm") else None,
        "vr":     round(a["vert_ratio"] * 10) / 10 if a.get("vert_ratio") else None,
        "sslp":   None,
        "resp":   None,
        # training effect
        "te":     round(a["aerobic_te"] * 10) / 10 if a.get("aerobic_te") else None,
        "ane":    round(a["anaerobic_te"] * 10) / 10 if a.get("anaerobic_te") else None,
        # HR zones (seconds)
        "hrZ1": round(hz.get(1, 0)) or None,
        "hrZ2": round(hz.get(2, 0)) or None,
        "hrZ3": round(hz.get(3, 0)) or None,
        "hrZ4": round(hz.get(4, 0)) or None,
        # drift
        "driftHR":   dr.get("hr_drift_pct"),
        "driftPace": dr.get("pace_drift_pct"),
        "paHr":      dr.get("aerobic_decoupling_pct"),
        # per-minute chart & laps
        "chart":     chart,
        "kmSplits":  km_splits,
        "exSets":    ex_sets if is_str else [],
        "powerCurve": pc,
        "copyText":  "\n".join(lines),
    }

# ── Activities: today's from dashboard_data.json (rich), rest from intervals.icu ──
today_act_dates = set()
acts = []
for a in sorted(g.get("activities", []), key=lambda x: x.get("start_time", ""), reverse=True):
    act = map_today_act(a, today_str)
    acts.append(act)
    today_act_dates.add(today_str)

for a in sorted(iv.get("activities", []), key=lambda x: x.get("date", ""), reverse=True):
    dt = (a.get("date") or "")[:10]
    if dt in today_act_dates:
        continue  # skip intervals dupe for today
    dk = a.get("distance_km") or 0
    dur = a.get("duration_seconds") or 0
    acts.append({
        "dt":    dt,
        "type":  a.get("type") or "",
        "name":  a.get("name") or "",
        "dist":  round(dk * 1000) if dk else 0,
        "dur":   dur,
        "movDur": a.get("moving_time_seconds") or dur,
        "avgHR": a.get("avg_hr"),
        "maxHR": a.get("max_hr"),
        "load":  a.get("training_load"),
        "elev":  a.get("elevation_m"),
        "cal":   None,
        "cad": None, "gct": None, "stride": None,
        "vo": None, "vr": None, "sslp": None, "resp": None,
        "chart": [], "kmSplits": [], "exSets": [],
        "copyText": None,
    })

# ── Assemble and inject ───────────────────────────────────────────────────────
gdata = {
    "today":   today,
    "daily":   daily,
    "monthly": monthly,
    "acts":    acts,
    "generated_at": g.get("generated_at"),
    "verdict":      g.get("verdict"),
    "flags":        g.get("flags", []),
}

with open(ARTIFACT, encoding="utf-8") as f:
    html = f.read()

new_json = json.dumps(gdata, ensure_ascii=False, separators=(",", ":"))
html2 = re.sub(
    r'(<script[^>]*id="_gdata"[^>]*>)(.*?)(</script>)',
    lambda m: m.group(1) + new_json + m.group(3),
    html, flags=re.DOTALL
)
# Update date chip
html2 = re.sub(
    r'(&#128248; )[A-Za-z]+ \d+, \d+ \d+:\d+',
    lambda m: m.group(1) + datetime.now().strftime("%b %#d, %Y %H:%M"),
    html2
)

with open(OUT, "w", encoding="utf-8") as f:
    f.write(html2)

print(f"Done → {OUT}")
print(f"  today={today_str}, readiness={today.get('training_readiness_score')}, hrv={today.get('hrv_last_night_avg')}")
print(f"  daily={len(daily)} rows, monthly={len(monthly)}, acts={len(acts)}")
print(f"  sample daily body_battery_high: {daily[-1].get('body_battery_high')}, stress: {daily[-1].get('avg_stress_level')}, steps: {daily[-1].get('total_steps')}")
