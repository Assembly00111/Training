# Training dashboard

A personal running dashboard built from my Garmin watch, with a half marathon
(3 Jan 2027) and an 800m race (March 2027) on the calendar.

**Live page:** https://assembly00111.github.io/Training/

It updates itself every 3 hours. To update right away (owner only), open
**Actions → Update dashboard → Run workflow**, then reload the page after
about 3 minutes.

## What's on it

- **This week:** running distance against the 8-week average, runs, time and pace
- **Half marathon:** countdown, longest recent run against the long-run target, predicted finish
- **800m speed:** fastest 1 km, a rough 800m estimate, and peak speed against the 2:15 goal
- **Plan vs actual:** every session of the training plan beside what was actually run, with
  easy-pace and cadence targets, key sessions and weekly planned-vs-actual distance
- **Trends:** weekly distance, pace, fastest 1 km, cadence, stride and year-on-year distance
- **Records:** best efforts, biggest weeks and months, longest runs, recent activities

Four looks (Watch, Log, Race day, Trail), hover or tap for exact numbers, and it
works on a phone.

## How it works

```
Garmin Personal API ──► pull.py ──► data/*.json ──► build.py ──► one HTML page
   (read-only key)                 (never committed)            (charts in plain SVG)
```

| File | What it does |
|---|---|
| `garmin.py` | Tiny client for Garmin's Personal API. Swaps the key for a 24-hour token. Python standard library only. |
| `pull.py` | Downloads activities, daily summaries, sleep, HRV and weight. Re-runs only fetch what's new. |
| `build.py` | Turns the data into a single self-contained `dashboard.html`. `--public` builds the copy for this site, with place names removed. |
| `template.html` | The dashboard's layout, styles and charts. |
| `config.json` | Title, race, units and default look. |
| `plan.json` | The training plan, session by session, used by the plan vs actual panel. |
| `.github/workflows/update.yml` | The scheduled job that pulls, builds and publishes. |

## Privacy

- The Garmin key is **read-only**: it can't change anything in the Garmin account.
- On GitHub, the key lives only in an encrypted repository secret
  (`GARMIN_PERSONAL_API_KEY`). It's never printed or committed.
- Raw Garmin data is never committed. It exists only on the GitHub runner while the
  job runs, and only the finished page is published.
- The public page has place names removed from activity names, and it asks search
  engines not to list it.

## Running it locally

Needs Python 3.10 or newer and nothing else.

1. Put the key in a file called `.env.local`: `GARMIN_PERSONAL_API_KEY=<key>` (it's git-ignored).
2. Run `refresh.bat` on Windows. It pulls, builds and opens `dashboard.html`.

The key expires after a year. Make a new one at
https://connect.garmin.com/app/settings/personalApiToken and update `.env.local`
and the repository secret.

## Notes

- GitHub pauses scheduled workflows after 60 days without commits to the repo. If
  that happens, it sends an email; re-enable the workflow under Actions.
- The 800m estimate comes from the fastest 1 km split and is only a guide.
