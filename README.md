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

## Editing the training plan

Click **✎ Edit plan** in the Plan vs actual section. You can change goals, the
checkpoint result, pace ranges, the cadence target, coach notes, every session
(date, type, description, distance, optional, milestone) and the gym days, and
add or delete sessions. Changes show on the page straight away.

**Save to GitHub** writes `plan.json` to this repository, and the live page
rebuilds itself about 2 minutes later. Saving needs a fine-grained GitHub token
that can only edit this repository's contents. It's kept in that browser's
storage and is only ever sent to GitHub. Without the token, anyone can open the
editor but nobody can save. **Download** saves a copy of `plan.json` instead.

### Importing a whole new plan from Markdown

On the laptop, drag the plan's `.md` file onto **`import_plan.bat`**. It reads the
plan twice, independently:

1. **Code** reads every table with a `Week` column and weekday columns (any order or
   wording), works out the dates, estimates each session's distance, and picks up
   rules written as sentences ("Weekly layout: Mon easy 5 km…", "Tue, Wed and Sat are
   optional", "Add 6 × 60 m hill sprints at the end of Tuesday in weeks 1–5").
2. **A local AI model** ([Ollama](https://ollama.com), `qwen3:8b` by default) reads
   each week again on its own, plus the goals and checkpoint result.

Then it checks the result: where the two readers disagree, the plan's own weekly
**Total** column doesn't add up, two sessions land on one day, or a long run jumps
more than 3 km. A preview page lists every change and anything to check, and nothing
is saved until you type **y**. The previous plan is backed up to
`data/plan_backups/`. Saving publishes it like the editor does.

Gym exercise details and the diet section aren't imported; gym days are kept from
the current plan. Change the model with `"import_model"` in `config.json`.

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
| `plan_import.py`, `import_plan.bat` | Imports a Markdown training plan (code reader + local AI reader + checks + preview). |
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
