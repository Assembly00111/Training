"""Check the Garmin connection by listing your 5 latest activities."""
import sys
from garmin import get, GarminError

try:
    acts = get("/activitylist-service/activities/search/activities?start=0&limit=5") or []
except GarminError as e:
    print("FAILED:", e)
    sys.exit(1)

print("Connected! Your 5 latest activities:\n")
for a in acts:
    date = (a.get("startTimeLocal") or "")[:16]
    kind = (a.get("activityType") or {}).get("typeKey", "?").replace("_", " ")
    km = (a.get("distance") or 0) / 1000
    print(f"  {date}  {kind:<22} {km:6.2f} km   {a.get('activityName') or ''}")
if not acts:
    print("  (no activities found)")
