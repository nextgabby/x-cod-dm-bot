# drop_test.py

Webhook-based repost-triggered DM bot for @pixelsattack, using the X Account Activity API. Deployed to Render as a free-tier web service.

When someone reposts the watched post, the server checks they follow @pixelsattack and their account is >= 30 days old, then posts a public @mention and DMs them.

## Prerequisites

- X Developer Portal app with **Account Activity API** access
- OAuth 1.0a keys for @pixelsattack (Read + Write + Direct Messages)
- A GitHub repo (Render deploys from it)

## X Developer Portal setup

1. Go to https://developer.x.com/en/portal/dashboard and open your **Project + App**.
2. Under **Keys and Tokens**, generate:
   - **Consumer Keys** (API Key & Secret) -> `CONSUMER_KEY` / `CONSUMER_SECRET`
   - **Access Token and Secret** with Read+Write+DM permissions -> `ACCESS_TOKEN` / `ACCESS_TOKEN_SECRET`
3. Under **Products > Premium > Dev Environments**, set up an Account Activity API environment (the label goes in `AAA_ENV`, defaults to `dev`).

### Access tiers

| Endpoint | Free | Basic ($100/mo) | Pro |
|---|---|---|---|
| `GET /users/{id}/followers` | yes | yes | yes |
| `POST /tweets` (mention) | yes (limited) | yes | yes |
| `POST /dm_conversations/with/{id}/messages` | **no -- 403** | **yes** | yes |
| Account Activity API (webhooks) | sandbox (limited) | premium | enterprise |

The **DM send endpoint requires Basic or higher**. On Free tier everything works except the DM step, which returns `403`. The script logs this as `UNREACHABLE` rather than crashing.

## Finding your numeric user ID

```bash
curl -s "https://api.x.com/2/users/by/username/pixelsattack" \
  -H "Authorization: Bearer YOUR_BEARER_TOKEN" | python3 -m json.tool
```

The `data.id` field is your numeric ID. Or use https://tweeterid.com.

## Deploy to Render

### 1. Push to GitHub

```bash
git init && git add -A && git commit -m "initial commit"
gh repo create x-cod-dm-bot --private --source . --push
```

### 2. Create a Render web service

1. Go to https://dashboard.render.com/select-repo and connect your GitHub repo.
2. Render will detect `render.yaml`. Select **Web Service**, free tier.
3. Under **Environment**, add these env vars:

| Variable | Value |
|---|---|
| `POST_ID` | The post ID to watch |
| `MY_USER_ID` | `701796333443743744` (or your numeric ID) |
| `MY_USERNAME` | `pixelsattack` |
| `CONSUMER_KEY` | Your API key |
| `CONSUMER_SECRET` | Your API secret |
| `ACCESS_TOKEN` | Your access token |
| `ACCESS_TOKEN_SECRET` | Your access token secret |
| `AAA_ENV` | `dev` (or your AAA environment label) |
| `DRY_RUN` | `false` (set `true` to log without sending) |
| `MIN_ACCOUNT_AGE_DAYS` | `30` |

`PORT` is set automatically by Render -- do not set it manually.

`WEBHOOK_URL` is **not** set on Render; it's only used locally when running `setup_webhook.py`.

4. Click **Deploy**. Wait for the build to finish and the service to show "Live".

Your service URL will be `https://<app-name>.onrender.com`.

### 3. Register the webhook (run once from your laptop)

Set `WEBHOOK_URL` to point at your Render service:

```bash
export WEBHOOK_URL=https://YOUR-APP.onrender.com/webhook
export CONSUMER_KEY=...
export CONSUMER_SECRET=...
export ACCESS_TOKEN=...
export ACCESS_TOKEN_SECRET=...
export AAA_ENV=dev

python3 scripts/setup_webhook.py
```

Or put these in your local `.env` and run:

```bash
python3 scripts/setup_webhook.py
```

This will:
- Delete any old webhook (e.g. a previous ngrok URL) automatically
- Register your Render URL as the new webhook
- Subscribe @pixelsattack to receive events
- Validate the subscription

Re-running is safe -- it detects and reuses the existing registration, or replaces it if the URL changed.

### 4. Test it

From a different X account, repost the watched post. Within seconds the Render logs should show:

```
REPOST detected: @someuser reposted post 2077815588022255963
QUALIFIED @someuser (follows, account 1200d old)
MENTION posted (tweet 123...): @someuser you're in -- check your DMs!
DM sent to @someuser
```

View logs: Render dashboard -> your service -> **Logs** tab.

If that user later replies to the DM, you'll see:

```
DM received -- sender_id=999... text='thanks!'
```

## State file

`processed.json` tracks every reposter by user ID so they're never processed twice.

On Render's free tier the filesystem is **ephemeral** -- state resets on each deploy or restart. The server logs a warning about this on startup. For a test this is fine; if you need persistence, set `REDIS_URL` to a Redis instance (e.g. Render Redis or Upstash) and `pip install redis` (add `redis` to `requirements.txt`).

## OAuth 2.0 login helper

`scripts/oauth2_login.py` performs the OAuth 2.0 Authorization Code + PKCE flow to obtain user-level access and refresh tokens. The bot itself uses OAuth 1.0a, but this helper is useful if you need OAuth 2.0 tokens for other v2 endpoints.

### Setup

1. In the X Developer Portal, under **User authentication settings**, enable **OAuth 2.0**.
2. Set the **Redirect URI** to `http://127.0.0.1:8000/callback`.
3. Copy the **Client ID** into your `.env`:

```
CLIENT_ID=your-oauth2-client-id
```

If your app is a **confidential** client, also set `CLIENT_SECRET`.

### Login

```bash
python3 scripts/oauth2_login.py
```

This will:
1. Generate a PKCE code verifier/challenge
2. Open the X authorization page in your browser
3. Run a local server on `127.0.0.1:8000` to catch the callback
4. Exchange the authorization code for tokens
5. Save `OAUTH2_ACCESS_TOKEN` and `OAUTH2_REFRESH_TOKEN` to `.env`

### Refresh

Access tokens expire after 2 hours. To refresh:

```bash
python3 scripts/oauth2_login.py refresh
```

This reads `OAUTH2_REFRESH_TOKEN` from `.env`, exchanges it for a new access token, and writes both the new access and rotated refresh token back to `.env`.

## Local development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python3 drop_test.py
```

For local testing with ngrok:

```bash
ngrok http 8000
# set WEBHOOK_URL=https://xxxx.ngrok-free.app/webhook in .env
python3 scripts/setup_webhook.py
```

When switching from ngrok back to Render (or vice versa), just re-run `setup_webhook.py` with the new `WEBHOOK_URL`. It will delete the old webhook and register the new one.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `403` on `POST /tweets` | App permissions are Read-only; regenerate tokens with Read+Write+DM. |
| `403` on `POST /dm_conversations/...` | Free-tier account; need Basic ($100/mo) for DM send. |
| `401` on any call | Consumer key/secret or access token/secret are wrong or expired. |
| `429` everywhere | Rate limit hit; the script auto-sleeps until the reset window. |
| Webhook registration fails with `Too many resources` | You already have a webhook registered. Re-run `setup_webhook.py` -- it handles this. |
| CRC challenge fails during registration | Make sure the Render service is deployed and live before running `setup_webhook.py`. |
| No events arriving | Re-run `setup_webhook.py` to verify the subscription. Check Render logs for CRC challenges (X sends them periodically). |
| State lost after redeploy | Expected on Render free tier (ephemeral disk). Set `REDIS_URL` for persistence. |
# x-cod-dm-bot
