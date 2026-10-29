#!/usr/bin/env python3
"""
Garmin Daily Coaching Brief — Morning Readout
Outputs a concise actionable readout focused on today's training decision.
"""

import json, os, sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

OUTPUT_DIR = str(Path(__file__).parent)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None

def fmt_pace(mps):
    if not mps or mps <= 0: return "N/A"
    s = 1000 / mps
    return f"{int(s//60)}:{int(s%60):02d}/km"

def fmt_dur(secs):
    if secs is None or secs < 0: return "N/A"
    s = int(secs); h, r = divmod(s, 3600); m, sc = divmod(r, 60)
    return f"{h}:{m:02d}:{sc:02d}" if h else f"{m}:{sc:02d}"

def bar(val, total, w=14):
    if not total: return ""
    f = min(int(round(val / total * w)), w)
    return f"[{'#'*f}{'.'*(w-f)}] {val/total*100:.0f}%"

# ---------------------------------------------------------------------------
# Brief builder (same style as weekly)
# ---------------------------------------------------------------------------

class Brief:
    def __init__(self): self._lines = []
    def banner(self, t): self._lines += ["", "="*72, f"  {t}", "="*72]
    def sub(self, t):    self._lines.append(f"\n  [{t}]")
    def row(self, lbl, val, ind=4):
        self._lines.append(f"{' '*ind}{lbl:<32} {val}")
    def note(self, t, ind=4): self._lines.append(f"{' '*ind}{t}")
    def blank(self): self._lines.append("")
    def render(self): return "\n".join(self._lines)

# ---------------------------------------------------------------------------
# Body battery overnight change — interpolate timeseries around sleep window
# ---------------------------------------------------------------------------

def _bb_timeseries(bb):
    """Extract [(epoch_ms, value), ...] from a body_battery response."""
    if not bb: return []
    e = bb[0] if isinstance(bb, list) and bb else (bb if isinstance(bb, dict) else None)
    if not isinstance(e, dict): return []
    arr = e.get("bodyBatteryValuesArray") or []
    out = []
    for row in arr:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            try:
                ts, val = float(row[0]), row[1]
                if val is not None:
                    out.append((ts, float(val)))
            except (TypeError, ValueError):
                continue
    return out

def _bb_at(ts_series, target_ms):
    """Nearest body-battery value to a given epoch_ms timestamp."""
    if not ts_series or target_ms is None: return None
    best = min(ts_series, key=lambda r: abs(r[0] - target_ms))
    # Reject if the nearest sample is more than 30 minutes away
    if abs(best[0] - target_ms) > 30 * 60 * 1000: return None
    return best[1]

def overnight_bb_change(bb_today, bb_yesterday, sleep_dto):
    """Body battery delta between sleep start and sleep end."""
    if not sleep_dto: return None, None, None
    start_ms = sleep_dto.get("sleepStartTimestampGMT")
    end_ms   = sleep_dto.get("sleepEndTimestampGMT")
    if not start_ms or not end_ms: return None, None, None
    combined = _bb_timeseries(bb_yesterday) + _bb_timeseries(bb_today)
    if not combined: return None, None, None
    start_v = _bb_at(combined, start_ms)
    end_v   = _bb_at(combined, end_ms)
    if start_v is None or end_v is None: return None, None, None
    return start_v, end_v, end_v - start_v

# ---------------------------------------------------------------------------
# Detailed-timeline + activity-details helpers
# ---------------------------------------------------------------------------

def _epoch_ms(t):
    """Convert various Garmin timestamp formats to epoch ms (UTC)."""
    if t is None: return None
    try:
        if isinstance(t, (int, float)): return float(t)
        s = str(t).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = datetime.fromisoformat(s.split(".")[0])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() * 1000
    except Exception:
        return None

def fmt_local_hm(t):
    """Format a Garmin epoch-ms or ISO timestamp as HH:MM local."""
    ms = _epoch_ms(t)
    if ms is None: return ""
    try:    return datetime.fromtimestamp(ms/1000).strftime("%H:%M")
    except: return ""

def _stage_label(v):
    """Map sleepLevels.activityLevel float (0=Deep, 1=Light, 2=REM, 3=Awake)."""
    if v is None: return "?"
    try:    r = round(float(v))
    except: return str(v)
    return {0: "Deep", 1: "Light", 2: "REM", 3: "Awake"}.get(r, str(r))

def details_series(details, *keys):
    """Return {key: [(timestamp_ms, value), ...]} from activity details metrics."""
    out = {k: [] for k in keys}
    if not details: return out
    idx = {d.get("key"): d.get("metricsIndex")
           for d in (details.get("metricDescriptors") or []) if d.get("key")}
    rows = details.get("activityDetailMetrics") or []
    ts_i = idx.get("directTimestamp")
    for r in rows:
        m = r.get("metrics") or []
        ts = None
        if ts_i is not None and ts_i < len(m): ts = m[ts_i]
        for k in keys:
            i = idx.get(k)
            if i is None: continue
            if i < len(m) and m[i] is not None:
                try: out[k].append((ts, float(m[i])))
                except (TypeError, ValueError): continue
    return out

def per_minute_summary(series_map):
    """Aggregate metric streams into per-minute averages.
    Returns list of {minute, <key>: avg, ...} keyed off the earliest sample."""
    t0 = None
    for samples in series_map.values():
        for ts, _ in samples:
            if ts is None: continue
            if t0 is None or ts < t0: t0 = ts
    if t0 is None: return []
    bins = {}
    for k, samples in series_map.items():
        for ts, v in samples:
            if ts is None: continue
            m = int((ts - t0) / 60000)
            bins.setdefault(m, {}).setdefault(k, []).append(v)
    rows = []
    for m in sorted(bins.keys()):
        row = {"minute": m}
        for k, vals in bins[m].items():
            if vals: row[k] = sum(vals) / len(vals)
        rows.append(row)
    return rows

def power_curve(power_series, durations_s):
    """Best rolling average for each duration (s). Assumes ~uniform sampling."""
    pts = sorted((t, v) for t, v in power_series
                 if t is not None and v is not None)
    if len(pts) < 2: return {}
    span_ms = pts[-1][0] - pts[0][0]
    if span_ms <= 0: return {}
    interval_s = span_ms / 1000 / (len(pts) - 1)
    if interval_s <= 0: return {}
    vals = [v for _, v in pts]
    out = {}
    for d in durations_s:
        w = max(1, int(round(d / interval_s)))
        if w > len(vals): continue
        run = sum(vals[:w]); best = run
        for i in range(w, len(vals)):
            run += vals[i] - vals[i-w]
            if run > best: best = run
        out[d] = best / w
    return out

def first_second_half(series):
    """Split a value series chronologically. Returns (avg_first, avg_second)."""
    vals = [v for _, v in series if v is not None]
    if len(vals) < 4: return None, None
    half = len(vals) // 2
    a1 = sum(vals[:half]) / half
    a2 = sum(vals[half:]) / (len(vals) - half)
    return a1, a2

def decoupling_pct(speed_series, hr_series):
    """Pa:Hr decoupling between halves. Positive = speed dropped vs HR."""
    sp = [(t, v) for t, v in speed_series if t is not None and v is not None and v > 0]
    hr = [(t, v) for t, v in hr_series   if t is not None and v is not None and v > 0]
    if len(sp) < 4 or len(hr) < 4: return None
    n = min(len(sp), len(hr))
    sp = sp[:n]; hr = hr[:n]
    half = n // 2
    sp1 = sum(v for _, v in sp[:half]) / half
    sp2 = sum(v for _, v in sp[half:]) / (n - half)
    hr1 = sum(v for _, v in hr[:half]) / half
    hr2 = sum(v for _, v in hr[half:]) / (n - half)
    r1 = sp1 / hr1
    r2 = sp2 / hr2
    if r1 <= 0: return None
    return (r1 - r2) / r1 * 100

