"""Laptop worker for plan imports.

When you import a plan on the dashboard and choose "My laptop", the page uploads it
encrypted with this laptop's public key (config.json "import_local_public_key"). This
worker checks GitHub every 30 seconds, decrypts the plan with the private key in
.env.local, reads it with the local AI (Ollama), and pushes the result back as
imports/result.json for the dashboard to show. The decrypted file is deleted afterwards.

It works in its own copy of the repo (data/worker-repo), so it never touches this folder.

Normally the dashboard opens it for you (the training-worker:// link, set up once with
setup_worker_link.py): it reads that one plan and closes, so nothing runs in the
background. You can also run start_worker.bat by hand.
  --launched  opened from the dashboard: read one plan, then exit (or exit after
              10 minutes if no plan for the laptop shows up)
  --once      stop after one import
  --test      skip the AI (code reader only), for a quick test of the plumbing
If Ollama isn't running, the worker starts it for the job and stops it afterwards.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLONE = ROOT / "data" / "worker-repo"
POLL_SECONDS = 30
MAX_AGE_HOURS = 24
OLLAMA = "http://127.0.0.1:11434"


def git(*args, cwd=CLONE, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:200]}")
    return r


def env_value(name):
    v = os.environ.get(name, "").strip()
    if v:
        return v
    env = ROOT / ".env.local"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith(name + "="):
                return ln.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def say(msg):
    print(f"[{datetime.now():%H:%M}] {msg}", flush=True)


def ollama_up():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=5) as r:
            return bool(r.read())
    except Exception:
        return False


def start_ollama():
    """Start Ollama if it isn't running. Returns the process if this worker started it (to stop it later), else None."""
    if ollama_up():
        return None
    exe = shutil.which("ollama") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe")
    if not Path(exe).exists() and not shutil.which("ollama"):
        return None
    say("Starting Ollama for this job (it's stopped again afterwards)...")
    proc = subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(60):
        if ollama_up():
            return proc
        time.sleep(1)
    return proc


def stop_ollama(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        say("Stopped Ollama again.")


def setup():
    if not (CLONE / ".git").exists():
        url = git("remote", "get-url", "origin", cwd=ROOT).stdout.strip()
        CLONE.parent.mkdir(exist_ok=True)
        say("Making the worker's own copy of the repo (first run only)...")
        subprocess.run(["git", "clone", "-q", url, str(CLONE)], check=True)
    for k in ("user.name", "user.email"):          # same identity as this repo (keeps your email private)
        v = git("config", k, cwd=ROOT, check=False).stdout.strip()
        if v:
            git("config", k, v)


def remote_json(path):
    r = git("show", f"origin/main:{path}", check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def sync():
    git("fetch", "-q", "origin", "main")
    git("reset", "-q", "--hard", "origin/main")    # the worker's own copy only


def push_result(obj, message):
    for _ in range(4):
        sync()
        p = CLONE / "imports" / "result.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        git("add", "imports/result.json")
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return
        git("commit", "-q", "-m", message)
        if git("push", "-q", "origin", "HEAD:main", check=False).returncode == 0:
            return
        time.sleep(5)
    raise RuntimeError("couldn't push the result to GitHub")


def fresh(req):
    try:
        at = datetime.fromisoformat(str(req.get("at", "")).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - at).total_seconds() < MAX_AGE_HOURS * 3600
    except ValueError:
        return False


def handle(req, test=False):
    rid = req.get("id")
    started = now()
    push_result({"id": rid, "at": started, "status": "running", "engine": "local"}, "Plan import: laptop started reading")
    tmp = Path(tempfile.mkdtemp(prefix="plan-import-"))
    md, idf, out = tmp / "plan.md", tmp / "id.txt", tmp / "result.json"
    try:
        key = env_value("PLAN_IMPORT_LOCAL_PRIVATE_KEY")
        dec = subprocess.run(["node", str(CLONE / ".github" / "scripts" / "decrypt_plan.mjs"), str(CLONE / "imports" / "request.json"), str(md), str(idf)],
                             env={**os.environ, "PLAN_IMPORT_PRIVATE_KEY": key}, capture_output=True, text=True)
        if not key or dec.returncode != 0:
            result = {"ok": False, "error": "The laptop couldn't decrypt the upload. Check PLAN_IMPORT_LOCAL_PRIVATE_KEY in .env.local "
                                            "matches import_local_public_key in config.json."}
        elif not test and not ollama_up():
            result = {"ok": False, "error": "Ollama isn't running on the laptop. Start Ollama and import again, or choose NVIDIA."}
        else:
            args = [sys.executable, str(CLONE / "plan_import.py"), str(md), "--local", "--no-open", f"--ci={out}", f"--id={rid}"]
            if test:
                args.append("--no-ai")
            subprocess.run(args, cwd=CLONE, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            result = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"ok": False, "error": "The importer stopped without a result."}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)       # the decrypted plan never stays on disk
    result.update({"id": rid, "engine": "local", "started": started})
    result.setdefault("at", now())
    git("fetch", "-q", "origin", "main")
    if (remote_json("imports/request.json") or {}).get("id") != rid:
        say("A newer plan was uploaded meanwhile (maybe sent to NVIDIA instead), so this result isn't sent.")
        return result
    push_result(result, "Plan import result")
    return result


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    launched, test = "--launched" in sys.argv, "--test" in sys.argv
    once = launched or "--once" in sys.argv
    idle_limit = 10 * 60 if launched else None
    if not env_value("PLAN_IMPORT_LOCAL_PRIVATE_KEY"):
        print("PLAN_IMPORT_LOCAL_PRIVATE_KEY isn't in .env.local, so this laptop can't read uploads.")
        sys.exit(1)
    setup()
    if launched:
        say("Opened from the dashboard. Waiting for your plan to arrive (usually under a minute)...")
    else:
        say("Plan worker is running. Leave this window open; plans you send to 'My laptop' from the dashboard are read here.")
    t_start = time.time()
    done = set()
    while True:
        if idle_limit and time.time() - t_start > idle_limit:
            say("No plan for the laptop arrived in 10 minutes, so the worker is closing.")
            return
        try:
            git("fetch", "-q", "origin", "main")
            req = remote_json("imports/request.json") or {}
            res = remote_json("imports/result.json") or {}
            rid = req.get("id")
            # New laptop request, not answered yet (a "running" result left by a worker that stopped counts as unanswered).
            if rid and req.get("engine") == "local" and rid not in done and fresh(req) and (res.get("id") != rid or res.get("status") == "running"):
                say("New plan to read. With the local AI this takes about an hour...")
                t0 = time.time()
                sync()
                ollama_proc = None if test else start_ollama()
                try:
                    r = handle(req, test)
                finally:
                    stop_ollama(ollama_proc)
                done.add(rid)
                say(f"Done in {(time.time() - t0) / 60:.0f} min: " + ("result sent to the dashboard." if r.get("ok") else "failed: " + r.get("error", "")))
                if once:
                    return
        except Exception as e:
            say(f"(problem: {e}; trying again shortly)")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
