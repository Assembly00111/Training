"""Import a training plan written in Markdown into plan.json.

  python plan_import.py "<plan>.md"            read, check, preview, then ask to save
  python plan_import.py "<plan>.md" --no-ai    skip the second (AI) reader

How it stays reliable:
  1. Code reads the plan's tables. Whatever it reads is exact; anything it can't
     read is reported, never guessed.
  2. A local AI model (Ollama, qwen3 by default) reads each week again on its
     own. Where the two readers disagree, the preview flags it.
  3. Rule checks run on the result: the plan's own weekly totals, one session
     per day, dates inside their week, races where the text says, and so on.
  4. Nothing is saved until you look at the preview and type "y".
Standard library only. The plan never leaves this computer except as the final
plan.json saved to your GitHub repository.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLAN_FILE = ROOT / "plan.json"
PREVIEW_FILE = ROOT / "data" / "plan_preview.html"
BACKUP_DIR = ROOT / "data" / "plan_backups"
OLLAMA = "http://127.0.0.1:11434"

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
DAY_RE = r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)"
EASY_PACE_MIN = 6.5      # min/km, used only to turn "10 min easy" into a distance
RACE_PACE_MIN = 5.5
WARMUP_KM_PHASE2 = 3.0   # Phase 2 speed sessions: 10 min jog + drills + strides + cool-down


# ---------------------------------------------------------------- utilities
def clean(s):
    """Strip Markdown emphasis, escapes and extra spaces from a cell."""
    s = re.sub(r"\*\*|__|`", "", s or "")
    s = s.replace("\\~", "~").replace("\\|", "|").replace("\\", "")
    return re.sub(r"\s+", " ", s).strip()


def parse_day_month(text, year_hint):
    """Find 'Nov 24' or '24 Nov' in text -> (month, day) or None."""
    m = re.search(r"\b([A-Z][a-z]{2})[a-z]*\.?\s+(\d{1,2})\b", text)
    if m and m.group(1).lower() in MONTHS:
        return MONTHS[m.group(1).lower()], int(m.group(2))
    m = re.search(r"\b(\d{1,2})\s+([A-Z][a-z]{2})[a-z]*\b", text)
    if m and m.group(2).lower() in MONTHS:
        return MONTHS[m.group(2).lower()], int(m.group(1))
    return None


class YearTracker:
    """Gives dates a year, rolling over when months go backwards (Dec -> Jan)."""
    def __init__(self, start):
        self.last = start

    def date(self, month, day):
        y = self.last.year
        d = date(y, month, day)
        if d < self.last - timedelta(days=60):
            d = date(y + 1, month, day)
        self.last = max(self.last, d)
        return d


def monday(d):
    return d - timedelta(days=d.weekday())


def pace_of(term):
    m = re.search(r"@\s*(\d{1,2}):(\d{2})\b", term)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 60
    if "race pace" in term:
        return RACE_PACE_MIN
    return EASY_PACE_MIN


def split_top(s, sep="+"):
    out, depth, cur = [], 0, ""
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if ch == sep and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [p.strip() for p in out if p.strip()]


def term_km(term):
    t = term.strip()
    m = re.match(r"^(\d+)\s*x\s*\((.*)\)", t)
    if m:
        inner = sum_km(m.group(2))
        return int(m.group(1)) * inner if inner else None
    m = re.match(r"^(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(km|m)\b", t)
    if m:
        return int(m.group(1)) * float(m.group(2)) * (1 if m.group(3) == "km" else 0.001)
    m = re.match(r"^(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*min\b", t)
    if m:
        return int(m.group(1)) * float(m.group(2)) / pace_of(t)
    m = re.match(r"^(\d+)\s*x\s*(\d{3,4})\b", t)
    if m:
        return int(m.group(1)) * int(m.group(2)) / 1000
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(km|m)\b", t)
    if m:
        return float(m.group(1)) * (1 if m.group(2) == "km" else 0.001)
    m = re.match(r"^(\d{3,4})\s*@", t)
    if m:
        return int(m.group(1)) / 1000
    m = re.match(r"^(\d+(?:\.\d+)?)\s*min\b", t)
    if m:
        return float(m.group(1)) / pace_of(t)
    return None


def sum_km(s):
    parts = [term_km(p) for p in split_top(s)]
    known = [p for p in parts if p is not None]
    return sum(known) if known else None


def estimate_km(text):
    """Distance of the running described in a cell, e.g. '2 km + 5 km @ 5:35 + 1 km' -> 8."""
    t = clean(text).lower().replace("×", "x").replace("−", "-")
    t = re.sub(r"^[a-z][a-z \-]{0,30}:\s*", "", t)  # drop a leading label such as "checkpoint:"
    t = re.sub(r"\(([^()]*)\)", lambda m: m.group(0) if "+" in m.group(1) else "", t)  # drop '(5:02)', keep grouped reps
    head = split_top(t, ",")[0] if t else ""
    km = sum_km(head)
    return round(km, 1) if km else None


# ---------------------------------------------------------------- Markdown structure
def sections(md):
    """[(level, heading, start_index)] for every heading."""
    return [(len(m.group(1)), m.group(2).strip(), m.start()) for m in re.finditer(r"^(#{1,6})\s+(.+)$", md, re.M)]


def heading_at(secs, pos, level=2):
    h = ""
    for lv, text, start in secs:
        if start > pos:
            break
        if lv <= level:
            h = text
    return h


def tables(md):
    """Every Markdown table: {'header': [...], 'rows': [[...]], 'pos': index}."""
    out, lines, pos = [], md.split("\n"), 0
    i = 0
    offsets = []
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            cells = lambda ln: [c.strip() for c in re.split(r"(?<!\\)\|", ln.strip().strip("|"))]
            header = [clean(c) for c in cells(lines[i])]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(cells(lines[j]))
                j += 1
            out.append({"header": header, "rows": rows, "pos": offsets[i]})
            i = j
        else:
            i += 1
    return out


def column_day(h):
    m = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)", h.lower())
    return m.group(1).title() if m else None


def column_hint(h):
    h = h.lower()
    if "long" in h:
        return "long"
    if "tempo" in h:
        return "tempo"
    if re.search(r"interval|speed|specific|track|reps?\b", h):
        return "intervals"
    if "easy" in h:
        return "easy"
    return None


def classify(cell, hint):
    t = clean(cell).lower()
    if re.search(r"\brace\b", t) and "race pace" not in t:
        return "race"
    if hint == "long":
        return "long"
    if re.search(r"\beasy\b", t) and not re.search(r"@|×|\bx\s*\d|\d\s*x\b", t):
        return "easy"
    if hint:
        return hint
    return "intervals" if re.search(r"@|×", t) else "easy"


# ---------------------------------------------------------------- code reader
def read_plan(md):
    secs = sections(md)
    issues, notes_rules = [], []
    m = re.search(r"\b([A-Z][a-z]{2}) (\d{1,2}), (\d{4})\b", md[:600])
    start = date(int(m.group(3)), MONTHS[m.group(1).lower()], int(m.group(2))) if m else date.today()
    years = YearTracker(start)

    # prose rules that change sessions
    optional_rules = []  # (phase heading, week_from, week_to, {days})
    for mm in re.finditer(rf"Weeks? (\d+)\s*[–-]\s*(\d+)[^:\n]*:\**\s*({DAY_RE}(?:\s*,\s*{DAY_RE})*(?:,?\s+and\s+{DAY_RE})?)\s+(?:are|is)\s+optional", md):
        optional_rules.append((heading_at(secs, mm.start()), int(mm.group(1)), int(mm.group(2)), set(re.findall(DAY_RE, mm.group(3)))))
    layout_rules = {}  # phase heading -> [(day, km)]
    for mm in re.finditer(r"Weekly layout:([^\n]+)", md):
        items = re.findall(rf"({DAY_RE}) easy (\d+(?:\.\d+)?) ?km", mm.group(1))
        if items:
            layout_rules[heading_at(secs, mm.start())] = [(d, float(k)) for d, k in items]
    extra_rules = []  # (phase heading, day, week_from, week_to, text)
    for mm in re.finditer(rf"Add (\d+\s*[×x]\s*\d+\s*m [a-z ]+?) at the end of ({DAY_RE})[a-z]* in weeks (\d+)\s*[–-]\s*(\d+)", md):
        extra_rules.append((heading_at(secs, mm.start()), mm.group(2), int(mm.group(3)), int(mm.group(4)), clean(mm.group(1))))
    race_around = None
    mm = re.search(r"race (?:around|on) (\d{1,2}) ([A-Z][a-z]+)", md)
    if mm and mm.group(2)[:3].lower() in MONTHS:
        race_around = (MONTHS[mm.group(2)[:3].lower()], int(mm.group(1)))

    weeks, sessions = [], []
    phase_no = 0
    for tb in tables(md):
        hdr = tb["header"]
        week_col = next((i for i, h in enumerate(hdr) if re.match(r"^week\b", h, re.I)), None)
        day_cols = [(i, column_day(h), column_hint(h)) for i, h in enumerate(hdr) if column_day(h)]
        if week_col is None or len(day_cols) < 2:
            continue
        heading = heading_at(secs, tb["pos"])
        hm = re.search(r"Phase\s*(\d+)", heading)
        phase_no = int(hm.group(1)) if hm else phase_no + 1
        total_col = next((i for i, h in enumerate(hdr) if re.match(r"^total", h, re.I)), None)
        for r_i, row in enumerate(tb["rows"]):
            label = clean(row[week_col]) if week_col < len(row) else ""
            wkm = re.match(r"^\s*(?:week\s*)?(\d+)", label, re.I)
            dm = parse_day_month(label, start.year)
            if not wkm or not dm:
                issues.append(("error", f"{heading}: couldn't read the week label '{label}' (row {r_i + 1}). That row was skipped."))
                continue
            wk = int(wkm.group(1))
            wk_start = monday(years.date(*dm))
            row_sessions = []
            for col, day, hint in day_cols:
                cell = row[col] if col < len(row) else ""
                text = clean(cell)
                if not text or re.fullmatch(r"[—–\-]+|rest\.?|off", text, re.I):
                    continue
                kind = classify(cell, hint)
                recovery = "recovery" in label.lower()
                if recovery and kind != "race":
                    kind = "easy"  # a recovery week is all easy running
                d = wk_start + timedelta(days=DAYS.index(day))
                if kind == "race":
                    rd = parse_day_month(text, start.year)
                    named_day = re.search(DAY_RE, text)
                    if rd:
                        d = years.date(*rd)
                    elif named_day:
                        d = wk_start + timedelta(days=DAYS.index(named_day.group(0)))
                    elif race_around and wk_start <= date(wk_start.year, *race_around) <= wk_start + timedelta(days=6):
                        d = date(wk_start.year, *race_around)
                    elif race_around and wk_start <= date(wk_start.year + 1, *race_around) <= wk_start + timedelta(days=6):
                        d = date(wk_start.year + 1, *race_around)
                s = {"d": d.isoformat(), "ph": phase_no, "wk": wk, "type": kind, "title": text, "day": DAYS[d.weekday()],
                     "km": estimate_km(text), "_bold": re.findall(r"\*\*([^*]+)\*\*", cell), "_src": "table"}
                if re.search(r"\(skip if|optional", text, re.I):
                    s["optional"] = True
                # a bare distance ("4 km", "10 km easy") reads better as a named run
                if kind in ("easy", "long") and re.fullmatch(r"\d+(?:\.\d+)?\s*km(?: easy)?", text, re.I):
                    s["title"] = ("Easy run" if kind == "easy" else "Long run" + (", easy" if "easy" in text.lower() else "")) + (" (recovery)" if recovery else "")
                row_sessions.append(s)
            # prose rules
            for d_name, km in layout_rules.get(heading, []):
                if "recovery" in label.lower() or any(s["day"] == d_name for s in row_sessions):
                    continue
                d = wk_start + timedelta(days=DAYS.index(d_name))
                row_sessions.append({"d": d.isoformat(), "ph": phase_no, "wk": wk, "type": "easy", "title": "Easy run",
                                     "day": d_name, "km": km, "_bold": [], "_src": "layout"})
            for h, d_name, w0, w1, extra in extra_rules:
                if h == heading and w0 <= wk <= w1:
                    for s in row_sessions:
                        if s["day"] == d_name and s["type"] == "intervals":
                            s["title"] += f" + {extra}"
                            s["_extra_km"] = estimate_km(extra) or 0
            for h, w0, w1, days in optional_rules:
                if h == heading and w0 <= wk <= w1:
                    for s in row_sessions:
                        if s["day"] in days:
                            s["optional"] = True
            # weekly total check (the plan's own "Total" column)
            total = None
            if total_col is not None and total_col < len(row):
                tm = re.search(r"(\d+(?:\.\d+)?)\s*km", clean(row[total_col]))
                total = float(tm.group(1)) if tm else None
            weeks.append({"phase": phase_no, "wk": wk, "label": label, "start": wk_start, "heading": heading,
                          "header": hdr, "row": [clean(c) for c in row], "sessions": row_sessions, "total": total})
            sessions += row_sessions

    # finish sessions: easy in Phase 2+ uses the Phase 2 easy pace, distances, keys
    half_goal = re.search(r"Goal for ([A-Z][a-z]{2,8} \d{1,2})", md)
    for s in sessions:
        if s["type"] == "easy" and s["ph"] >= 2:
            s["type"] = "easy2"
        if s["type"] == "race":
            is_half = s["ph"] == 1 or "half" in heading_for_phase(weeks, s["ph"]).lower()
            s["km"] = 21.1 if is_half else round(0.8 + WARMUP_KM_PHASE2, 1)
            s["key"] = "Half marathon" if is_half else ("800 m race" if "800" in heading_for_phase(weeks, s["ph"]) else "Race")
            s["title"] = "Half marathon race" if is_half else ("800 m race" + (" (date to confirm)" if race_around else ""))
        else:
            if s["km"] is None:
                issues.append(("warn", f"{s['d']}: couldn't work out the distance of '{s['title']}'. Set to 0 km; fix it in Edit plan."))
                s["km"] = 0
            if s["type"] == "intervals" and s["ph"] >= 2:
                s["km"] = round(s["km"] + WARMUP_KM_PHASE2 + s.get("_extra_km", 0), 1)
            for b in s["_bold"]:
                b = clean(b).split(",")[0].rstrip(":").strip()
                if b and not re.fullmatch(r"race.*", b, re.I):
                    s["key"] = b if len(b) > 12 else f"{b} {TYPE_WORD.get(s['type'], '')}".strip()
    return {"start": start, "weeks": weeks, "sessions": sessions, "issues": issues}


TYPE_WORD = {"tempo": "tempo", "long": "long run", "intervals": "session", "easy": "run", "easy2": "run"}


def heading_for_phase(weeks, ph):
    return next((w["heading"] for w in weeks if w["phase"] == ph), "")


def read_settings(md):
    """Goals, checkpoint, paces, cadence target and coach notes, read by code."""
    out, found = {}, {}
    m = re.search(r"Goal for ([A-Z][a-z]{2,8} \d{1,2}):\s*\**\s*(\d:\d{2}(?::\d{2})?)", md)
    if m:
        g = m.group(2)
        out["half_goal"] = g if g.count(":") == 2 else g + ":00"
    m = re.search(r"\*\*Goal:\s*(\d:\d{2})[^*\n]*?stretch\s*(\d:\d{2})", md)
    if m:
        out["goal_800"], out["stretch_800"] = m.group(1), m.group(2)
    m = re.search(r"[Cc]heckpoint (passed|failed|not passed) on (\d{1,2} [A-Z][a-z]{2,8}|[A-Z][a-z]{2,8} \d{1,2})", md)
    if m:
        out["checkpoint"] = "passed" if m.group(1) == "passed" else "failed"
        out["checkpoint_date"] = m.group(2)
    m = re.search(r"[Aa]im for (\d{3})\s*[–-]\s*(\d{3})", md)
    if m:
        out["cadence"] = [int(m.group(1)), int(m.group(2))]
    paces = {}
    for tb in tables(md):
        hdr = [h.lower() for h in tb["header"]]
        if hdr and hdr[0].startswith("session") and len(hdr) >= 3:
            for row in tb["rows"]:
                name = clean(row[0]).lower()
                key = "easy" if name.startswith("easy") else "long" if name.startswith("long") else "tempo" if name.startswith("tempo") else None
                r1, r2 = (parse_range(row[1]) if len(row) > 1 else None), (parse_range(row[2]) if len(row) > 2 else None)
                if key and r1 and r2:
                    paces[key] = {"p200": r1, "p155": r2}
        elif hdr and hdr[0].startswith("rep"):
            for row in tb["rows"]:
                if clean(row[0]).lower().startswith("easy") and len(row) > 1 and parse_range(row[1]):
                    r = parse_range(row[1])
                    paces["easy2"] = {"p200": r, "p155": parse_range(row[2]) if len(row) > 2 and parse_range(row[2]) else r}
    out["paces"] = paces
    notes = []
    for mm in re.finditer(r"^\*\*([^*\n]{0,60}?\b(?:result|review)\b[^*\n]{0,40}?):?\*\*:?\s*(.+)$", md, re.M | re.I):
        notes.append(f"{clean(mm.group(1)).rstrip(':')} · {clean(mm.group(2))}")
    out["notes"] = notes
    return out


def parse_range(cell):
    m = re.search(r"(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})", clean(cell))
    if not m:
        return None
    a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
    return sorted([a, b])


# ---------------------------------------------------------------- second reader (local AI)
def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=5) as r:
            return bool(r.read())
    except Exception:
        return False


NVIDIA = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_DEFAULT_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
PRIVACY_NAME = None   # the runner's name, taken from the plan title and swapped out before anything is sent to NVIDIA


def nvidia_key():
    """NVIDIA_API_KEY from the environment or .env.local. Never printed."""
    k = os.environ.get("NVIDIA_API_KEY", "").strip()
    if k:
        return k
    env = ROOT / ".env.local"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith("NVIDIA_API_KEY="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def ask_nvidia(model, prompt, schema, timeout=180):
    if PRIVACY_NAME:
        prompt = re.sub(rf"\b{re.escape(PRIVACY_NAME)}\b('s)?", "the runner", prompt, flags=re.I)
    body = {"model": model, "temperature": 0, "max_tokens": 2048,
            "messages": [{"role": "system", "content": "Answer with JSON only, matching this JSON schema:\n" + json.dumps(schema)},
                         {"role": "user", "content": prompt}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}}
    req = urllib.request.Request(NVIDIA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "Authorization": "Bearer " + nvidia_key()})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"] or ""
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit("\n\n  NVIDIA refused the key (HTTP %d). Check NVIDIA_API_KEY in .env.local, or run with --local." % e.code)
            if e.code == 404:
                raise SystemExit(f"\n\n  NVIDIA doesn't offer the model '{model}'. Change \"import_nvidia_model\" in config.json.")
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def ask_ai(model, prompt, schema, num_ctx=8192, timeout=1800, think=True):
    if model.startswith("nvidia:"):
        return ask_nvidia(model[len("nvidia:"):], prompt, schema)
    body = {"model": model, "prompt": prompt, "format": schema, "stream": False, "think": think,
            "options": {"temperature": 0, "num_ctx": num_ctx}}
    req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(json.loads(r.read())["response"])


def week_schema(days):
    return {"type": "object", "properties": {"sessions": {"type": "array", "items": {"type": "object", "properties": {
        "day": {"type": "string", "enum": days},
        "type": {"type": "string", "enum": ["easy", "long", "tempo", "intervals", "race"]},
        "optional": {"type": "boolean"}}, "required": ["day", "type", "optional"]}}}, "required": ["sessions"]}


def phase_prose(md, heading):
    """The sentences of a phase section that change sessions (layout, optional days, extras, race date)."""
    start = md.find(heading)
    if start < 0:
        return ""
    nxt = re.search(r"^## ", md[start + len(heading):], re.M)
    body = md[start:start + len(heading) + (nxt.start() if nxt else len(md))]
    keep = [ln for ln in body.split("\n") if not ln.lstrip().startswith("|") and re.search(r"optional|Weekly layout|Add \d|race around|race on", ln)]
    return "\n".join(clean(k) for k in keep)


WEEK_RULES = """How to read it:
- Each cell that has text, and is not "rest", "off" or a dash, is ONE run on its column's weekday.
- Type: use the column header. "easy" -> easy, "long" -> long, "tempo" -> tempo, "intervals", "speed" or "specific" -> intervals.
  Exception: a cell that just says "N km easy" in a speed/specific column is easy. In a week labelled "recovery", every run is easy.