def best_window(dist_series, target_dist_m):
    """Fastest window covering target distance. Returns (time_s, start_dist_m)."""
    pts = sorted((t, d) for t, d in dist_series
                 if t is not None and d is not None)
    if len(pts) < 2: return None
    best = None
    j = 0
    n = len(pts)
    for i in range(n):
        if j <= i: j = i + 1
        while j < n and (pts[j][1] - pts[i][1]) < target_dist_m:
            j += 1
        if j >= n: break
        t_diff = (pts[j][0] - pts[i][0]) / 1000
        if t_diff <= 0: continue
        if best is None or t_diff < best[0]:
            best = (t_diff, pts[i][1])
    return best

# ---------------------------------------------------------------------------
# Strength training helpers
# ---------------------------------------------------------------------------

def _pick(d, *keys, default=None):
    """Return first non-empty value from a dict across common Garmin key variants."""
    if not isinstance(d, dict): return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default

def _as_list(v):
    if v is None: return []
    if isinstance(v, list): return v
    if isinstance(v, dict): return [v]
    return []

def _is_strength_activity(a):
    type_key = str(((a.get("activityType") or {}).get("typeKey")) or "").lower()
    name = str(a.get("activityName") or "").lower()
    parent = str(((a.get("activityType") or {}).get("parentTypeId")) or "").lower()
    return "strength" in type_key or "weight" in type_key or "strength" in name or parent == "13"

def _format_weight_kg(weight, unit=None):
    if weight in (None, ""): return None
    try: w = float(weight)
    except (TypeError, ValueError): return None
    if w <= 0:
        return None
    unit_s = str(unit or "").lower()
    if unit_s in ("pound", "pounds", "lb", "lbs"):
        w *= 0.45359237
    elif unit_s in ("gram", "grams", "g") or w > 1000:
        w /= 1000
    return w

def _strength_weight_value(raw):
    weight = _pick(raw, "weight", "Weight", "weightValue", "weightInKg",
                   "weightKg", "weightAmount", "exerciseWeight")
    unit = _pick(raw, "weightUnit", "WeightUnit", "weightDisplayUnit",
                 "weightUnitKey", "unitKey")
    kg = _format_weight_kg(weight, unit)
    if kg is None:
        # Some Garmin payloads nest the display weight.
        w_obj = _pick(raw, "weightDTO", "weight", "weightValueDTO")
        if isinstance(w_obj, dict):
            kg = _format_weight_kg(
                _pick(w_obj, "value", "weight", "amount"),
                _pick(w_obj, "unitKey", "unit", "uom"),
            )
    return kg

def _strength_reps(raw):
    reps = _pick(raw, "reps", "Reps", "repetitionCount", "repetitions",
                 "actualReps", "targetReps", "numberOfRepetitions")
    try: return int(float(reps)) if reps not in (None, "") else None
    except (TypeError, ValueError): return None

def _strength_seconds(raw, *keys):
    value = _pick(raw, *keys)
    if value in (None, ""): return None
    try:
        sec = float(value)
        # Garmin sometimes stores milliseconds.
        if sec > 60 * 60 * 6:
            sec /= 1000
        return sec
    except (TypeError, ValueError):
        return None

def _strength_name(raw, fallback="Unknown exercise"):
    name = _pick(raw, "exerciseName", "ExerciseName", "exerciseDisplayName",
                 "name", "workoutStepName", "workoutStepExerciseName",
                 "description", "Description", "exercise", "exerciseDTO",
                 "exerciseDetails", "exerciseDefinition")
    if isinstance(name, dict):
        name = _pick(name, "name", "displayName", "exerciseName", "value",
                     "exerciseKey", "typeKey")
    if name:
        return str(name).replace("_", " ").title()
    cat = _pick(raw, "exerciseCategory", "category", "exerciseCategoryName",
                "exerciseCategoryKey")
    return str(cat).replace("_", " ").title() if cat else fallback

def _strength_set_type(raw):
    st = _pick(raw, "setType", "SetType", "type", "setTypeKey", "workoutSetType")
    if isinstance(st, dict):
        st = _pick(st, "typeKey", "key", "name", "displayName")
    if not st: return ""
    return str(st).replace("_", " ").lower()

def _is_rest_strength_row(raw, set_type=None):
    st = str(set_type if set_type is not None else _strength_set_type(raw)).lower()
    if "rest" in st:
        return True
    reps = _strength_reps(raw)
    kg = _strength_weight_value(raw)
    name = _strength_name(raw, "")
    if reps is not None and reps <= 0 and kg is None:
        return True
    return reps is None and kg is None and not name

def _strength_description(raw):
    parts = []
    for k in ("description", "Description", "note", "notes", "exerciseCategory",
              "exerciseCategoryName", "muscleGroup", "movement", "equipment",
              "workoutStepDescription"):
        v = _pick(raw, k)
        if v and not isinstance(v, (dict, list)):
            s = str(v).replace("_", " ").strip()
            if s and s.lower() not in [p.lower() for p in parts]:
                parts.append(s)
    return " | ".join(parts)

def _flatten_strength_sets(obj):
    """Find Garmin exercise set dicts in varied get_activity_* payload shapes."""
    sets = []
    if isinstance(obj, list):
        for item in obj:
            sets.extend(_flatten_strength_sets(item))
        return sets
    if not isinstance(obj, dict): return sets

    looks_like_set = any(k in obj for k in (
        "reps", "actualReps", "targetReps", "repetitionCount", "repetitions",
        "numberOfRepetitions", "weight", "weightValue", "weightInKg",
        "weightKg", "setNumber", "setType", "setTypeKey", "workoutSetType",
        "exerciseName", "exerciseDisplayName", "exerciseDTO",
        "exerciseCategory", "workoutStepName", "workoutStepExerciseName",
    ))
    if looks_like_set:
        sets.append(obj)

    for key in (
        "exerciseSets", "activityExerciseSets", "activityExerciseSetDTOs",
        "activityExerciseSetsDTO", "exerciseSetDTOs", "sets", "setDTOs",
        "activityWorkoutExerciseSets", "workoutExerciseSets",
        "activityExerciseSetsDTOList", "exerciseSetDTOList",
        "workoutExerciseSetDTOs", "strengthExerciseSets",
    ):
        if key in obj:
            sets.extend(_flatten_strength_sets(obj.get(key)))
    return sets

def _get_strength_sets(client, act_id, details=None):
    """Prefer Garmin's exercise-set endpoint, then fall back to activity details."""
    payloads = []
    for method_name in (
        "get_activity_exercise_sets",
        "get_activity_exercise_sets_by_activity_id",
        "get_activity_exercise_sets_by_id",
    ):
        method = getattr(client, method_name, None)
        if callable(method):
            payload = safe(method, act_id)
            if payload:
                payloads.append(payload)
    if details:
        payloads.append(details)
    out = []
    for payload in payloads:
        out.extend(_flatten_strength_sets(payload))
    return out

