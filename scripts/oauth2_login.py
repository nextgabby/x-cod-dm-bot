#!/usr/bin/env python3
"""
oauth2_login.py — One-shot OAuth 2.0 Authorization Code + PKCE flow for X.

Usage:
    python3 scripts/oauth2_login.py          # login (opens browser)
    python3 scripts/oauth2_login.py refresh   # refresh an expired access token

Reads CLIENT_ID (and optionally CLIENT_SECRET) from .env / environment.
On success, writes OAUTH2_ACCESS_TOKEN and OAUTH2_REFRESH_TOKEN back to .env.
"""

import base64
import hashlib
import http.server
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=ENV_FILE)

CLIENT_ID = os.environ.get("CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")  # confidential clients only
REDIRECT_URI = "http://127.0.0.1:8000/callback"
SCOPES = "tweet.read tweet.write users.read dm.read dm.write offline.access"
TOKEN_URL = "https://api.x.com/2/oauth2/token"

if not CLIENT_ID:
    print("ERROR: CLIENT_ID not set. Add it to .env or export it.")
    print("       (Found in your X app under 'OAuth 2.0 Client ID')")
    sys.exit(1)


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def make_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── .env writer ───────────────────────────────────────────────────────────────

def update_env_file(key: str, value: str) -> None:
    """Set or replace a key in .env.  Appends if the key doesn't exist."""
    if not ENV_FILE.exists():
        ENV_FILE.write_text(f"{key}={value}\n")
        return
    lines = ENV_FILE.read_text().splitlines(keepends=True)
    found = False
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}={value}\n")
    ENV_FILE.write_text("".join(lines))


# ── Tiny callback server ─────────────────────────────────────────────────────

_auth_code: str | None = None
_server_error: str | None = None


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        global _auth_code, _server_error
        qs = parse_qs(urlparse(self.path).query)

        if "error" in qs:
            _server_error = qs["error"][0]
            desc = qs.get("error_description", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                f"<h2>Authorization failed</h2><p>{_server_error}: {desc}</p>"
                .encode()
            )
            return

        code = qs.get("code", [None])[0]
        if code:
            _auth_code = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Authorization successful</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h2>No code received</h2>")

    def log_message(self, format, *args):
        pass  # silence request logs


def wait_for_callback() -> str:
    """Start a server on 127.0.0.1:8000, wait for the callback, return the code."""
    server = http.server.HTTPServer(("127.0.0.1", 8000), CallbackHandler)
    server.timeout = 120
    server.handle_request()  # blocks until one request arrives
    server.server_close()
    if _server_error:
        print(f"\nAuthorization error: {_server_error}")
        sys.exit(1)
    if not _auth_code:
        print("\nNo authorization code received (timed out or bad request)")
        sys.exit(1)
    return _auth_code


# ── Token exchange ────────────────────────────────────────────────────────────

def _token_request(data: dict) -> dict:
    """POST to the token endpoint.  Handles both public and confidential clients."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    auth = None
    if CLIENT_SECRET:
        # Confidential client — use Basic auth
        creds = base64.b64encode(
            f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
        ).decode()
        headers["Authorization"] = f"Basic {creds}"
    else:
        # Public client — send client_id in body
        data["client_id"] = CLIENT_ID

    resp = httpx.post(TOKEN_URL, data=data, headers=headers, timeout=30)
    if resp.status_code != 200:
        print(f"\nToken request failed: {resp.status_code}")
        print(resp.text)
        sys.exit(1)
    return resp.json()


def exchange_code(code: str, verifier: str) -> dict:
    return _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })


def refresh_token(token: str) -> dict:
    return _token_request({
        "grant_type": "refresh_token",
        "refresh_token": token,
    })


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_login() -> None:
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorize_url = f"https://x.com/i/oauth2/authorize?{urlencode(params)}"

    print("Opening browser for authorization ...\n")
    print(f"If it doesn't open, visit:\n{authorize_url}\n")
    print("Waiting for callback on http://127.0.0.1:8000/callback ...")
    webbrowser.open(authorize_url)

    code = wait_for_callback()
    print("\nExchanging code for tokens ...")
    tokens = exchange_code(code, verifier)

    access = tokens["access_token"]
    refresh = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", "?")

    print(f"\nAccess token (expires in {expires_in}s):")
    print(f"  {access[:20]}...{access[-10:]}")
    if refresh:
        print(f"Refresh token:")
        print(f"  {refresh[:20]}...{refresh[-10:]}")

    update_env_file("OAUTH2_ACCESS_TOKEN", access)
    if refresh:
        update_env_file("OAUTH2_REFRESH_TOKEN", refresh)
    print(f"\nSaved to {ENV_FILE}")


def cmd_refresh() -> None:
    token = os.environ.get("OAUTH2_REFRESH_TOKEN", "")
    if not token:
        print("ERROR: OAUTH2_REFRESH_TOKEN not set. Run login first:")
        print("  python3 scripts/oauth2_login.py")
        sys.exit(1)

    print("Refreshing access token ...")
    tokens = refresh_token(token)

    access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", "?")

    print(f"\nNew access token (expires in {expires_in}s):")
    print(f"  {access[:20]}...{access[-10:]}")

    update_env_file("OAUTH2_ACCESS_TOKEN", access)
    # X rotates the refresh token on each use
    if new_refresh:
        update_env_file("OAUTH2_REFRESH_TOKEN", new_refresh)
        print("Refresh token rotated and saved")
    print(f"\nSaved to {ENV_FILE}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        cmd_refresh()
    else:
        cmd_login()
