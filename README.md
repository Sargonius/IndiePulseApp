# IndiePulse

Local-first Windows app to write, schedule and cross-post news posts and short videos
(Telegram, Discord, VK, X, YouTube, Facebook, Instagram Reels, Reddit, TikTok, Bluesky).

## Quick start (Windows)

1. Optional: copy `.env.example` to `.env` (host/port, DB path).
2. Double-click `start.bat`. It creates `.venv`, installs `requirements.txt`, and opens
   two windows: **IndiePulse Server** and **IndiePulse Worker** (the Huey worker that
   actually publishes; scheduled posts don't go out while it's closed).
3. Open http://127.0.0.1:8000, fill in platform credentials on the ⚙️ Settings page.

`restart.bat` (or the ↻ button in the header) restarts both processes.

## Checking the integrations

The 🩺 page (`/status`) checks every configured platform in one click without posting
anything: token validity, permissions, expiry dates, plus each platform's last
successful publish and errors since. Run it after the app has been idle for a while.

## Tech
- FastAPI + Jinja2 + htmx, SQLite (SQLModel, WAL mode), Huey (SQLite-backed queue)
- FFmpeg (auto-detected; `vendor/ffmpeg/ffmpeg.exe` preferred)

## Layout
- `app/routers/` — pages (`pages.py`) and the JSON/htmx API (`api/`, one module per area)
- `app/platforms/` — per-platform `publish()` / `fetch_metrics()` (one module per network)
- `app/jobs/tasks.py` — Huey tasks: publish dispatcher, scheduling watchdog, metrics refresh
- `app/services/` — low-level API clients, settings, tokens, health check
- `media/` uploads, `render/` rendered videos, `logs/` logs
- `.settings.json` — all credentials (local only, git-ignored)

## Security notes
The app has no login and must stay bound to 127.0.0.1. It rejects cross-site POSTs
and unknown Host headers so other websites can't drive it from your browser.

## Development
```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
```

## License
Proprietary — all rights reserved.


## Facebook API setup (Page posting)

IndiePulse posts to Facebook Pages using a Page Access Token. An App Token or User Token alone will not work for publishing. Use the steps below to obtain a Page Access Token and the Page ID, then add them to `.settings.json`.

### Prerequisites
- A Meta app on `https://developers.facebook.com` with an App ID and App Secret
- You are an admin/editor of the target Facebook Page
- App permissions (granted to your user token):
  - `pages_manage_posts` (required to publish)
  - `pages_read_engagement` (required to read `permalink_url`)
  - `pages_show_list` (required to list pages and get page tokens)
  - Optional: `pages_manage_metadata`

Note: In Development mode, only users with a role on the app can generate tokens that work. For posting to non-admin users or public usage, the app must be set to Live and the above permissions approved via App Review.

### Step 1 — Get a short-lived User Access Token
You can use Graph API Explorer:
1. Open `https://developers.facebook.com/tools/explorer/`
2. In the app drop-down, select your app
3. Click “Get Token” → “Get User Access Token”
4. Check permissions: `pages_manage_posts`, `pages_read_engagement`, `pages_show_list` (and agree to prompts)
5. Copy the resulting user access token

### Step 2 — Exchange for a long-lived User Access Token
Use your App ID and App Secret to exchange the short-lived token:

```bash
curl "https://graph.facebook.com/v19.0/oauth/access_token\
?grant_type=fb_exchange_token\
&client_id=APP_ID\
&client_secret=APP_SECRET\
&fb_exchange_token=SHORT_LIVED_USER_TOKEN"
```

Save the `access_token` from the response (this is the long-lived user token).

### Step 3 — Get your Page ID and Page Access Token
Call `/me/accounts` with the long-lived user token to list pages you manage and retrieve their page tokens:

```bash
curl -H "Authorization: Bearer LONG_LIVED_USER_TOKEN" \
  "https://graph.facebook.com/v19.0/me/accounts?fields=id,name,access_token"
```

- Locate the target Page in the response
- Copy its `id` (Page ID) and `access_token` (Page Access Token)

You can verify the token by publishing a test post:

```bash
curl -X POST \
  -F "message=Hello from IndiePulse" \
  -F "access_token=PAGE_ACCESS_TOKEN" \
  "https://graph.facebook.com/v19.0/PAGE_ID/feed"
```

To retrieve a permalink for a post (what IndiePulse does), your token needs `pages_read_engagement`:

```bash
curl -H "Authorization: Bearer PAGE_ACCESS_TOKEN" \
  "https://graph.facebook.com/v19.0/POST_ID?fields=permalink_url"
```

### Step 4 — Add to IndiePulse settings
Edit (or create) `.settings.json` in the project root and fill in the fields used by the app:

```json
{
  "fb_ru_page_access_token": "PAGE_ACCESS_TOKEN_FOR_RU",
  "fb_en_page_access_token": "PAGE_ACCESS_TOKEN_FOR_EN",
  "fb_ru_page_id": "RU_PAGE_ID",
  "fb_en_page_id": "EN_PAGE_ID"
}
```

You can maintain one or both language groups depending on your setup. IndiePulse reads these keys from `.settings.json` at runtime.

### Common pitfalls
- Do not use an App Token (`APP_ID|APP_SECRET`) for posting; Facebook requires a Page Access Token
- Tokens generated in Development mode only work for app-role users
- Long-lived user tokens typically last ~60 days; page tokens are tied to the user token validity. Repeat the exchange when needed
- Ensure the required permissions were selected when generating the user token