def _normalise_strength_sets(raw_sets):
    """Group set rows by exercise and calculate volume/max load."""
    exercises = {}
    ordered = []
    for raw in raw_sets:
        if not isinstance(raw, dict): continue
        name = _strength_name(raw)
        kg = _strength_weight_value(raw)
        reps = _strength_reps(raw)
        dur = _strength_seconds(raw, "duration", "durationSeconds", "elapsedDuration",
                                "setDuration", "time", "actualDuration")
        rest = _strength_seconds(raw, "restDuration", "restTime", "restSeconds",
                                 "restTimeSeconds", "restDurationSeconds")
        set_type = _strength_set_type(raw)
        is_rest = _is_rest_strength_row(raw, set_type)
        desc = _strength_description(raw)
        start = _pick(raw, "startTimeGMT", "startGMT", "startTime", "startTimestampGMT")

        ex = exercises.setdefault(name, {
            "name": name, "sets": [], "volume": 0.0, "max_kg": None,
            "description": desc,
        })
        if name not in ordered:
            ordered.append(name)
        if desc and not ex.get("description"):
            ex["description"] = desc
        volume = 0 if is_rest else (kg or 0) * (reps or 0)
        if volume:
            ex["volume"] += volume
        if not is_rest and kg is not None and (ex["max_kg"] is None or kg > ex["max_kg"]):
            ex["max_kg"] = kg
        ex["sets"].append({
            "kg": kg, "reps": reps, "duration": dur, "rest": rest,
            "set_type": set_type, "description": desc, "start": start,
            "is_rest": is_rest,
        })
    return [exercises[name] for name in ordered]

def _render_strength_detail(brief, client, act_id, details, activity):
    raw_sets = _get_strength_sets(client, act_id, details)
    groups = _normalise_strength_sets(raw_sets)

    brief.note("Strength details:")
    if not groups:
        brief.note("  No exercise-set breakdown available from Garmin for this activity.", 6)
        return

    total_sets = sum(1 for g in groups for s in g["sets"] if not s.get("is_rest"))
    total_rest = sum(1 for g in groups for s in g["sets"] if s.get("is_rest"))
    total_volume = sum(g["volume"] for g in groups)
    max_kg = max([g["max_kg"] for g in groups if g["max_kg"] is not None], default=None)
    brief.note(f"  Session totals: {len(groups)} exercises | {total_sets} sets | "
               f"{total_volume:,.0f} kg volume"
               + (f" | max {max_kg:.1f} kg" if max_kg is not None else "")
               + (f" | {total_rest} rest intervals" if total_rest else ""), 6)

    calories = activity.get("calories")
    avg_hr = activity.get("averageHR")
    if calories or avg_hr:
        bits = []
        if calories: bits.append(f"{calories:.0f} cal")
        if avg_hr: bits.append(f"avg HR {avg_hr:.0f} bpm")
        brief.note(f"  Session context: {' | '.join(bits)}", 6)

    for g in groups:
        brief.note(f"[{g['name']}]", 6)
        if g.get("description"):
            brief.note(f"  {g['description']}", 8)
        active_idx = 0
        for s in g["sets"]:
            parts = []
            if s["start"]: parts.append(fmt_local_hm(s["start"]))
            if s.get("is_rest"):
                rest_dur = s["rest"] if s["rest"] is not None else s["duration"]
                rest_label = f"Rest after set {active_idx}" if active_idx else "Rest before set 1"
                parts.append(f"{rest_label}: {fmt_dur(rest_dur) if rest_dur is not None else 'N/A'}")
            else:
                active_idx += 1
                load = f"{s['kg']:.1f}kg" if s["kg"] is not None else "bodyweight/unknown load"
                if s["reps"] is not None:
                    parts.append(f"Set {active_idx}: {load} x {s['reps']} reps")
                elif s["duration"] is not None:
                    parts.append(f"Set {active_idx}: {load} for {fmt_dur(s['duration'])}")
                else:
                    parts.append(f"Set {active_idx}: {load}")
            extras = []
            if s["set_type"] and not s.get("is_rest"): extras.append(s["set_type"])
            if s["duration"] is not None and s["reps"] is not None and not s.get("is_rest"):
                extras.append(f"duration {fmt_dur(s['duration'])}")
            if s["rest"] is not None and not s.get("is_rest"):
                extras.append(f"rest {fmt_dur(s['rest'])}")
            if s["description"] and s["description"] != g.get("description"):
                extras.append(s["description"])
            line = "  " + "  ".join(p for p in parts if p)
            if extras:
                line += "  (" + " | ".join(extras) + ")"
            brief.note(line, 8)
        brief.note(f"  Total volume: {g['volume']:,.0f} kg"
                   + (f" | Max: {g['max_kg']:.1f} kg" if g["max_kg"] is not None else ""), 8)

# ---------------------------------------------------------------------------
# Sleep section
# ---------------------------------------------------------------------------

def section_sleep(client, today, brief, ctx):
    brief.banner("1. LAST NIGHT SLEEP")

    # Try today first (Garmin records a sleep session under the date it ENDS on,
    # so today's record = last night). Fall back to yesterday if today's hasn't
    # posted yet — that means the most recent completed session is from the
    # previous night.
    sleep_date = today
    sleep = safe(client.get_sleep_data, sleep_date.isoformat())
    dto   = (sleep or {}).get("dailySleepDTO") or {}
    if not (dto.get("sleepTimeSeconds") or 0):
        sleep_date = today - timedelta(days=1)
        sleep = safe(client.get_sleep_data, sleep_date.isoformat())
        dto   = (sleep or {}).get("dailySleepDTO") or {}

    total = dto.get("sleepTimeSeconds") or 0
    if not total:
        brief.note("  No sleep data available."); return

    if sleep_date != today:
        brief.note(f"  (today's sleep not yet posted — showing session ending {sleep_date.isoformat()})")

    deep  = dto.get("deepSleepSeconds") or 0
    rem   = dto.get("remSleepSeconds") or 0
    light = dto.get("lightSleepSeconds") or 0
    awake = dto.get("awakeSleepSeconds") or 0
    sc    = dto.get("sleepScores")
    score = (sc or {}).get("overall", {}).get("value") if isinstance(sc, dict) else None
    ctx["sleep_score"] = score
    ctx["sleep_json"] = {
        "total_seconds": total, "deep_seconds": deep, "rem_seconds": rem,
        "light_seconds": light, "awake_seconds": awake, "score": score,
        "spo2_avg": None, "spo2_lowest": None, "respiration_avg": None,
        "body_battery_start": None, "body_battery_end": None, "body_battery_delta": None,
    }

    brief.row("Total sleep", fmt_dur(total))
    if score is not None:
        feedback = (sc.get("overall", {}) or {}).get("qualifierKey") or ""
        brief.row("Sleep score", f"{score}" + (f"  ({feedback.replace('_',' ').title()})" if feedback else ""))
    brief.row("Deep", f"{fmt_dur(deep)}  ({deep/total*100:.0f}%)" if total else fmt_dur(deep))
    brief.row("REM",  f"{fmt_dur(rem)}  ({rem/total*100:.0f}%)"  if total else fmt_dur(rem))
    brief.row("Light",f"{fmt_dur(light)}  ({light/total*100:.0f}%)" if total else fmt_dur(light))
    brief.row("Awake",f"{fmt_dur(awake)}  ({awake/total*100:.0f}%)" if total else fmt_dur(awake))

    restless = len(sleep.get("sleepMovement") or [])
    if restless: brief.row("Restless moments", f"{restless}")

    # SpO2 during the same sleep session
    spo2 = safe(client.get_spo2_data, sleep_date.isoformat())
    if spo2:
        avg = spo2.get("averageSpO2"); low = spo2.get("lowestSpO2")
        if avg is not None:
            brief.row("SpO2 (avg / lowest)", f"{avg}% / {low}%" if low is not None else f"{avg}%")
            ctx["sleep_json"]["spo2_avg"] = avg
        if low is not None:
            ctx["spo2_lowest"] = low
            ctx["sleep_json"]["spo2_lowest"] = low

    # Respiration during the same sleep session
    resp = safe(client.get_respiration_data, sleep_date.isoformat())
    if resp:
        sl = resp.get("avgSleepRespirationValue")
        hi = resp.get("highestRespirationValue")
        lo = resp.get("lowestRespirationValue")
        if sl is not None:
            line = f"{sl:.1f} brpm"
            if hi is not None and lo is not None:
                line += f"  (range {lo:.1f}-{hi:.1f})"
            brief.row("Sleep respiration", line)
            ctx["sleep_json"]["respiration_avg"] = sl

    # Body battery change across the sleep window
    bb_end_day   = safe(client.get_body_battery, sleep_date.isoformat(), sleep_date.isoformat()) \
                   or safe(client.get_body_battery, sleep_date.isoformat())
    start_d      = (sleep_date - timedelta(days=1)).isoformat()
    bb_start_day = safe(client.get_body_battery, start_d, start_d) \
                   or safe(client.get_body_battery, start_d)
    start_v, end_v, delta = overnight_bb_change(bb_end_day, bb_start_day, dto)
    if delta is not None:
        sign = "+" if delta >= 0 else ""
        brief.row("Body Battery overnight", f"{start_v:.0f} → {end_v:.0f}  ({sign}{delta:.0f})")
        ctx["sleep_json"].update({"body_battery_start": start_v, "body_battery_end": end_v, "body_battery_delta": delta})
    elif bb_end_day:
        e = bb_end_day[0] if isinstance(bb_end_day, list) and bb_end_day else bb_end_day
        if isinstance(e, dict):
            ch = e.get("charged")
            if ch is not None:
                brief.row("Body Battery charged (day)", f"+{ch}")

