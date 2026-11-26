# Training dashboard

A personal running dashboard built from my Garmin watch, with a half marathon
(3 Jan 2027) and an 800m race (March 2027) on the calendar.

**Live page:** https://assembly00111.github.io/Training/

It updates itself every 3 hours. The footer says when the data was last updated. To
update right away (owner only), click **↻ Refresh now** there, or open **Actions →
Update dashboard → Run workflow**, then reload the page after about 3 minutes.

## What's on it

- **This week:** running distance against the 8-week average, runs, time and pace
- **Half marathon:** countdown, longest recent run against the long-run target, predicted finish
- **800m speed:** fastest 1 km, a rough 800m estimate, and peak speed against the 2:15 goal
- **Growth:** distance, time, number of runs and typical run pace for the last 90 days against the 90
  before. The pace is a median that leaves out runs far slower than usual (hikes, walks,
  a collapsed race), so one odd run can't skew it.
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

## Importing a whole new plan from Markdown

Instead of editing session by session, you can import the plan's `.md` file. It's
read twice, independently, and the two readings are compared:

1. **Code** reads every table with a `Week` column and weekday columns (any order or
   wording), works out the dates, estimates each session's distance, and picks up
   rules written as sentences ("Weekly layout: Mon easy 5 km…", "Tue, Wed and Sat are
   optional", "Add 6 × 60 m hill sprints at the end of Tuesday in weeks 1–5"). The
   code's reading is the one that gets used.
2. **An AI** reads each week again on its own, plus the goals, checkpoint result and
   cadence target. It's there to catch anything the code misread.

Then come the checks: where the two readers disagree, the plan's own weekly **Total**
column doesn't add up, two sessions land on one day, a session falls outside its
week, or a long run jumps more than 3 km. You see every change and anything to
check before anything is saved.

Gym exercise details and the diet section aren't imported; gym days are kept from
the current plan.

### Which AI

| | NVIDIA (cloud) | My laptop (local) |
|---|---|---|
| Model | `nvidia/nemotron-3-ultra-550b-a55b` via build.nvidia.com (free) | `qwen3:8b` via [Ollama](https://ollama.com) |
| Time for a full plan | about 10 minutes | about an hour, using the laptop's processor |
| Privacy | The text it needs is sent to NVIDIA (see Privacy) | Nothing leaves the laptop |
| Needs | `NVIDIA_API_KEY` | Ollama with `qwen3:8b`, Node.js |

Both found the same thing on the current plan, so pick by time and privacy. Use
NVIDIA when the laptop is busy (simulations, model training).

### On the site (any device)

1. In the editor, click **⇪ Import .md** and pick the file. Paste the GitHub token first
   if that device doesn't remember it.
2. Choose **NVIDIA (cloud)** or **My laptop (local qwen3)**. The site remembers your
   choice for next time.
3. Wait. You can close the page; the result waits in the editor until you open it again.
   - **NVIDIA:** a GitHub job reads it.
   - **My laptop:** on the laptop, the browser asks to open the **Plan worker** (tick
     "always allow" to stop it asking). A window opens, reads that one plan and closes
     itself. Nothing runs in the background. If Ollama isn't running, the worker starts
     it for the job and stops it afterwards. From another device, the laptop must be on
     and you run `start_worker.bat` there. If the laptop hasn't picked the plan up after 2
     minutes, the editor says so and offers **Use NVIDIA instead**.
4. Read what changed and anything to check, click **Load into editor**, look it over,
   then **Save to GitHub**. Nothing changes until you save.

### On the laptop

Drag the `.md` file onto **`import_plan.bat`**. It asks which AI to use (Enter means the
default), opens a preview page in the browser, and saves only if you type **y**. The
previous plan is backed up to `data/plan_backups/`, and saving publishes it like the
editor does.

From a terminal: `python plan_import.py <plan.md>` with `--local` or `--nvidia` to skip
the question, `--no-ai` for code only, and `--dry-run` to save nothing.

### How site imports work

GitHub is the job queue and storage; there are two workers.

```
browser ── encrypts .md ──► imports/request.json ──┬─ engine "nvidia" ─► Import plan workflow (GitHub)
           (public key for                         │
            the chosen worker)                     └─ engine "local" ──► plan_worker.py (laptop)
                                                                              │
editor ◄── polls ── imports/result.json ◄── {status: "running"}, then the result ┘
```

- The file is encrypted in the browser (AES-256-GCM, with that key wrapped by RSA),
  so this public repo only ever holds ciphertext. Each worker has its own key pair.
  The public keys are in `config.json`. The GitHub job's private key is the
  `PLAN_IMPORT_PRIVATE_KEY` secret, and the laptop's is `PLAN_IMPORT_LOCAL_PRIVATE_KEY`
  in `.env.local`.
- Only the result (the new plan, the changes and anything to check) is committed, as
  `imports/result.json`. The decrypted file is deleted as soon as it's read.
- The site opens the laptop worker with a `training-worker://` link. Run
  `python setup_worker_link.py` once to register it for your Windows user (no admin
  needed), and `--remove` to undo it. Run it again if this folder moves.
- The worker checks GitHub every 30 seconds, works in its own copy of the repo
  (`data/worker-repo`, so it never touches this folder), posts a "running" status when
  it starts, and exits after one plan, or after 10 minutes if none arrives. If a newer
  upload replaced its plan meanwhile, it doesn't send its result, so a late laptop
  result can't overwrite NVIDIA's.

## How it works

```
Garmin Personal API ──► pull.py ──► data/*.json ──► build.py ──► one HTML page
   (read-only key)                 (never committed)            (charts in plain SVG)
```

| File | What it does |
|---|---|
| `garmin.py` | Tiny client for Garmin's Personal API. Swaps the key for a 24-hour token. Python standard library only. |
| `test_connection.py` | Checks the Garmin key works by listing the 5 latest activities. |
| `pull.py` | Downloads activities, daily summaries, sleep, HRV and weight. Re-runs only fetch what's new; `--full` fetches all history. |
| `build.py` | Turns the data into a single self-contained `dashboard.html`. `--public` builds the copy for this site, with place names removed. |
| `template.html` | The dashboard's layout, styles and charts, the plan editor and the import screens. |
| `refresh.bat` | Fetches plan changes from GitHub, pulls new Garmin data and rebuilds the local dashboard. |
| `config.json` | Title, race, units, default look, the repository, and the two public keys for imports. |
| `plan.json` | The training plan, session by session, used by the plan vs actual panel. |
| `plan_import.py`, `import_plan.bat` | Reads a Markdown plan (code reader + AI reader + checks + preview). |
| `plan_worker.py`, `start_worker.bat` | The laptop worker for imports sent to "My laptop". |
| `setup_worker_link.py` | Lets the site open the laptop worker (`training-worker://`). |
| `imports/` | `request.json` (the latest encrypted upload) and `result.json` (its result). |
| `.github/workflows/update.yml` | The scheduled job that pulls, builds and publishes. |
| `.github/workflows/import-plan.yml`, `.github/scripts/decrypt_plan.mjs` | Imports a plan uploaded from the site for NVIDIA. |

### Keys and secrets

| Name | Where | What for |
|---|---|---|
| `GARMIN_PERSONAL_API_KEY` | `.env.local` and a repository secret | Reading Garmin data |
| `NVIDIA_API_KEY` | `.env.local` and a repository secret | The NVIDIA reader (optional) |
| `PLAN_IMPORT_PRIVATE_KEY` | Repository secret only | Decrypting uploads sent to NVIDIA |
| `PLAN_IMPORT_LOCAL_PRIVATE_KEY` | `.env.local` only | Decrypting uploads sent to the laptop |
| GitHub token (fine-grained, this repo's contents) | The browser's storage | Saving the plan and uploading imports from the site |

`.env.local` and `data/` are git-ignored, so none of these can be committed by accident.

## Privacy

- The Garmin key is **read-only**: it can't change anything in the Garmin account.
- On GitHub, keys live only in encrypted repository secrets. They're never printed or committed.
- Raw Garmin data is never committed. It exists only on the GitHub runner while the
  job runs, and only the finished page is published.
- The public page has place names removed from activity names, and it asks search
  engines not to list it.
- Plan files uploaded from the site are encrypted before they leave the browser; the
  repo never holds the readable file (it has notes on weight, injuries and diet).
- Only the NVIDIA reader sends anything to an outside AI. It sends the week tables
  and the goal, checkpoint and cadence lines, with the runner's name replaced. The
  diet section and Garmin data are never sent. NVIDIA's free trial terms allow it to
  use what's sent to improve its products. For a plan that should stay completely
  private, choose **My laptop**.

## Running it locally

Needs Python 3.10 or newer. Plan imports with the local AI also need
[Ollama](https://ollama.com) with `ollama pull qwen3:8b`, and Node.js.

1. Put the key in a file called `.env.local`: `GARMIN_PERSONAL_API_KEY=<key>` (it's git-ignored).
2. Run `refresh.bat` on Windows. It fetches plan changes, pulls data, builds and opens `dashboard.html`.
3. For site imports with the local AI, run `python setup_worker_link.py` once.

Optional settings in `config.json`: `"import_model"` (local model, default `qwen3:8b`),
`"import_think"` (let qwen think first: slower but more reliable, default on),
`"import_nvidia_model"`, and `"import_ai"` (`"local"` or `"nvidia"`, the laptop
importer's default).

## Renewing things

- **Garmin key:** expires after a year. Make a new one at
  https://connect.garmin.com/app/settings/personalApiToken and update `.env.local`
  and the repository secret.
- **GitHub token:** fine-grained tokens expire. Make a new one (this repository only,
  Contents read and write) and paste it into the editor.
- **NVIDIA model:** NVIDIA retires models. If imports fail with "doesn't offer the
  model", pick a current one from build.nvidia.com and set `"import_nvidia_model"`.
- **Import keys:** if a private key is lost, make a new key pair and replace both halves:
  the public key in `config.json`, and the private key in the secret or `.env.local`.

## Notes

- GitHub pauses scheduled workflows after 60 days without commits to the repo. If
  that happens, it sends an email; re-enable the workflow under Actions.
- The 800m estimate comes from the fastest 1 km split and is only a guide.
