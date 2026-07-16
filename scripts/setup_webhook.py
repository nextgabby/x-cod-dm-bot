#!/usr/bin/env python3
"""
setup_webhook.py — Register an Account Activity API webhook and subscribe
@pixelsattack.  Idempotent: re-running detects and reuses an existing
webhook/subscription.  If an old webhook exists with a different URL (e.g.
a stale ngrok tunnel), it is deleted and replaced.
"""

import base64
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv
from requests import PreparedRequest
from requests_oauthlib import OAuth1

# Try loading .env from the project root — not required; env vars can be set
# directly (e.g. exported in the shell before running this script).
load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

CONSUMER_KEY = os.environ["CONSUMER_KEY"]
CONSUMER_SECRET = os.environ["CONSUMER_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_TOKEN_SECRET = os.environ["ACCESS_TOKEN_SECRET"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]
AAA_ENV = os.environ.get("AAA_ENV", "dev")

BASE = f"https://api.x.com/1.1/account_activity/all/{AAA_ENV}"

client = httpx.Client(timeout=30)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_bearer_token() -> str:
    """Derive an app-only bearer token from the consumer key/secret."""
    creds = base64.b64encode(
        f"{CONSUMER_KEY}:{CONSUMER_SECRET}".encode()
    ).decode()
    resp = client.post(
        "https://api.x.com/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        content="grant_type=client_credentials",
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


_oauth = OAuth1(
    CONSUMER_KEY,
    client_secret=CONSUMER_SECRET,
    resource_owner_key=ACCESS_TOKEN,
    resource_owner_secret=ACCESS_TOKEN_SECRET,
)


def _user_headers(method: str, url: str,
                  body: str | None = None) -> dict[str, str]:
    """Sign a request with OAuth 1.0a user context."""
    req = PreparedRequest()
    req.prepare(method=method, url=url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
                if body else {})
    _oauth(req)
    return dict(req.headers)


# ── AAA operations ────────────────────────────────────────────────────────────

def list_webhooks(bearer: str) -> list[dict]:
    url = f"{BASE}/webhooks.json"
    resp = client.get(url, headers={"Authorization": f"Bearer {bearer}"})
    if resp.status_code == 200:
        data = resp.json()
        # The response is either a list or {"environments": [...]}
        if isinstance(data, list):
            return data
        # Premium AAA wraps in environments
        for env in data.get("environments", []):
            if env.get("environment_name") == AAA_ENV:
                return env.get("webhooks", [])
        return []
    print(f"  List webhooks: {resp.status_code} {resp.text}")
    return []


def register_webhook(bearer: str) -> str | None:
    url = f"{BASE}/webhooks.json"
    resp = client.post(
        url,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        content=f"url={WEBHOOK_URL}",
    )
    if resp.status_code in (200, 201):
        wh = resp.json()
        print(f"  Registered webhook: id={wh['id']}")
        return wh["id"]
    print(f"  Register failed: {resp.status_code} {resp.text}")
    return None


def delete_webhook(bearer: str, webhook_id: str) -> bool:
    url = f"{BASE}/webhooks/{webhook_id}.json"
    resp = client.delete(url, headers={"Authorization": f"Bearer {bearer}"})
    if resp.status_code == 204:
        print(f"  Deleted webhook {webhook_id}")
        return True
    print(f"  Delete failed: {resp.status_code} {resp.text}")
    return False


def subscribe() -> bool:
    url = f"{BASE}/subscriptions.json"
    headers = _user_headers("POST", url)
    resp = client.post(url, headers=headers)
    if resp.status_code in (200, 201, 204):
        print("  Subscription active")
        return True
    if resp.status_code == 409:
        print("  Subscription already exists")
        return True
    print(f"  Subscribe failed: {resp.status_code} {resp.text}")
    return False


def check_subscription() -> bool:
    url = f"{BASE}/subscriptions.json"
    headers = _user_headers("GET", url)
    resp = client.get(url, headers=headers)
    return resp.status_code == 204


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"Account Activity API setup (env: {AAA_ENV})")
    print(f"Webhook URL: {WEBHOOK_URL}\n")

    # 1 — Bearer token (app-only auth for webhook management)
    print("1. Obtaining bearer token ...")
    bearer = get_bearer_token()
    print("   OK\n")

    # 2 — List existing webhooks and decide what to do
    print("2. Checking existing webhooks ...")
    webhooks = list_webhooks(bearer)
    webhook_id = None

    for wh in webhooks:
        if wh.get("url") == WEBHOOK_URL:
            # Exact match — reuse it
            webhook_id = wh["id"]
            valid = wh.get("valid", False)
            print(f"   Found existing webhook {webhook_id} (valid={valid})")
        else:
            # Different URL (old ngrok, previous Render deploy, etc.) — remove
            print(f"   Removing old webhook {wh['id']} ({wh.get('url', '?')})")
            delete_webhook(bearer, wh["id"])

    # 3 — Register if needed
    if webhook_id:
        print("\n3. Webhook already registered, skipping registration")
    else:
        print("\n3. Registering webhook ...")
        webhook_id = register_webhook(bearer)
        if not webhook_id:
            print("\nFATAL: could not register webhook")
            sys.exit(1)

    # 4 — Subscribe @pixelsattack
    print("\n4. Subscribing user ...")
    if not subscribe():
        print("\nFATAL: could not subscribe")
        sys.exit(1)

    # 5 — Validate
    print("\n5. Validating subscription ...")
    if check_subscription():
        print("   Confirmed")
    else:
        print("   WARNING: subscription check returned non-204 (may still work)")

    print(f"\nDone. Webhook {webhook_id} is live for env '{AAA_ENV}'.")


if __name__ == "__main__":
    main()