# ---------------------------------------------------------------------------
# Readiness section
# ---------------------------------------------------------------------------

def section_readiness(client, today, brief, ctx):
    brief.banner("2. TODAY'S READINESS")

    # Training Readiness
    tr = safe(client.get_training_readiness, today.isoformat())
    e  = tr[0] if isinstance(tr, list) and tr else (tr if isinstance(tr, dict) else None)
    score = level = phrase = None
    if e:
        score  = e.get("score")
        level  = e.get("level", "")
        phrase = e.get("feedbackPhrase", "")
    if score is not None:
        brief.row("Training readiness", f"{score}  [{level}]")
        if phrase:
            brief.note(f"    {phrase.replace('_',' ').lower()}")
    else:
        brief.row("Training readiness", "N/A")
    ctx["readiness"] = score
    status = None
    ctx["readiness_json"] = {
        "score": score, "level": level or "",
        "hrv_5min_high": None, "hrv_nightly_avg": None, "hrv_status": "",
        "hrv_7day_trend": None, "hrv_7day_avg": None, "hrv_delta_7day": None,
        "rhr_today": None, "rhr_7day_avg": None, "rhr_delta": None,
        "body_battery_current": None, "recovery_time_h": None,
    }

    # HRV — last night
    hrv = safe(client.get_hrv_data, today.isoformat())
    sm  = (hrv or {}).get("hrvSummary") or {}
    last5_high = sm.get("lastNight5MinHigh")
    nightly    = sm.get("lastNight")
    status     = sm.get("hrvStatus", "")
    ctx["readiness_json"]["hrv_status"] = status or ""
    bal_lo     = sm.get("baselineLowUpper")
    bal_hi     = sm.get("baselineBalancedHigh")
    if last5_high is not None:
        brief.row("HRV 5-min high (last night)", f"{last5_high} ms")
        try:
            ctx["hrv_today"] = float(last5_high)
            ctx["readiness_json"]["hrv_5min_high"] = float(last5_high)
        except (TypeError, ValueError): pass

    # 7-day HRV history powers both the baseline comparison and trend
    history = _hrv_history(client, today)

    # HRV 5-min high vs prior 7-day average
    if last5_high is not None:
        bl_avg, bl_delta, bl_label = _hrv_baseline(history, float(last5_high))
        if bl_avg is not None:
            ctx["hrv_7day_avg"] = bl_avg
            ctx["hrv_delta_7day"] = bl_delta
            ctx["readiness_json"]["hrv_7day_avg"] = bl_avg
            ctx["readiness_json"]["hrv_delta_7day"] = bl_delta
            sign = "+" if bl_delta >= 0 else ""
            brief.row("HRV vs baseline",
                      f"{last5_high} ms  (7-day avg {bl_avg:.0f}, {sign}{bl_delta:.0f} — {bl_label})")

    if nightly is not None:
        line = f"{nightly} ms"
        if bal_lo and bal_hi:
            line += f"  (baseline {bal_lo}-{bal_hi})"
        if status:
            line += f"  [{status}]"
        brief.row("HRV nightly avg", line)
        try: ctx["readiness_json"]["hrv_nightly_avg"] = float(nightly)
        except (TypeError, ValueError): pass

    # HRV trend direction from 7-night series
    trend_dir, trend_pct = _hrv_trend(history)
    if trend_dir:
        brief.row("HRV 7-day trend", f"{trend_dir}  ({trend_pct:+.1f}%)")
    ctx["hrv_trend"] = trend_dir
    ctx["hrv_status"] = status
    ctx["readiness_json"]["hrv_7day_trend"] = trend_dir
    ctx["readiness_json"]["hrv_status"] = status or ""

    # Resting HR today vs 7-day average
    today_stats = safe(client.get_stats, today.isoformat()) or {}
    rhr_today   = today_stats.get("restingHeartRate")
    rhr_vals    = []
    for i in range(1, 8):
        d = (today - timedelta(days=i)).isoformat()
        s = safe(client.get_stats, d) or {}
        v = s.get("restingHeartRate")
        if v: rhr_vals.append(v)
    if rhr_today:
        ctx["readiness_json"]["rhr_today"] = rhr_today
        if rhr_vals:
            avg7 = sum(rhr_vals) / len(rhr_vals)
            delta = rhr_today - avg7
            sign = "+" if delta >= 0 else ""
            brief.row("Resting HR", f"{rhr_today} bpm  (7-day avg {avg7:.0f}, {sign}{delta:.0f})")
            ctx["rhr_delta_7d"] = delta
            ctx["readiness_json"]["rhr_7day_avg"] = round(avg7, 1)
            ctx["readiness_json"]["rhr_delta"] = round(delta, 1)
        else:
            brief.row("Resting HR", f"{rhr_today} bpm")

    # Body Battery — current
    bb = safe(client.get_body_battery, today.isoformat(), today.isoformat()) \
         or safe(client.get_body_battery, today.isoformat())
    if bb:
        bb_e = bb[0] if isinstance(bb, list) and bb else bb
        if isinstance(bb_e, dict):
            ts = _bb_timeseries(bb)
            if ts:
                current_bb = ts[-1][1]
                ch = bb_e.get("charged"); dr = bb_e.get("drained")
                line = f"{current_bb:.0f}"
                if ch is not None and dr is not None:
                    line += f"  (charged {ch} / drained {dr} today)"
                brief.row("Body Battery (current)", line)
                ctx["body_battery_current"] = current_bb
                ctx["readiness_json"]["body_battery_current"] = current_bb
            else:
                ev = bb_e.get("endValue")
                if ev is not None:
                    brief.row("Body Battery", f"{ev}")
                    ctx["body_battery_current"] = ev

    # Recovery time + training load (one fetch, reused by verdict + projection)
    ts_raw = safe(client.get_training_status, today.isoformat())
    recov_t = _recovery_time(ts_raw)
    if recov_t is not None and recov_t > 0:
        brief.row("Recovery time remaining", f"{recov_t:.0f} h")
    elif recov_t == 0:
        brief.row("Recovery time remaining", "0 h  (fully recovered)")
    ctx["recovery_time_h"] = recov_t
    ctx["training_load"]   = _training_load(ts_raw)
    ctx["readiness_json"]["recovery_time_h"] = recov_t

