#!/usr/bin/env python3
"""
drop_test.py — Webhook server for the X Account Activity API.
Listens for repost events on a watched post, verifies follow + account age,
then posts a public @mention and sends a DM from @pixelsattack.
"""

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import JSONResponse
from requests import PreparedRequest
from requests_oauthlib import OAuth1

# .env is convenient locally but not required — on Render, env vars come from
# the dashboard.  load_dotenv() is a no-op when the file is absent.
load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

POST_ID = os.environ["POST_ID"]
MY_USER_ID = os.environ["MY_USER_ID"]
MY_USERNAME = os.environ.get("MY_USERNAME", "pixelsattack")
CONSUMER_KEY = os.environ["CONSUMER_KEY"]
CONSUMER_SECRET = os.environ["CONSUMER_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_TOKEN_SECRET = os.environ["ACCESS_TOKEN_SECRET"]
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
MIN_ACCOUNT_AGE_DAYS = int(os.environ.get("MIN_ACCOUNT_AGE_DAYS", "30"))
PORT = int(os.environ.get("PORT", "8000"))
REDIS_URL = os.environ.get("REDIS_URL", "")

STATE_FILE = Path("processed.json")
BASE = "https://api.x.com/2"

# In-memory dedup for webhook retries (resets on restart — that's fine,
# processed.json / Redis handles durable user-level dedup).
_seen_events: set[str] = set()

# Follower cache — refreshed at most once per 5 minutes.
_follower_ids: set[str] = set()
_follower_ts: float = 0
_FOLLOWER_TTL = 300

# Optional Redis client (created lazily).
_redis = None


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# ── State persistence ────────────────────────────────────────────────────────
# Default: processed.json on disk (ephemeral on Render — fine for testing).
# Optional: set REDIS_URL to persist state across deploys.

_REDIS_KEY = "drop_test:processed"


def _get_redis():
    global _redis
    if _redis is None and REDIS_URL:
        import redis
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


def load_state() -> dict:
    r = _get_redis()
    if r:
        raw = r.get(_REDIS_KEY)
        return json.loads(raw) if raw else {}
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    r = _get_redis()
    if r:
        r.set(_REDIS_KEY, json.dumps(state))
        return
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── OAuth 1.0a signing ───────────────────────────────────────────────────────

_oauth = OAuth1(
    CONSUMER_KEY,
    client_secret=CONSUMER_SECRET,
    resource_owner_key=ACCESS_TOKEN,
    resource_owner_secret=ACCESS_TOKEN_SECRET,
)


def _sign(method: str, url: str, body: str | None = None) -> dict[str, str]:
    """Return headers with a signed OAuth 1.0a Authorization header."""
    req = PreparedRequest()
    req.prepare(method=method, url=url, data=body,
                headers={"Content-Type": "application/json"} if body else {})
    _oauth(req)
    return dict(req.headers)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

_client = httpx.Client(timeout=30)


def _handle_rate_limit(resp: httpx.Response) -> None:
    if resp.status_code != 429:
        return
    reset = resp.headers.get("x-rate-limit-reset")
    if reset:
        wait = max(int(reset) - int(time.time()), 1) + 1
        log(f"RATE-LIMITED — sleeping {wait}s until reset")
        time.sleep(wait)
    else:
        log("RATE-LIMITED — no reset header, sleeping 60s")
        time.sleep(60)


def api_get(path: str, params: dict | None = None) -> httpx.Response:
    url = f"{BASE}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    while True:
        headers = _sign("GET", url)
        resp = _client.get(url, headers=headers)
        if resp.status_code == 429:
            _handle_rate_limit(resp)
            continue
        return resp


def api_post(path: str, json_body: dict) -> httpx.Response:
    url = f"{BASE}{path}"
    body = json.dumps(json_body)
    while True:
        headers = _sign("POST", url, body)
        headers["Content-Type"] = "application/json"
        resp = _client.post(url, headers=headers, content=body)
        if resp.status_code == 429:
            _handle_rate_limit(resp)
            continue
        return resp


# ── Follower cache ────────────────────────────────────────────────────────────

def get_follower_ids() -> set[str]:
    global _follower_ids, _follower_ts
    now = time.time()
    if _follower_ids and (now - _follower_ts) < _FOLLOWER_TTL:
        return _follower_ids
    resp = api_get(f"/users/{MY_USER_ID}/followers", {"max_results": "1000"})
    if resp.status_code != 200:
        log(f"ERROR fetching followers: {resp.status_code} {resp.text}")
        return _follower_ids  # return stale cache rather than empty
    data = resp.json().get("data", [])
    _follower_ids = {u["id"] for u in data}
    _follower_ts = now
    log(f"Refreshed follower cache: {len(_follower_ids)} followers")
    return _follower_ids


# ── Actions ───────────────────────────────────────────────────────────────────

def send_mention(username: str) -> bool:
    text = f"@{username} you're in — check your DMs!"
    if DRY_RUN:
        log(f"DRY-RUN would post mention: {text}")
        return True
    resp = api_post("/tweets", {"text": text})
    if resp.status_code in (200, 201):
        tweet_id = resp.json().get("data", {}).get("id", "?")
        log(f"MENTION posted (tweet {tweet_id}): {text}")
        return True
    log(f"ERROR posting mention for @{username}: {resp.status_code} {resp.text}")
    return False


def send_dm(user_id: str, username: str) -> str:
    if DRY_RUN:
        log(f"DRY-RUN would DM @{username} (id {user_id}): hey")
        return "DM_DRY_RUN"
    resp = api_post(f"/dm_conversations/with/{user_id}/messages", {"text": "hey"})
    if resp.status_code in (200, 201):
        log(f"DM sent to @{username}")
        return "DM_SENT"
    if resp.status_code in (403, 400):
        log(f"UNREACHABLE @{username}: {resp.status_code} {resp.text}")
        return "UNREACHABLE"
    log(f"ERROR sending DM to @{username}: {resp.status_code} {resp.text}")
    return f"DM_ERROR_{resp.status_code}"


# ── Event processing ─────────────────────────────────────────────────────────

def process_repost(tweet: dict) -> None:
    """Handle a single repost event from the webhook payload."""
    event_id = tweet.get("id_str", "")
    if event_id in _seen_events:
        log(f"DUPLICATE event {event_id}, skipping")
        return
    _seen_events.add(event_id)

    user = tweet.get("user", {})
    uid = user.get("id_str", "")
    username = user.get("screen_name", "")

    state = load_state()
    if uid in state:
        log(f"ALREADY PROCESSED @{username} (id {uid})")
        return

    # ── Follow check ──
    follower_ids = get_follower_ids()
    if uid not in follower_ids:
        log(f"SKIP @{username}: doesn't follow @{MY_USERNAME}")
        state[uid] = {"outcome": "NO_FOLLOW", "username": username}
        save_state(state)
        return

    # ── Age check (v1.1 format: "Wed Oct 10 20:19:24 +0000 2012") ──
    created_at_str = user.get("created_at", "")
    if not created_at_str:
        log(f"SKIP @{username}: no created_at in payload")
        state[uid] = {"outcome": "NO_CREATED_AT", "username": username}
        save_state(state)
        return
    created = datetime.strptime(created_at_str, "%a %b %d %H:%M:%S %z %Y")
    now = datetime.now(timezone.utc)
    age_days = (now - created).days
    if age_days < MIN_ACCOUNT_AGE_DAYS:
        log(f"SKIP @{username}: account {age_days} days old "
            f"(need >= {MIN_ACCOUNT_AGE_DAYS})")
        state[uid] = {"outcome": "TOO_NEW", "username": username,
                      "age_days": age_days}
        save_state(state)
        return

    # ── Mention + DM ──
    log(f"QUALIFIED @{username} (follows, account {age_days}d old)")
    mention_ok = send_mention(username)
    if not mention_ok:
        state[uid] = {"outcome": "MENTION_FAILED", "username": username}
        save_state(state)
        return

    dm_outcome = send_dm(uid, username)
    state[uid] = {"outcome": dm_outcome, "username": username}
    save_state(state)


def handle_events(payload: dict) -> None:
    """Background task — process the full webhook payload."""
    # Only act on events for our user.
    for_user = payload.get("for_user_id", "")
    if for_user and for_user != MY_USER_ID:
        return

    # ── Repost events ──
    for tweet in payload.get("tweet_create_events", []):
        rt = tweet.get("retweeted_status")
        if not rt:
            continue
        if rt.get("id_str") != POST_ID:
            continue
        log(f"REPOST detected: @{tweet['user']['screen_name']} "
            f"reposted post {POST_ID}")
        process_repost(tweet)

    # ── DM events (log only) ──
    for event in payload.get("direct_message_events", []):
        event_id = event.get("id", "")
        if event_id in _seen_events:
            continue
        _seen_events.add(event_id)

        msg = event.get("message_create", {})
        sender_id = msg.get("sender_id", "")
        if sender_id == MY_USER_ID:
            continue  # ignore our own outgoing messages
        text = msg.get("message_data", {}).get("text", "")
        log(f"DM received — sender_id={sender_id} text={text!r}")


# ── FastAPI routes ────────────────────────────────────────────────────────────

app = FastAPI()


@app.get("/webhook")
async def crc_challenge(crc_token: str = Query(...)):
    """Answer X's CRC challenge to validate webhook ownership."""
    digest = hmac.new(
        CONSUMER_SECRET.encode(),
        crc_token.encode(),
        hashlib.sha256,
    ).digest()
    token = f"sha256={base64.b64encode(digest).decode()}"
    log("CRC challenge answered")
    return {"response_token": token}


@app.post("/webhook")
async def webhook_event(request: Request, bg: BackgroundTasks):
    """Receive webhook events — return 200 immediately, process in background."""
    payload = await request.json()
    bg.add_task(handle_events, payload)
    return JSONResponse(status_code=200, content={})


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = "DRY-RUN" if DRY_RUN else "LIVE"
    log(f"Starting webhook server [{mode}] on port {PORT}")
    log(f"Watching post {POST_ID} for reposts of @{MY_USERNAME}")
    if not REDIS_URL:
        log("WARNING: No REDIS_URL set — processed.json state is ephemeral "
            "and will be lost on restart/redeploy. Set REDIS_URL for "
            "persistent state.")
    else:
        log("Using Redis for persistent state")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