- A cell that says "Race" is a race. It happens on the weekday the cell names (e.g. "Race - Sun 3 Jan" -> Sun);
  if the cell names no day, use the date the rules give (e.g. "race around 14 March"), otherwise its column's day.
- A "Weekly layout" rule like "Mon easy 5 km" adds an easy run on that day ONLY if this week has no cell for that day
  and the week is not a recovery week. "rest" or "rest or 20 min jog" in the layout adds NOTHING.
- Never add a run on any other day. Never split one cell into several runs.
- optional = true only if a rule makes that weekday optional for this week number, or the cell says skip/optional."""


def second_reader(md, plan_read, model, progress, think=True):
    weeks = plan_read["weeks"]
    results = []
    for i, w in enumerate(weeks):
        progress(i, len(weeks), f"Phase {w['phase']} week {w['wk']}")
        prose = phase_prose(md, w["heading"])
        cells = [(h, c) for h, c in zip(w["header"], w["row"]) if not re.match(r"^(week|total)", h, re.I)]
        listing = "\n".join(f"- {h}: {c or '(empty)'}" for h, c in cells)
        days = [d for d in DAYS if re.search(rf"\b{d}", " ".join(h for h, _ in cells) + " " + prose + " " + " ".join(c for _, c in cells))]
        prompt = (f"You check one week of a running training plan.\n\nWeek label: {w['label']} (week number {w['wk']})\n"
                  f"This week's cells (column header: cell text):\n{listing}\n\nRules from the plan's text:\n{prose or '(none)'}\n\n"
                  f"{WEEK_RULES}\n\nList this week's runs.")
        try:
            results.append(ask_ai(model, prompt, week_schema(days or DAYS), think=think)["sessions"])
        except Exception:
            results.append(None)
    progress(len(weeks), len(weeks), "goals and checkpoint")

    def para_with(pattern, limit=900):
        m = re.search(pattern, md)
        if not m:
            return ""
        s = md.rfind("\n\n", 0, m.start()) + 2
        e = md.find("\n\n", m.end())
        return clean(md[s:e if e > 0 else None])[:limit]

    settings = {}
    try:
        txt = para_with(r"Goal for ")
        r = ask_ai(model, "From this paragraph of a running plan, give the CURRENT half-marathon goal time (h:mm:ss) and, if the paragraph says "
                          "the goal will be reviewed or changed later, a short note saying so (else empty).\n\n" + txt,
                   {"type": "object", "properties": {"half_goal": {"type": "string"}, "half_goal_note": {"type": "string"}},
                    "required": ["half_goal", "half_goal_note"]}, think=think) if txt else {}
        settings.update(r)
        txt = para_with(r"\*\*Goal:")
        r = ask_ai(model, "From this sentence of an 800 m training plan, give the 800 m goal time and the stretch goal time, as m:ss.\n\n" + txt,
                   {"type": "object", "properties": {"goal_800": {"type": "string"}, "stretch_800": {"type": "string"}},
                    "required": ["goal_800", "stretch_800"]}, think=think) if txt else {}
        settings.update(r)
        lines = "\n".join(clean(ln) for ln in md.split("\n") if re.search(r"checkpoint", ln, re.I) and not ln.lstrip().startswith("|"))
        r = ask_ai(model, "These lines are from a running plan. Has the checkpoint been run yet, and if so, was it passed or failed? "
                          "Give the date it was run (e.g. 26 Nov) or empty.\n\n" + lines,
                   {"type": "object", "properties": {"checkpoint": {"type": "string", "enum": ["passed", "failed", "not_run"]},
                                                     "checkpoint_date": {"type": "string"}}, "required": ["checkpoint", "checkpoint_date"]},
                   think=think) if lines else {}
        settings.update(r)
        txt = para_with(r"[Cc]adence is|[Aa]im for \d")
        r = ask_ai(model, "What cadence target range (steps per minute) does this text recommend? Give the low and high numbers.\n\n" + txt,
                   {"type": "object", "properties": {"cadence_low": {"type": "integer"}, "cadence_high": {"type": "integer"}},
                    "required": ["cadence_low", "cadence_high"]}, think=think) if txt else {}
        settings.update(r)
    except Exception:
        settings = settings or None
    return results, settings


# ---------------------------------------------------------------- checks
def norm_type(t):
    return "easy" if t in ("easy", "easy2") else t


def compare(plan_read, ai_weeks):
    flags = []
    for w, ai in zip(plan_read["weeks"], ai_weeks):
        name = f"Phase {w['phase']} week {w['wk']} ({w['label']})"
        if ai is None:
            flags.append(("warn", f"{name}: the AI reader didn't answer for this week, so it's checked by code only."))
            continue
        code = sorted((s["day"], norm_type(s["type"])) for s in w["sessions"])
        mine = sorted((s["day"], s["type"]) for s in ai)
        if code != mine:
            only_code = [f"{d} {t}" for d, t in code if (d, t) not in mine]
            only_ai = [f"{d} {t}" for d, t in mine if (d, t) not in code]
            flags.append(("attn", f"{name}: the readers disagree. Code read: {', '.join(only_code) or 'nothing extra'}. "
                                  f"AI read: {', '.join(only_ai) or 'nothing extra'}. The code's version is used; check it against the plan."))
        code_opt = sorted(s["day"] for s in w["sessions"] if s.get("optional"))
        ai_opt = sorted(s["day"] for s in ai if s.get("optional"))
        if code_opt != ai_opt:
            flags.append(("minor", f"{name}: optional days differ. Code: {', '.join(code_opt) or 'none'}; AI: {', '.join(ai_opt) or 'none'}."))
    return flags


def rule_checks(plan_read):
    flags = []
    prev_long = None
    for w in plan_read["weeks"]:
        name = f"Phase {w['phase']} week {w['wk']} ({w['label']})"
        days = [s["day"] for s in w["sessions"]]
        for d in set(days):
            if days.count(d) > 1:
                flags.append(("attn", f"{name}: two sessions on {d}."))
        for s in w["sessions"]:
            sd = date.fromisoformat(s["d"])
            if not (w["start"] <= sd <= w["start"] + timedelta(days=6)):
                flags.append(("attn", f"{name}: '{s['title']}' lands on {s['d']}, outside its week."))
        if w["total"]:
            got = sum(s["km"] for s in w["sessions"] if s["type"] != "race" and s["_src"] == "table")
            if abs(got - w["total"]) > max(4, 0.2 * w["total"]):
                flags.append(("attn", f"{name}: sessions add up to {got:.1f} km but the plan's Total says ~{w['total']:g} km. A session may be missing or misread."))
        longs = [s["km"] for s in w["sessions"] if s["type"] == "long"]
        if longs:
            if prev_long and max(longs) - prev_long > 3.01:
                flags.append(("minor", f"{name}: long run jumps {max(longs) - prev_long:.0f} km from last week (the plan's rule is 3 km at most)."))
            prev_long = max(longs)
        if "recovery" in w["label"].lower() or any(s["type"] == "race" for s in w["sessions"]):
            prev_long = None  # a fresh build starts after a race or recovery week
        if not any(s["type"] in ("long", "race") for s in w["sessions"]) and "recovery" not in w["label"].lower():
            flags.append(("minor", f"{name}: no long run or race this week."))
    return flags


def settings_checks(code, ai):
    flags = []
    if ai is None:
        return [("warn", "The AI reader couldn't read the goals and checkpoint, so they're checked by code only.")]
    def norm_t(x):
        parts = re.findall(r"\d+", x or "")
        if len(parts) == 3 and parts[2] == "00":
            parts = parts[:2]
        return ":".join(str(int(p)) if i == 0 else p for i, p in enumerate(parts))

    def same_time(a, b):
        return norm_t(a) == norm_t(b)
    if code.get("half_goal") and not same_time(code["half_goal"], ai.get("half_goal")):
        flags.append(("attn", f"Half-marathon goal: code read {code['half_goal']}, AI read {ai.get('half_goal')}."))
    if not code.get("half_goal") and ai.get("half_goal"):
        flags.append(("attn", f"Half-marathon goal: only the AI found one ({ai['half_goal']}). Not used; set it in Edit plan if it's right."))
    for k in ("goal_800", "stretch_800"):
        if code.get(k) and ai.get(k) and code[k] != ai[k]:
            flags.append(("attn", f"800m {'goal' if k == 'goal_800' else 'stretch goal'}: code read {code[k]}, AI read {ai[k]}."))
    if code.get("checkpoint") and ai.get("checkpoint") and code["checkpoint"] != ai["checkpoint"]:
        flags.append(("attn", f"Checkpoint: code read '{code['checkpoint']}', AI read '{ai['checkpoint']}'."))
    if not code.get("checkpoint") and ai.get("checkpoint") in ("passed", "failed"):
        flags.append(("attn", f"Checkpoint: only the AI found a result ('{ai['checkpoint']}' on {ai.get('checkpoint_date') or '?'}). Not used; set it in Edit plan if it's right."))
    if code.get("cadence") and ai.get("cadence_low") and code["cadence"] != [ai["cadence_low"], ai["cadence_high"]]:
        flags.append(("minor", f"Cadence target: code read {code['cadence']}, AI read {[ai['cadence_low'], ai['cadence_high']]}."))
    return flags


# ---------------------------------------------------------------- build the new plan
def build_plan(current, read, st, ai_settings):
    new = json.loads(json.dumps(current)) if current else {}
    start = read["start"]
    sessions = []
    cur_by_key = {(s["d"], norm_type(s["type"])): s for s in (current or {}).get("sessions", [])}
    for s in sorted(read["sessions"], key=lambda s: s["d"]):
        if date.fromisoformat(s["d"]) < start:
            continue
        out = {"d": s["d"], "type": s["type"], "title": s["title"], "km": s["km"]}
        if s.get("optional"):
            out["optional"] = True
        old = cur_by_key.get((s["d"], norm_type(s["type"])))
        key = s.get("key") or (old or {}).get("key")
        if key:
            out["key"] = key
        sessions.append(out)
    new["sessions"] = sessions
    new["start"] = start.isoformat()
    years = YearTracker(start)
    races = [s for s in sessions if s["type"] == "race"]
    goals = new.setdefault("goals", {})
    half = goals.setdefault("half", {})
    if st.get("half_goal"):
        half["goal"] = st["half_goal"]
    half.pop("a_goal", None)
    if races:
        half["date"] = races[0]["d"]
    note = (ai_settings or {}).get("half_goal_note", "").strip()
    if note:
        half["note"] = note
    e8 = goals.setdefault("800", {})
    if st.get("goal_800"):
        e8["goal"], e8["stretch"] = st["goal_800"], st["stretch_800"]
    if len(races) > 1:
        e8["date"] = races[-1]["d"]
    if st.get("checkpoint"):
        new["checkpoint_passed"] = st["checkpoint"] == "passed"
        dm = parse_day_month(st.get("checkpoint_date", ""), start.year)
        new["fast_from"] = years.date(*dm).isoformat() if dm and st["checkpoint"] == "passed" else None
    if st.get("cadence"):
        new["cadence_target"] = st["cadence"]
    if st.get("paces"):
        new.setdefault("paces", {}).update(st["paces"])
    new["notes"] = st.get("notes", [])
    # phases from the tables
    phases = []
    for ph in sorted({w["phase"] for w in read["weeks"]}):
        ws = [w for w in read["weeks"] if w["phase"] == ph]
        last = max((s["d"] for s in sessions if any(s["d"] == x["d"] for w in ws for x in w["sessions"])), default=None)
        old = next((p for p in (current or {}).get("phases", []) if p.get("n") == ph), {})
        phases.append({"n": ph, "name": old.get("name") or re.sub(r":.*", "", ws[0]["heading"]).strip(),
                       "from": min(w["start"] for w in ws).isoformat(), "to": last or (max(w["start"] for w in ws) + timedelta(days=6)).isoformat(),
                       "first_week": min(w["wk"] for w in ws)})
    if phases:
        new["phases"] = phases
    order = ["name", "start", "checkpoint_passed", "fast_from", "goals", "paces", "cadence_target", "phases", "gym", "notes", "sessions"]
    return {k: new[k] for k in order if k in new} | {k: v for k, v in new.items() if k not in order}


def diff_plans(old, new):
    ch = {"added": [], "removed": [], "changed": [], "settings": []}
    o = {(s["d"], norm_type(s["type"])): s for s in (old or {}).get("sessions", [])}
    n = {(s["d"], norm_type(s["type"])): s for s in new["sessions"]}
    for k in sorted(set(o) | set(n)):
        if k not in o:
            ch["added"].append(n[k])
        elif k not in n:
            ch["removed"].append(o[k])
        else:
            diffs = [f for f in ("title", "km", "optional", "key") if o[k].get(f) != n[k].get(f)]
            if diffs:
                ch["changed"].append((o[k], n[k], diffs))
    def show(v):
        return json.dumps(v, ensure_ascii=False)
    for path in [("goals", "half", "goal"), ("goals", "half", "date"), ("goals", "half", "note"), ("goals", "800", "goal"),
                 ("goals", "800", "stretch"), ("goals", "800", "date"), ("checkpoint_passed",), ("fast_from",), ("cadence_target",), ("paces",), ("notes",), ("start",), ("phases",)]:
        a, b = old or {}, new
        for p in path:
            a = a.get(p) if isinstance(a, dict) else None
            b = b.get(p) if isinstance(b, dict) else None
        if a != b:
            ch["settings"].append((" › ".join(path), show(a), show(b)))
    return ch


# ---------------------------------------------------------------- preview page
def preview_html(src, new, ch, flags, ai_used, model):
    e = html.escape
    sev = {"error": ("Couldn't read", "#e66767"), "attn": ("Check this", "#fab219"), "warn": ("Note", "#8b8a85"), "minor": ("Minor", "#8b8a85")}
    flag_rows = "".join(f'<li><b style="color:{sev[k][1]}">{sev[k][0]}:</b> {e(t)}</li>' for k, t in flags) or '<li style="color:#2fbf4f">No problems found. Both readers agree and every check passed.</li>'
    sess = lambda s: f"{e(s['d'])} · {e(s['type'])} · {e(s['title'])} · {s['km']:g} km{' · optional' if s.get('optional') else ''}{' · ★ ' + e(s['key']) if s.get('key') else ''}"
    changed = "".join(f"<li>{e(a['d'])}: " + "; ".join(f"{e(f)}: <s>{e(str(a.get(f, '')))}</s> → <b>{e(str(b.get(f, '')))}</b>" for f in fs) + "</li>" for a, b, fs in ch["changed"])
    settings = "".join(f"<li><b>{e(p)}</b>: <s>{e(a)}</s> → {e(b)}</li>" for p, a, b in ch["settings"])
    n_attn = sum(1 for k, _ in flags if k in ("attn", "error"))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Plan import preview</title>
<style>body{{background:#08090b;color:#f0efec;font:14px/1.5 system-ui,Segoe UI,sans-serif;max-width:980px;margin:0 auto;padding:24px 16px}}
h1{{font-size:24px;margin:0}} h2{{font-size:16px;margin:28px 0 8px}} .card{{background:#121317;border:1px solid rgba(255,255,255,.08);border-radius:14px;padding:14px 18px;margin-top:12px}}
li{{margin:4px 0}} s{{color:#8b8a85}} .sub{{color:#8b8a85;font-size:13px}} .big{{font-size:22px;font-weight:700}} .row{{display:flex;gap:12px;flex-wrap:wrap}} .row .card{{flex:1;min-width:150px}}</style></head><body>
<h1>Plan import preview</h1><div class="sub">From {e(src)} · second reader: {e(model) if ai_used else 'off'} · nothing is saved yet</div>
<div class="row"><div class="card"><div class="sub">Sessions in new plan</div><div class="big">{len(new['sessions'])}</div></div>
<div class="card"><div class="sub">Added / removed / changed</div><div class="big">{len(ch['added'])} / {len(ch['removed'])} / {len(ch['changed'])}</div></div>
<div class="card"><div class="sub">Needs your attention</div><div class="big" style="color:{'#fab219' if n_attn else '#2fbf4f'}">{n_attn}</div></div></div>
<h2>Checks</h2><div class="card"><ul>{flag_rows}</ul></div>
<h2>Goals and settings that change</h2><div class="card"><ul>{settings or '<li class="sub">No changes</li>'}</ul></div>
<h2>Sessions added</h2><div class="card"><ul>{''.join(f'<li>{sess(s)}</li>' for s in ch['added']) or '<li class="sub">None</li>'}</ul></div>
<h2>Sessions removed</h2><div class="card"><ul>{''.join(f'<li>{sess(s)}</li>' for s in ch['removed']) or '<li class="sub">None</li>'}</ul></div>
<h2>Sessions changed</h2><div class="card"><ul>{changed or '<li class="sub">None</li>'}</ul></div>
<p class="sub">Go back to the black window and type y to save this plan, or n to cancel. You can still fine-tune anything afterwards with Edit plan on the dashboard.</p>
</body></html>"""