def _ts_first_device(ts_raw):
    """Navigate the get_training_status response to the first device entry."""
    if not isinstance(ts_raw, dict): return {}
    mts = ts_raw.get("mostRecentTrainingStatus") or {}
    ltd = mts.get("latestTrainingStatusData") if isinstance(mts, dict) else None
    if not isinstance(ltd, dict) or not ltd:
        ltd = ts_raw.get("latestTrainingStatusData")
    if isinstance(ltd, dict) and ltd:
        entry = next(iter(ltd.values()), {})
        return entry if isinstance(entry, dict) else {}
    return {}

def _recovery_time(ts_raw):
    entry = _ts_first_device(ts_raw)
    rt = entry.get("recoveryTime")
    try: return float(rt) if rt is not None else None
    except (TypeError, ValueError): return None

def _training_load(ts_raw):
    """Returns {atl, ctl, acwr, acwr_label} from training status, partial-fill OK."""
    entry = _ts_first_device(ts_raw)
    atl_dto = entry.get("acuteTrainingLoadDTO") or {}
    atl  = atl_dto.get("dailyTrainingLoadAcute")
    ctl  = atl_dto.get("dailyTrainingLoadChronic")
    acwr = atl_dto.get("dailyAcuteChronicWorkloadRatio")
    acwr_status = atl_dto.get("acwrStatus")
    out = {}
    try:
        if atl is not None: out["atl"] = float(atl)
    except (TypeError, ValueError): pass
    try:
        if ctl is not None: out["ctl"] = float(ctl)
    except (TypeError, ValueError): pass
    try:
        if acwr is not None:
            r = float(acwr)
            out["acwr"] = r
            if acwr_status:
                out["acwr_label"] = str(acwr_status).replace("_", " ").title()
            else:
                out["acwr_label"] = ("Undertraining" if r < 0.8
                                     else "Optimal" if r <= 1.3
                                     else "Overreaching")
    except (TypeError, ValueError): pass
    return out

def _hrv_history(client, today):
    """Fetch 7 nights of HRV. Returns list of (days_ago, nightly_avg, high_5min)."""
    out = []
    for i in range(7):
        d = (today - timedelta(days=i)).isoformat()
        h = safe(client.get_hrv_data, d)
        if not h: continue
        sm = h.get("hrvSummary") or {}
        nightly = sm.get("lastNight")
        high5   = sm.get("lastNight5MinHigh")
        out.append((i,
                    float(nightly) if nightly else None,
                    float(high5)   if high5   else None))
    return out

def _hrv_trend(history):
    """Return (label, pct_change) from nightly-avg series in HRV history."""
    vals = [(i, n) for (i, n, _) in history if n is not None]
    if len(vals) < 3: return None, None
    vals.sort(key=lambda x: -x[0])  # oldest first
    half  = len(vals) // 2
    early = sum(v for _, v in vals[:half]) / half
    late  = sum(v for _, v in vals[half:]) / (len(vals) - half)
    pct   = (late - early) / early * 100
    if   pct >  3: return "rising",  pct
    elif pct < -3: return "falling", pct
    else:          return "stable",  pct

def _hrv_baseline(history, today_high):
    """Compare today's 5-min high to the prior-6-night average.
    Returns (baseline_avg, delta, direction_label) or (None, None, None)."""
    if today_high is None: return None, None, None
    prior = [h for (i, _, h) in history if i > 0 and h is not None]
    if not prior: return None, None, None
    avg   = sum(prior) / len(prior)
    delta = today_high - avg
    if   delta >  5: label = "ABOVE BASELINE ↑"
    elif delta < -5: label = "BELOW BASELINE ↓"
    else:            label = "WITHIN BASELINE"
    return avg, delta, label

# ---------------------------------------------------------------------------
# Today's training
# ---------------------------------------------------------------------------

