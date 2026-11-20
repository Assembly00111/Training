"""Download your Garmin history into data/ as JSON.

  python pull.py          last 12 months (or just what's new since last run)
  python pull.py --full   your full history

Re-running only fetches what is new. Read-only: nothing in Garmin is changed.
"""
import json
import sys
from datetime import date, datetime, timedelta

from garmin import DATA, GarminError, TooBig, get

TODAY = date.today()
META_FILE = DATA / "meta.json"
OVERLAP = 3  # always re-fetch the last few days (today is still in progress)


# ---------- small helpers ----------
def load(name, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return default


def save(name, obj):
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    tmp.replace(DATA / name)


def d2s(d):
    return d.isoformat()


def s2d(s):
    return date.fromisoformat(s[:10])


def bar(label, done, total, extra=""):
    total = max(total, 1)
    filled = int(28 * min(done, total) / total)
    sys.stdout.write(f"\r  {label:<16} [{'#' * filled}{'.' * (28 - filled)}] {extra:<28}")
    sys.stdout.flush()


def dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def item_date(it):
    for k in ("calendarDate", "summaryDate", "date"):
        v = it.get(k) if isinstance(it, dict) else None
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    for sub in ("dailySleepDTO", "dailySleepSummary"):
        v = dig(it, sub, "calendarDate")
        if v:
            return v[:10]
    return None


def has_values(obj):
    """True if a dict/list holds any non-empty, non-zero value."""
    if isinstance(obj, dict):
        return any(has_values(v) for k, v in obj.items()
                   if k not in ("calendarDate", "uuid", "userProfilePk", "source",
                                "includesWellnessData", "includesActivityData"))
    if isinstance(obj, list):
        return any(has_values(v) for v in obj)
    if isinstance(obj, bool):
        return False
    if isinstance(obj, (int, float)):
        return obj != 0
    return obj not in (None, "")


# ---------- activities ----------
def pull_activities(meta, want_from):
    store = {str(a["activityId"]): a for a in load("activities.json", [])}
    covered_from = meta.get("activities_from")
    backfill = covered_from is None or want_from < covered_from
    start, new = 0, 0
    while True:
        page = get(f"/activitylist-service/activities/search/activities?start={start}&limit=20") or []
        stop = False
        for a in page:
            aid = str(a.get("activityId"))
            day = (a.get("startTimeLocal") or "9999")[:10]
            if day < want_from:
                stop = True
                break
            if aid in store and not backfill:
                stop = True
                break
            if aid not in store:
                new += 1
            store[aid] = a
        bar("Activities", 1, 1 if (stop or len(page) < 20) else 2, f"{len(store)} total, {new} new")
        if stop or len(page) < 20:
            break
        start += 20
    print()
    acts = sorted(store.values(), key=lambda a: a.get("startTimeLocal") or "", reverse=True)
    save("activities.json", acts)
    if acts and want_from == "0000-00-00":
        meta["activities_from"] = "0000-00-00"
    elif covered_from is None or want_from < covered_from:
        meta["activities_from"] = want_from
    return acts


# ---------- date-ranged series ----------
def fetch_range(path_fn, extract, a, b, chunk, progress):
    """Fetch [a, b] in chunks, halving the chunk on 'too big' errors."""
    out = []
    cur = a
    while cur <= b:
        size = chunk
        while True:
            end = min(cur + timedelta(days=size - 1), b)
            try:
                r = get(path_fn(cur, end))
                break
            except TooBig:
                if size == 1:
                    r = None
                    break
                size = max(1, size // 2)
        out.extend(extract(r) or [])
        progress(end)
        cur = end + timedelta(days=1)
    return out


def pull_series(meta, name, label, path_fn, extract, chunk, want_from):
    store = load(f"{name}.json", {})
    key = f"{name}_range"
    rng = meta.get(key)
    want = s2d(want_from)
    jobs = []
    if rng is None:
        jobs.append((want, TODAY))
    else:
        have_from, have_to = s2d(rng[0]), s2d(rng[1])
        if want < have_from:
            jobs.append((want, have_from - timedelta(days=1)))
        jobs.append((max(want, have_to - timedelta(days=OVERLAP)), TODAY))
    total = sum((b - a).days + 1 for a, b in jobs)
    done = [0]
    base = [0]

    for a, b in jobs:
        def progress(end, a=a):
            done[0] = base[0] + (end - a).days + 1
            bar(label, done[0], total, f"up to {end}")
        items = fetch_range(path_fn, extract, a, b, chunk, progress)
        base[0] += (b - a).days + 1
        for it in items:
            d = item_date(it)
            if d and has_values(it):
                store[d] = it
    bar(label, 1, 1, f"{len(store)} days with data")
    print()
    save(f"{name}.json", dict(sorted(store.items())))
    new_from = want if rng is None else min(want, s2d(rng[0]))
    meta[key] = [d2s(new_from), d2s(TODAY)]
    return store


def as_list(r, *keys):
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        for k in keys:
            if isinstance(r.get(k), list):
                return r[k]
    return []


def main():
    full = "--full" in sys.argv
    DATA.mkdir(exist_ok=True)
    meta = load("meta.json", {})
    if full:
        meta["mode"] = "full"
    mode = meta.get("mode", "12m")

    print(f"Pulling Garmin data ({'full history' if mode == 'full' else 'last 12 months'})...\n")
    try:
        want_from = "0000-00-00" if mode == "full" else d2s(TODAY - timedelta(days=365))
        acts = pull_activities(meta, want_from)
        save("meta.json", meta)

        if mode == "full":
            oldest = min((a.get("startTimeLocal", "")[:10] for a in acts if a.get("startTimeLocal")),
                         default=d2s(TODAY - timedelta(days=365)))
            series_from = min(oldest, d2s(TODAY - timedelta(days=365)))
        else:
            series_from = want_from

        pull_series(meta, "daily", "Daily summaries",
                    lambda a, b: f"/usersummary-service/v2/dailysummaries?startDate={a}&endDate={b}",
                    lambda r: as_list(r, "data"), 60, series_from)
        save("meta.json", meta)
        pull_series(meta, "sleep", "Sleep",
                    lambda a, b: f"/sleep-service/sleep/dailySleepSummariesByDate?startDate={a}&endDate={b}",
                    lambda r: as_list(r, "data", "dailySleepSummaries"), 31, series_from)
        save("meta.json", meta)
        pull_series(meta, "hrv", "HRV",
                    lambda a, b: f"/hrv-service/hrv/daily/{a}/{b}",
                    lambda r: as_list(r, "hrvSummaries"), 365, series_from)
        save("meta.json", meta)
        pull_series(meta, "weight", "Weight",
                    lambda a, b: f"/weight-service/weight/range/{a}/{b}",
                    lambda r: as_list(r, "dailyWeightSummaries"), 365, series_from)
        save("meta.json", meta)

        bar("Fitness age, zones", 0, 2)
        fa = get(f"/fitnessage-service/fitnessage/v2/{d2s(TODAY)}")
        if fa:
            save("fitness_age.json", fa)
        bar("Fitness age, zones", 1, 2)
        zones = get("/biometric-service/heartRateZones/v2")
        if zones:
            save("hr_zones.json", zones)
        bar("Fitness age, zones", 2, 2, "done")
        print()
    except GarminError as e:
        print(f"\n\nStopped: {e}")
        print("Everything fetched so far is saved; re-run to continue.")
        save("meta.json", meta)
        sys.exit(1)

    meta["last_pull"] = datetime.now().astimezone().isoformat(timespec="seconds")
    save("meta.json", meta)

    # ---- summary ----
    daily, sleep = load("daily.json", {}), load("sleep.json", {})
    hrv, weight = load("hrv.json", {}), load("weight.json", {})
    dates = [a["startTimeLocal"][:10] for a in acts if a.get("startTimeLocal")]
    print("\nDone! Here's what you have:")
    print(f"  Activities:       {len(acts)}"
          + (f"  ({min(dates)} to {max(dates)})" if dates else ""))
    print(f"  Days of summaries:{len(daily):>4}")
    print(f"  Nights of sleep:  {len(sleep)}")
    print(f"  HRV readings:     {len(hrv)}")
    print(f"  Weigh-ins:        {len(weight)}")


if __name__ == "__main__":
    main()