# ---------------------------------------------------------------- main
def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def bar(i, n, label):
    filled = int(24 * i / max(n, 1))
    sys.stdout.write(f"\r  Second reader   [{'#' * filled}{'.' * (24 - filled)}] {label:<26}")
    sys.stdout.flush()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Drag your plan's .md file onto import_plan.bat, or run: python plan_import.py <plan.md>")
        sys.exit(1)
    src = Path(args[0])
    md = src.read_text(encoding="utf-8")
    cfg = {}
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    model = cfg.get("import_model", "qwen3:8b")
    global PRIVACY_NAME
    t = re.search(r"^#\s*([A-Z][\w-]+)['’]s\b", md, re.M)
    PRIVACY_NAME = t.group(1) if t else None

    # Which second reader: --local / --nvidia, else ask (when a key is set), else config "import_ai".
    backend = "nvidia" if "--nvidia" in sys.argv else "local" if "--local" in sys.argv else None
    has_key = bool(nvidia_key())
    if backend is None:
        backend = cfg.get("import_ai", "local")
        if has_key and sys.stdin.isatty() and "--dry-run" not in sys.argv:
            print("\nWhich AI should double-check the plan?")
            print("  1  Local qwen3 on this laptop: private, about an hour")
            print("  2  NVIDIA (cloud, free): a minute or two; sends the plan's week tables and goal lines, not your name or diet")
            pick = input(f"Type 1 or 2 (Enter = {'2' if backend == 'nvidia' else '1'}): ").strip()
            backend = "nvidia" if pick == "2" else "local" if pick == "1" else backend
    if backend == "nvidia":
        model = "nvidia:" + cfg.get("import_nvidia_model", NVIDIA_DEFAULT_MODEL)

    print(f"\nImporting {src.name}\n")
    pulled = git("pull", "--ff-only", "-q") if not CI_OUT else None
    if pulled is not None and pulled.returncode != 0:
        print("  (Couldn't fetch the latest plan from GitHub; comparing with the plan on this computer.)")
    current = json.loads(PLAN_FILE.read_text(encoding="utf-8")) if PLAN_FILE.exists() else None

    read = read_plan(md)
    st = read_settings(md)
    print(f"  Code reader     found {len(read['weeks'])} weeks and {len(read['sessions'])} sessions")
    if not read["sessions"]:
        print("\n  No session tables found. The importer looks for tables with a 'Week' column and weekday columns (Mon, Tue...).")
        sys.exit(1)

    flags = list(read["issues"])
    ai_used = False
    ai_settings = None
    if "--no-ai" in sys.argv:
        flags.append(("warn", "Second reader switched off (--no-ai): only the code reader and rule checks were used."))
    elif backend == "nvidia" and not has_key:
        flags.append(("attn", "NVIDIA was chosen but NVIDIA_API_KEY isn't set (in .env.local, or as a secret on GitHub), so there was no second reader."))
        print("  Second reader   No NVIDIA key in .env.local. Continuing with code and rule checks only.")
    elif backend == "local" and not ollama_up():
        flags.append(("attn", "Ollama isn't running, so there was no second reader. Start Ollama and import again for the full check."))
        print("  Second reader   Ollama isn't running. Continuing with code and rule checks only.")
    else:
        t0 = time.time()
        ai_weeks, ai_settings = second_reader(md, read, model, bar, think=cfg.get("import_think", True))
        print(f"\n  Second reader   done in {time.time() - t0:.0f}s")
        flags += compare(read, ai_weeks)
        flags += settings_checks(st, ai_settings)
        ai_used = True
    flags += rule_checks(read)

    new = build_plan(current, read, st, ai_settings)
    ch = diff_plans(current, new)
    order = {"error": 0, "attn": 1, "warn": 2, "minor": 3}
    flags.sort(key=lambda f: order[f[0]])
    PREVIEW_FILE.parent.mkdir(exist_ok=True)
    PREVIEW_FILE.write_text(preview_html(src.name, new, ch, flags, ai_used, model), encoding="utf-8")
    n_attn = sum(1 for k, _ in flags if k in ("attn", "error"))
    print(f"\n  Result          {len(new['sessions'])} sessions · {len(ch['added'])} added, {len(ch['removed'])} removed, {len(ch['changed'])} changed · "
          f"{len(ch['settings'])} settings changed · {n_attn} item(s) to check")
    for k, t in flags:
        if k in ("attn", "error"):
            print(f"    ! {t}")
    print(f"\n  Preview opened in your browser ({PREVIEW_FILE.name}).")
    if "--no-open" not in sys.argv:
        webbrowser.open(PREVIEW_FILE.as_uri())
    if CI_OUT:
        ci_write({"ok": True, "model": model.replace("nvidia:", "") if ai_used else None, "ai_used": ai_used,
                  "flags": flags, "counts": {k: len(v) for k, v in ch.items()},
                  "changes": {"added": ch["added"], "removed": ch["removed"],
                              "changed": [{"old": o, "new": n, "fields": f} for o, n, f in ch["changed"]],
                              "settings": [{"what": w, "old": a, "new": b} for w, a, b in ch["settings"]]},
                  "plan": new})
        print("  Result written for the dashboard.")
        return
    if "--dry-run" in sys.argv:
        json.dump(new, open(ROOT / "data" / "plan_import_result.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print("  Dry run: nothing saved.")
        return

    ans = input("\n  Save this plan and publish it? Type y for yes, anything else to cancel: ").strip().lower()
    if ans != "y":
        print("  Cancelled. Nothing was changed.")
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if current:
        (BACKUP_DIR / f"plan-{datetime.now():%Y%m%d-%H%M%S}.json").write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    PLAN_FILE.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("  Saved plan.json (the old one is backed up in data\\plan_backups).")
    git("add", "plan.json")
    c = git("commit", "-q", "-m", f"Import training plan from {src.name}")
    if c.returncode == 0:
        p = git("push", "-q", "origin", "main")
        print("  Published to GitHub. The live site rebuilds in about 2 minutes." if p.returncode == 0 else
              "  Saved locally, but the upload to GitHub failed. Run refresh.bat later or check your connection.")
    else:
        print("  No changes to publish.")
    subprocess.run([sys.executable, str(ROOT / "build.py"), "--no-open"], cwd=ROOT)


def _opt(name):
    for a in sys.argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


# Cloud mode (used by the GitHub import job): --ci=<result.json> --id=<request id>.
# Never asks anything; writes the result, or the error, to the file for the dashboard to show.
CI_OUT = _opt("ci")
CI_ID = _opt("id")


def ci_write(obj):
    obj = {"id": CI_ID, "at": datetime.now().astimezone().isoformat(timespec="seconds"), **obj}
    Path(CI_OUT).parent.mkdir(parents=True, exist_ok=True)
    Path(CI_OUT).write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if CI_OUT:
        try:
            main()
        except BaseException as e:   # includes SystemExit from a refused key
            msg = str(e).strip() or type(e).__name__
            if msg.isdigit():
                msg = "The importer couldn't find any session tables (a table with a Week column and weekday columns)."
            ci_write({"ok": False, "error": msg})
            print("  Import failed:", msg)
    else:
        main()