def _activity_start_local(act):
    raw = act.get("startTimeLocal") or act.get("startTimeGMT") or act.get("startTime")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.strptime(str(raw)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

def _daily_training_window(today):
    """Report activities from yesterday 12:00 through today before 10:00."""
    prev = today - timedelta(days=1)
    window_start = datetime.combine(prev, datetime.min.time()).replace(hour=12)
    window_end = datetime.combine(today, datetime.min.time()).replace(hour=10)
    return prev, window_start, window_end

def _activities_for_daily_window(client, today):
    prev, window_start, window_end = _daily_training_window(today)
    raw = []
    raw += safe(client.get_activities_by_date, prev.isoformat(), prev.isoformat()) or []
    raw += safe(client.get_activities_by_date, today.isoformat(), today.isoformat()) or []

    seen = set()
    out = []
    for act in raw:
        act_id = act.get("activityId") or id(act)
        if act_id in seen:
            continue
        seen.add(act_id)
        st = _activity_start_local(act)
        if st is None:
            continue
        if window_start <= st < window_end:
            out.append(act)
    out.sort(key=lambda a: _activity_start_local(a) or datetime.min)
    return out, window_start, window_end

def section_today(client, today, brief, ctx):
    brief.banner(f"3. TODAY'S TRAINING  ({today.isoformat()})")

    acts, window_start, window_end = _activities_for_daily_window(client, today)
    today_acts = [a for a in acts if (_activity_start_local(a) or datetime.min).date() == today]
    previous_acts = [a for a in acts if (_activity_start_local(a) or datetime.min).date() < today]
    ctx["activity_done_today"] = bool(today_acts)
    ctx["activities_json"] = []
    if previous_acts:
        ctx["previous_training_date"] = (today - timedelta(days=1)).isoformat()

    brief.row("Training window",
              f"{window_start.strftime('%Y-%m-%d %H:%M')} to {window_end.strftime('%Y-%m-%d %H:%M')}")

    if not acts:
        brief.note("  No activity logged in training window.")
        return

    # Track peak training effect across the window for projection/context use.
    today_max_te = 0.0
    previous_max_te = 0.0

    for a in acts:
        start_dt = _activity_start_local(a)
        act_id   = a.get("activityId")
        type_key = (a.get("activityType") or {}).get("typeKey", "unknown")
        name     = a.get("activityName", "Activity")
        dist_km  = (a.get("distance") or 0) / 1000
        dur_s    = a.get("duration") or 0
        avg_hr   = a.get("averageHR") or 0
        max_hr   = a.get("maxHR") or 0
        speed    = a.get("averageSpeed") or 0
        aer_te   = a.get("aerobicTrainingEffect") or 0
        ana_te   = a.get("anaerobicTrainingEffect") or 0
        aer_msg  = a.get("aerobicTrainingEffectMessage") or ""

        act_json = {
            "name": name, "type": type_key,
            "start_time": start_dt.isoformat() if start_dt else None,
            "distance_km": round(dist_km, 3), "duration_seconds": dur_s,
            "avg_pace_mps": speed, "avg_hr": avg_hr, "max_hr": max_hr,
            "aerobic_te": aer_te, "anaerobic_te": ana_te,
            "cadence_spm": None, "stride_cm": None, "gct_ms": None,
            "vert_osc_cm": None, "vert_ratio": None,
            "hr_zones": [], "laps": [], "per_minute": [],
            "power_curve": {}, "drift": {}, "strength_sets": [],
        }
        start_label = start_dt.strftime("%Y-%m-%d %H:%M") if start_dt else ""
        brief.sub(f"{start_label}  {name}  [{type_key}]".strip())
        if dist_km > 0: brief.row("Distance",     f"{dist_km:.2f} km")
        brief.row("Duration",                     fmt_dur(dur_s))
        if speed:       brief.row("Avg pace",     fmt_pace(speed))
        if avg_hr:      brief.row("Avg / Max HR", f"{avg_hr:.0f} / {max_hr:.0f} bpm")
        if aer_te:
            te = f"{aer_te:.1f} aerobic / {ana_te:.1f} anaerobic"
            if aer_msg: te += f"  ({aer_msg.replace('_',' ').title()})"
            brief.row("Training effect", te)

        # Running dynamics (for runs only)
        if "run" in type_key.lower():
            cadence  = a.get("averageRunningCadenceInStepsPerMinute")
            stride   = a.get("avgStrideLength")        # cm
            vert_osc = a.get("avgVerticalOscillation") # cm
            gct      = a.get("avgGroundContactTime")   # ms
            vert_rat = a.get("avgVerticalRatio")
            if any([cadence, stride, vert_osc, gct, vert_rat]):
                brief.note("Running Dynamics:")
                if cadence:   brief.note(f"  Cadence              {cadence:.0f} spm", 6)
                if stride:    brief.note(f"  Stride length        {float(stride)/100:.2f} m", 6)
                if vert_osc:  brief.note(f"  Vertical oscillation {vert_osc:.1f} cm", 6)
                if gct:       brief.note(f"  Ground contact time  {gct:.0f} ms", 6)
                if vert_rat:  brief.note(f"  Vertical ratio       {vert_rat:.1f}%", 6)
            act_json.update({
                "cadence_spm": cadence,
                "stride_cm": float(stride) if stride else None,
                "gct_ms": float(gct) if gct else None,
                "vert_osc_cm": vert_osc, "vert_ratio": vert_rat,
            })

        # HR zone split
        if act_id:
            zones = safe(client.get_activity_hr_in_timezones, act_id)
            if zones and isinstance(zones, list):
                shown = False
                for z in zones:
                    if not isinstance(z, dict): continue
                    zsec = z.get("secsInZone") or 0
                    if zsec <= 0: continue
                    if not shown:
                        brief.note("HR Zones:"); shown = True
                    zn   = z.get("zoneNumber", "?")
                    zpct = z.get("zonePct") or 0
                    brief.note(f"  Zone {zn}: {fmt_dur(zsec)} ({zpct:.0f}%)", 6)
                    act_json["hr_zones"].append({"zone": zn, "seconds": zsec, "pct": zpct})

        # Per-km splits
        if act_id:
            splits = safe(client.get_activity_splits, act_id)
            laps   = (splits or {}).get("lapDTOs") or []
            if laps:
                brief.note(f"Per-km splits ({len(laps)} laps):")
                for lap in laps:
                    li   = lap.get("lapIndex") or lap.get("messageIndex", "?")
                    ldst = (lap.get("distance") or 0) / 1000
                    lspd = lap.get("averageSpeed") or 0
                    lhr  = lap.get("averageHR") or 0
                    lcad = lap.get("averageRunningCadenceInStepsPerMinute") or 0
                    parts = [f"{ldst:.2f}km"]
                    if lspd: parts.append(fmt_pace(lspd))
                    if lhr:  parts.append(f"{lhr:.0f}bpm")
                    if lcad: parts.append(f"{lcad:.0f}spm")
                    brief.note(f"  Lap {li}: {' | '.join(parts)}", 6)
                    act_json["laps"].append({
                        "lap_index": li, "distance_km": round(ldst, 3),
                        "avg_pace_mps": lspd, "avg_hr": lhr, "cadence_spm": lcad,
                    })

        # Track peak TE for tomorrow's projection
        try:
            aer_te_f = float(aer_te or 0)
            if start_dt and start_dt.date() == today:
                if aer_te_f > today_max_te: today_max_te = aer_te_f
            else:
                if aer_te_f > previous_max_te: previous_max_te = aer_te_f
        except (TypeError, ValueError): pass

        # ----- Granular detail from get_activity_details / exercise sets -----
        if not act_id: continue
        details = safe(client.get_activity_details, act_id)

        if _is_strength_activity(a):
            _render_strength_detail(brief, client, act_id, details, a)
            raw_sets = _get_strength_sets(client, act_id, details)
            for g in _normalise_strength_sets(raw_sets):
                act_json["strength_sets"].append({
                    "exercise": g["name"],
                    "sets": [{"kg": s["kg"], "reps": s["reps"], "duration_s": s["duration"]}
                             for s in g["sets"] if not s.get("is_rest")],
                    "volume_kg": round(g["volume"], 1),
                    "max_kg": g["max_kg"],
                })
            ctx["activities_json"].append(act_json)
            continue

        if not details:
            ctx["activities_json"].append(act_json)
            continue

        series = details_series(
            details,
            "directHeartRate", "directSpeed", "directDistance", "directElevation",
            "directRunCadence", "directDoubleCadence",
            "directPower", "directStrideLength",
            "directGroundContactTime", "directVerticalOscillation",
            "directVerticalRatio",
        )
        # Combine cadence aliases (different watches use different keys)
        cad_series = series.get("directRunCadence") or series.get("directDoubleCadence") or []

        # Per-minute summary table
        active_keys = {"hr": series["directHeartRate"], "speed": series["directSpeed"],
                       "cad": cad_series, "pwr": series["directPower"],
                       "stride": series["directStrideLength"],
                       "gct": series["directGroundContactTime"],
                       "vosc": series["directVerticalOscillation"],
                       "vrat": series["directVerticalRatio"],
                       "elev": series["directElevation"]}
        active_keys = {k: v for k, v in active_keys.items() if v}
        rows = per_minute_summary(active_keys) if active_keys else []
        if rows:
            brief.note(f"Per-minute summary ({len(rows)} min):")
            brief.note("  Min  HR    Pace      Cad  Pwr    Stride  GCT   VOsc  VRat  Elev", 6)
            for r in rows:
                hr   = f"{r['hr']:.0f}"      if r.get("hr")     else "-"
                pace = fmt_pace(r['speed'])  if r.get("speed")  else "-"
                cad  = f"{r['cad']:.0f}"     if r.get("cad")    else "-"
                pwr  = f"{r['pwr']:.0f}W"    if r.get("pwr")    else "-"
                stride = f"{r['stride']/100:.2f}m" if r.get("stride") else "-"
                gct  = f"{r['gct']:.0f}"     if r.get("gct")    else "-"
                vosc = f"{r['vosc']:.1f}"    if r.get("vosc")   else "-"
                vrat = f"{r['vrat']:.1f}"    if r.get("vrat")   else "-"
                elev = f"{r['elev']:.0f}m"   if r.get("elev")   else "-"
                brief.note(f"  {r['minute']:>3}  {hr:<4}  {pace:<9} "
                           f"{cad:<4} {pwr:<5}  {stride:<6} {gct:<5} {vosc:<5} {vrat:<5} {elev}", 6)
                act_json["per_minute"].append({
                    "minute": r["minute"],
                    "hr": r.get("hr"), "pace_mps": r.get("speed"),
                    "cadence": r.get("cad"), "power_w": r.get("pwr"),
                    "stride_cm": r.get("stride"), "gct_ms": r.get("gct"),
                    "vosc_cm": r.get("vosc"), "vrat_pct": r.get("vrat"),
                    "elevation_m": r.get("elev"),
                })

        # Best splits (rolling-window over distance series)
        dist_series = series.get("directDistance") or []
        if dist_series:
            b400 = best_window(dist_series, 400)
            b1k  = best_window(dist_series, 1000)
            if b400 or b1k:
                brief.note("Best splits:")
                if b400:
                    t_s, start_m = b400
                    eq_speed = 400 / t_s if t_s > 0 else 0
                    brief.note(f"  Fastest 400m: {fmt_dur(int(t_s))}  "
                               f"(equiv {fmt_pace(eq_speed)})  starting at {start_m/1000:.2f} km", 6)
                if b1k:
                    t_s, start_m = b1k
                    eq_speed = 1000 / t_s if t_s > 0 else 0
                    brief.note(f"  Fastest km:   {fmt_dur(int(t_s))}  "
                               f"(equiv {fmt_pace(eq_speed)})  starting at {start_m/1000:.2f} km", 6)

        # Power curve (if device recorded power)
        pwr_series = series.get("directPower") or []
        if pwr_series:
            durations = [1, 5, 10, 30, 60, 300, 600, 1200, 1800]
            curve = power_curve(pwr_series, durations)
            if curve:
                brief.note("Power curve (best avg for duration):")
                labels = {1:"1s", 5:"5s", 10:"10s", 30:"30s", 60:"1min",
                          300:"5min", 600:"10min", 1200:"20min", 1800:"30min"}
                for d in durations:
                    if d in curve:
                        brief.note(f"  {labels[d]:<6} {curve[d]:.0f} W", 6)
                        act_json["power_curve"][labels[d]] = round(curve[d], 1)

        # HR drift / pace drift / decoupling
        hr_avg1, hr_avg2     = first_second_half(series.get("directHeartRate") or [])
        sp_avg1, sp_avg2     = first_second_half(series.get("directSpeed") or [])
        dec = decoupling_pct(series.get("directSpeed") or [], series.get("directHeartRate") or [])

        if hr_avg1 is not None or sp_avg1 is not None or dec is not None:
            brief.note("Drift analysis (1st half → 2nd half):")
        if hr_avg1 is not None:
            d_pct = (hr_avg2 - hr_avg1) / hr_avg1 * 100 if hr_avg1 > 0 else 0
            sign  = "+" if d_pct >= 0 else ""
            brief.note(f"  HR drift:    {hr_avg1:.0f} → {hr_avg2:.0f} bpm  ({sign}{d_pct:.1f}%)", 6)
            act_json["drift"]["hr_drift_pct"] = round(d_pct, 2)
        if sp_avg1 is not None:
            d_pct = (sp_avg2 - sp_avg1) / sp_avg1 * 100 if sp_avg1 > 0 else 0
            sign  = "+" if d_pct >= 0 else ""
            tag   = "slowing" if d_pct < 0 else "accelerating"
            brief.note(f"  Pace drift:  {fmt_pace(sp_avg1)} → {fmt_pace(sp_avg2)}  "
                       f"({sign}{d_pct:.1f}% speed, {tag})", 6)
            act_json["drift"]["pace_drift_pct"] = round(d_pct, 2)
        if dec is not None:
            prev_dec = ctx.get("max_aerobic_decoupling")
            if prev_dec is None or dec > prev_dec:
                ctx["max_aerobic_decoupling"] = dec
            if   dec <= 3: tag = "excellent aerobic state"
            elif dec <= 5: tag = "good aerobic state"
            elif dec <= 8: tag = "fair — moderate decoupling"
            else:           tag = "poor — significant decoupling"
            sign = "+" if dec >= 0 else ""
            brief.note(f"  Aerobic decoupling (Pa:Hr): {sign}{dec:.1f}%  ({tag})", 6)
            act_json["drift"]["aerobic_decoupling_pct"] = round(dec, 2)

        ctx["activities_json"].append(act_json)

    # End of activities loop — record peak TE for projection / context.
    ctx["today_max_te"] = today_max_te if today_max_te > 0 else None
    ctx["previous_max_te"] = previous_max_te if previous_max_te > 0 else None

# ---------------------------------------------------------------------------
# Coaching verdict
# ---------------------------------------------------------------------------

def coaching_verdict(ctx):
    readiness = ctx.get("readiness")
    if readiness is None:
        return "UNKNOWN", "No training readiness data — defer to subjective feel."
    if readiness >= 70:
        verdict = "GREEN"
        msg = "GREEN — proceed with planned session"
    elif readiness >= 40:
        verdict = "AMBER"
        msg = "AMBER — reduce intensity, consider easy only"
    else:
        verdict = "RED"
        msg = "RED — rest or walk only"
    return verdict, msg

def section_verdict(brief, ctx):
    brief.banner("4. DAILY COACHING VERDICT")
    activity_done = ctx.get("activity_done_today", False)
    brief.blank()

    if activity_done:
        verdict = "DONE"
        brief.note("  >>> Session complete — recovery focus for remainder of day <<<")
    else:
        verdict, msg = coaching_verdict(ctx)
        brief.note(f"  >>> {msg} <<<")
    brief.blank()

    # When a session is done, show training load instead of the go/no-go score
    if activity_done:
        load = ctx.get("training_load") or {}
        atl  = load.get("atl"); ctl = load.get("ctl")
        acwr = load.get("acwr"); acwr_lbl = load.get("acwr_label")
        if atl is not None:  brief.row("Acute Load (ATL)",   f"{atl:.1f}")
        if ctl is not None:  brief.row("Chronic Load (CTL)", f"{ctl:.1f}")
        if acwr is not None:
            line = f"{acwr:.2f}"
            if acwr_lbl: line += f"  [{acwr_lbl}]"
            line += "  (optimal 0.80-1.30)"
            brief.row("ACWR", line)
        if atl is None and ctl is None and acwr is None:
            brief.note("    (training load data not available)")
    else:
        readiness = ctx.get("readiness")
        if readiness is not None:
            brief.row("Readiness score", f"{readiness}")

    hrv_trend  = ctx.get("hrv_trend")
    hrv_status = ctx.get("hrv_status")
    if hrv_trend:
        line = hrv_trend
        if hrv_status: line += f"  (status: {hrv_status})"
        brief.row("HRV trend", line)

    sleep_score = ctx.get("sleep_score")
    if sleep_score is not None:
        if   sleep_score >= 80: tag = "excellent"
        elif sleep_score >= 60: tag = "fair"
        else:                   tag = "poor"
        brief.row("Sleep score", f"{sleep_score}  ({tag})")

    brief.blank()
    nuance = []
    if hrv_trend == "falling":
        nuance.append("HRV trending down — fatigue accumulating, prioritise recovery today.")
    elif hrv_trend == "rising":
        nuance.append("HRV trending up — body is adapting, normal training response.")
    if sleep_score is not None and sleep_score < 60:
        nuance.append("Sleep score is poor — favour aerobic over anaerobic work.")
    if not activity_done and sleep_score is not None and sleep_score >= 85 and verdict == "GREEN":
        nuance.append("Strong sleep + good readiness — solid day for a quality session.")
    if activity_done:
        acwr = (ctx.get("training_load") or {}).get("acwr")
        if acwr is not None:
            if acwr > 1.3:
                nuance.append("ACWR above 1.30 — overreaching zone, keep tomorrow easy.")
            elif acwr < 0.8:
                nuance.append("ACWR below 0.80 — undertraining zone, room to add load this week.")
    for n in nuance:
        brief.note(f"  • {n}")

    ctx["verdict_str"] = verdict
    ctx["flags"] = []
    return verdict

# ---------------------------------------------------------------------------
# Recovery projection (Section 5)
# ---------------------------------------------------------------------------

def section_recovery_projection(brief, ctx):
    brief.banner("5. RECOVERY PROJECTION")

    load          = ctx.get("training_load") or {}
    atl           = load.get("atl")
    ctl           = load.get("ctl")
    acwr          = load.get("acwr")
    acwr_lbl      = load.get("acwr_label")
    sleep_score   = ctx.get("sleep_score")
    readiness     = ctx.get("readiness")
    hrv_trend     = ctx.get("hrv_trend")
    hrv_status    = ctx.get("hrv_status")
    rhr_delta     = ctx.get("rhr_delta_7d")
    spo2_low      = ctx.get("spo2_lowest")
    recov_h       = ctx.get("recovery_time_h")
    today_te      = ctx.get("today_max_te")
    previous_te   = ctx.get("previous_max_te")
    previous_date = ctx.get("previous_training_date")
    activity_done = ctx.get("activity_done_today", False)
    hrv_today     = ctx.get("hrv_today")
    hrv_7day_avg  = ctx.get("hrv_7day_avg")
    decoupling    = ctx.get("max_aerobic_decoupling")

    # --- Project tomorrow's readiness (heuristic, transparent) ---
    base = readiness if readiness is not None else 65
    proj = float(base)

    if sleep_score is not None:
        if   sleep_score < 60: proj -= 8
        elif sleep_score >= 80: proj += 4

    if activity_done and today_te is not None:
        if   today_te >= 4.0: proj -= 20
        elif today_te >= 3.0: proj -= 12
        elif today_te >= 2.0: proj -= 6
        else:                  proj -= 2

    if acwr is not None:
        if   acwr > 1.5: proj -= 15
        elif acwr > 1.3: proj -= 8
        elif acwr < 0.6: proj += 4

    if   hrv_trend == "falling": proj -= 8
    elif hrv_trend == "rising":  proj += 4

    if recov_h is not None:
        if   recov_h > 24: proj -= 10
        elif recov_h > 12: proj -= 5

    proj = max(0, min(100, proj))

    # --- Tomorrow's recommended session type ---
    if   proj >= 75 and (acwr is None or acwr <= 1.3):
        rec = "MODERATE/HARD — quality session ok"
    elif proj >= 55:
        rec = "EASY/MODERATE — keep effort conversational"
    elif proj >= 35:
        rec = "EASY only — recovery-paced"
    else:
        rec = "REST — full recovery day or walk only"

    brief.row("Projected tomorrow readiness", f"{proj:.0f}  (heuristic estimate)")
    brief.row("Recommended session type",     rec)
    ctx["projection_json"] = {"tomorrow_readiness": round(proj), "recommended_session": rec}

    # --- Inputs the projection is based on ---
    brief.sub("Inputs used")
    if readiness   is not None: brief.row("Today's readiness",   f"{readiness}")
    if sleep_score is not None: brief.row("Last-night sleep",    f"{sleep_score}")
    if hrv_trend:               brief.row("HRV trend",           f"{hrv_trend}" + (f" ({hrv_status})" if hrv_status else ""))
    if rhr_delta   is not None: brief.row("RHR vs 7-day avg",    f"{rhr_delta:+.0f} bpm")
    if today_te    is not None: brief.row("Today's peak TE",     f"{today_te:.1f}")
    if previous_date and previous_te is not None:
        brief.row("Previous training TE", f"{previous_te:.1f}  ({previous_date})")
    if recov_h     is not None: brief.row("Recovery time left",  f"{recov_h:.0f} h")
    if atl is not None and ctl is not None:
        brief.row("ATL / CTL",  f"{atl:.1f} / {ctl:.1f}")
    if acwr is not None:
        brief.row("ACWR",       f"{acwr:.2f}" + (f"  [{acwr_lbl}]" if acwr_lbl else ""))

    # --- Warning flags ---
    brief.sub("Flags")
    flags = []
    if spo2_low is not None and spo2_low < 88:
        flags.append("FLAG: Lowest SpO2 below 88% — monitor sleep-disordered breathing trend.")
    if hrv_today is not None and hrv_7day_avg is not None and hrv_today < hrv_7day_avg - 10:
        flags.append("FLAG: HRV more than 10 ms below 7-day average — reduce intensity.")
    if readiness is not None and readiness < 30:
        flags.append("FLAG: Training readiness low — easy or rest only.")
    if acwr is not None and acwr > 1.30:
        flags.append("FLAG: ACWR above optimal — injury risk rising.")
    if acwr is not None and acwr < 0.80:
        flags.append("FLAG: ACWR below optimal — training stimulus too low.")
    if decoupling is not None and decoupling > 10:
        flags.append("FLAG: Aerobic decoupling above 10% — endurance durability issue.")
    if not flags:
        brief.note("  None.", 4)
    else:
        for f in flags:
            brief.note(f"  {f}", 4)
    ctx["flags"] = flags
# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def export_dashboard_json(ctx, today, output_dir):
    data = {
        "generated_at": datetime.now().isoformat(),
        "date": today.isoformat(),
        "sleep": ctx.get("sleep_json", {}),
        "readiness": ctx.get("readiness_json", {}),
        "activities": ctx.get("activities_json", []),
        "load": ctx.get("training_load", {}),
        "verdict": ctx.get("verdict_str", "UNKNOWN"),
        "projection": ctx.get("projection_json", {}),
        "flags": ctx.get("flags", []),
    }
    path = os.path.join(output_dir, "dashboard_data.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Dashboard JSON written to: {path}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    email    = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        print("ERROR: Set GARMIN_EMAIL and GARMIN_PASSWORD."); sys.exit(1)

    try:
        from garminconnect import Garmin
    except ImportError:
        print("ERROR: pip install garminconnect"); sys.exit(1)

    print("Connecting to Garmin Connect...")
    try:
        client = Garmin(email, password, prompt_mfa=lambda: input("MFA code: "))
        client.login(os.path.expanduser("~/.garth"))
    except Exception as e:
        print(f"ERROR: {e}"); sys.exit(1)
    print("Authenticated.")

    today = date.today()
    ctx   = {}

    brief = Brief()
    brief._lines += [
        "=" * 72,
        "  GARMIN DAILY COACHING BRIEF",
        f"  {today.strftime('%A, %d %B %Y')}",
        "=" * 72,
    ]

    print("Fetching sleep...");      section_sleep(client, today, brief, ctx)
    print("Fetching readiness...");  section_readiness(client, today, brief, ctx)
    print("Fetching today...");      section_today(client, today, brief, ctx)
    print("Building verdict...");    verdict = section_verdict(brief, ctx)
    print("Building projection..."); section_recovery_projection(brief, ctx)

    brief._lines += [
        "",
        "=" * 72,
        f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 72,
        "",
    ]

    out = brief.render()
    output_file = os.path.join(OUTPUT_DIR, f"garmin_daily_{today.isoformat()}.txt")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"\nBrief written to: {output_file}")

    # Verdict marker for batch file to read (single line: RED/AMBER/GREEN/UNKNOWN)
    verdict_file = os.path.join(OUTPUT_DIR, "garmin_daily_verdict.txt")
    with open(verdict_file, "w", encoding="utf-8") as f:
        f.write(verdict)
    print(f"Verdict: {verdict}")
    export_dashboard_json(ctx, today, OUTPUT_DIR)

if __name__ == "__main__":
    main()

