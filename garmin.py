"""Tiny read-only client for Garmin's Personal API. Standard library only.

The API key is read from .env.local and is never printed or logged.
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ENV_FILE = ROOT / ".env.local"
TOKEN_FILE = DATA / "token.json"
BASE = "https://personal.connectapi.garmin.com"
TOKEN_URL = BASE + "/personalapitoken-service/personal-api/personal-api-key/token"
TOKEN_MAX_AGE = 20 * 3600  # tokens last 24h; renew after 20h


class GarminError(Exception):
    pass


class TooBig(GarminError):
    """HTTP 500 with empty body: response exceeded ~256 KB."""


def _read_key():
    # On GitHub Actions the key comes from an encrypted repository secret.
    env_key = os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()
    if env_key:
        return env_key
    if not ENV_FILE.exists():
        raise GarminError(f"{ENV_FILE} not found.")
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        if line.strip().startswith("GARMIN_PERSONAL_API_KEY") and "=" in line:
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            if key:
                return key
    raise GarminError("GARMIN_PERSONAL_API_KEY is empty in .env.local.")


def _new_token():
    key = _read_key()
    req = urllib.request.Request(
        TOKEN_URL, data=b"", method="POST",
        headers={"Authorization": f"ApiKey {key}", "Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            token = r.read().decode("utf-8").strip().strip('"')
    except urllib.error.HTTPError as e:
        raise GarminError(
            f"Token request failed (HTTP {e.code}). The key in .env.local is "
            "probably missing, mistyped or expired."
        ) from None
    except urllib.error.URLError as e:
        raise GarminError(f"Could not reach Garmin: {e.reason}") from None
    if not token:
        raise GarminError("Garmin returned an empty token.")
    DATA.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"token": token, "created": time.time()}))
    return token


def get_token(force=False):
    if not force and TOKEN_FILE.exists():
        try:
            saved = json.loads(TOKEN_FILE.read_text())
            if time.time() - saved["created"] < TOKEN_MAX_AGE and saved["token"]:
                return saved["token"]
        except (ValueError, KeyError):
            pass
    return _new_token()


def get(path, retries=4):
    """GET a path and return parsed JSON (or None for empty body)."""
    token = get_token()
    refreshed = False
    attempt = 0
    while True:
        req = urllib.request.Request(
            BASE + path,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            time.sleep(0.3)  # be polite
            return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code == 401 and not refreshed:
                token = get_token(force=True)
                refreshed = True
                continue
            if e.code == 500 and not body.strip():
                raise TooBig(path) from None
            if e.code in (429, 502, 503) and attempt < retries:
                attempt += 1
                time.sleep(3 * attempt)
                continue
            if e.code in (204, 404):
                return None
            raise GarminError(f"HTTP {e.code} for {path}") from None
        except urllib.error.URLError as e:
            if attempt < retries:
                attempt += 1
                time.sleep(3 * attempt)
                continue
            raise GarminError(f"Network error: {e.reason}") from None
