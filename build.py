"""Build dashboard.html from the JSON files in data/.

  python build.py            build and open in your browser
  python build.py --no-open  build only
  python build.py --public   build the public copy (site/index.html) with
                             place names removed from activity names
"""
import json
import re
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "dashboard.html"

DEFAULT_CONFIG = {
    "title": "My Training",
    "race": {"name": "Half Marathon", "date": "2027-01-03",
             "distance_km": 21.0975, "long_run_target_km": 18},
    "goal": "A faster 800m",
    "units": "km",
    "default_theme": "watch",
}


def load(name, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return default


def dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def sport_group(type_key):
    t = (type_key or "").lower()
    if "run" in t:
        return "run"
    if "cycl" in t or "bik" in t or "ride" in t:
        return "ride"
    if "hik" in t:
        return "hike"
    if "walk" in t:
        return "walk"
    if any(s in t for s in ("soccer", "football", "hockey")):
        return "team"
    return "other"


SPORT_WORD = {"run": "Run", "ride": "Ride", "hike": "Hike", "walk": "Walk", "team": "Match", "other": "Workout"}
GENERIC = re.compile(r"\b(running|run|cycling|ride|hike|hiking|trek|walk|walking)\s*$", re.I)


def scrub_name(name, places, sport):
    """Remove place names so a public page doesn't reveal where you train."""
    n = (name or "").strip()
    if GENERIC.search(n):  # "Pilani Running", "Triund Hike" -> "Run", "Hike"
        return SPORT_WORD.get(sport, "Workout")
    for p in sorted(places, key=len, reverse=True):
        n = re.sub(r"\b" + re.escape(p) + r"\b", "", n, flags=re.I)
    n = re.sub(r"\s{2,}", " ", re.sub(r"^[\s\-–·,]+|[\s\-–·,]+$", "", n)).strip()
    return (n[:1].upper() + n[1:]) if n else SPORT_WORD.get(sport, "Workout")


def num(v, dp=None):
    if v is None or isinstance(v, bool):
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, dp) if dp is not None else v


def slim_activity(a):
    tk = dig(a, "activityType", "typeKey")
    rec = {
        "d": (a.get("startTimeLocal") or "")[:10],
        "t": (a.get("startTimeLocal") or "")[11:16],
        "s": sport_group(tk),
        "k": tk,
        "n": a.get("activityName") or "",
        "km": num((a.get("distance") or 0) / 1000, 3),
        "sec": num(a.get("duration"), 0),
        "el": num(a.get("elevationGain"), 0),
        "cad": num(a.get("averageRunningCadenceInStepsPerMinute"), 1),
        "sl": num(a.get("avgStrideLength"), 1),
        "mx": num(a.get("maxSpeed"), 3),
        "hr": num(a.get("averageHR"), 0),
        "mhr": num(a.get("maxHR"), 0),
        "ld": num(a.get("activityTrainingLoad"), 0),
        "te": num(a.get("aerobicTrainingEffect"), 1),
        "cal": num(a.get("calories"), 0),
        "laps": a.get("lapCount"),
        "f1k": num(a.get("fastestSplit_1000"), 1),
        "fmi": num(a.get("fastestSplit_1609"), 1),
        "f5k": num(a.get("fastestSplit_5000"), 1),
        "f10k": num(a.get("fastestSplit_10000"), 1),
        "fhm": num(a.get("fastestSplit_21098"), 1),
    }
    for z in range(1, 6):
        v = num(a.get(f"hrTimeInZone_{z}"), 0)
        if v:
            rec[f"z{z}"] = v
    return {k: v for k, v in rec.items() if v not in (None, "", 0) or k in ("km", "sec")}


def series(store, fn):
    out = []
    for d, it in sorted(store.items()):
        try:
            v = fn(it)
        except Exception:
            v = None
        if v not in (None, 0):
            out.append([d, v])
    return out


def main():
    cfg_path = ROOT / "config.json"
    cfg = DEFAULT_CONFIG
    if cfg_path.exists():
        try:
            cfg = {**DEFAULT_CONFIG, **json.loads(cfg_path.read_text(encoding="utf-8"))}
        except ValueError:
            print("Warning: config.json is not valid JSON, using defaults.")

    public = "--public" in sys.argv
    raw = [a for a in load("activities.json", []) if a.get("startTimeLocal")]
    acts = [slim_activity(a) for a in raw]
    if public:
        places = {a.get("locationName").strip() for a in raw if (a.get("locationName") or "").strip()}
        for rec in acts:
            rec["n"] = scrub_name(rec["n"], places, rec["s"])
    if not acts:
        print("No activities found in data/. Run pull.py first.")
        sys.exit(1)

    daily = load("daily.json", {})
    sleep = load("sleep.json", {})
    hrv = load("hrv.json", {})
    weight = load("weight.json", {})
    meta = load("meta.json", {})

    def sleep_hours(it):
        s = it.get("sleepTimeSeconds") or dig(it, "dailySleepDTO", "sleepTimeSeconds")
        return round(s / 3600, 2) if s else None

    def sleep_score(it):
        return (dig(it, "sleepScores", "overall", "value")
                or dig(it, "dailySleepDTO", "sleepScores", "overall", "value"))

    payload = {
        "generated": datetime.now().astimezone().isoformat(timespec="minutes"),
        "public": public,
        "lastPull": meta.get("last_pull"),
        "config": cfg,
        "acts": acts,
        "rhr": series(daily, lambda it: dig(it, "heartRate", "restingValue")),
        "stress": series(daily, lambda it: dig(it, "stress", "avgLevel")),
        "steps": series(daily, lambda it: dig(it, "movement", "steps", "value")),
        "hrv": series(hrv, lambda it: it.get("lastNightAvg")),
        "sleep": series(sleep, sleep_hours),
        "sleepScore": series(sleep, sleep_score),
        "weight": series(weight, lambda it: round(dig(it, "latestWeight", "weight") / 1000, 1)),
    }
    plan_path = ROOT / "plan.json"
    if plan_path.exists():
        try:
            payload["plan"] = json.loads(plan_path.read_text(encoding="utf-8"))
        except ValueError:
            print("Warning: plan.json is not valid JSON, skipping the plan panel.")

    data_js = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    html = template.replace("/*__DATA__*/null", data_js).replace("__TITLE__", cfg.get("title", "My Training"))
    out = ROOT / "site" / "index.html" if public else OUT
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Built {out} from {len(acts)} activities{' (place names removed)' if public else ''}.")
    if not public and "--no-open" not in sys.argv:
        webbrowser.open(OUT.as_uri())


if __name__ == "__main__":
    main()
