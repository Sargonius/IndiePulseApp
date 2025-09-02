# IndiePulse (MVP)

Local-first Windows app to generate, schedule, and cross-post news and short videos with AI assistance.

## Quick start (Windows)

1. Configure environment:
   - Copy `.env.example` to `.env` and fill in keys as needed
2. Start the web app:
   - Double-click `start.bat` (or run in terminal)
3. Start the job worker (new terminal):
   - Double-click `run_worker.bat`

App runs at http://127.0.0.1:8000

## Tech (MVP)
- FastAPI + Jinja2 + htmx
- SQLite (SQLModel)
- Huey (SQLite-backed queue)
- FFmpeg (external binary)

## Layout
- app/  application code
- media/  user-uploaded files
- render/  rendered videos
- logs/  logs per job
- vendor/ffmpeg/  optional local ffmpeg binaries

## Packaging
- Run via `start.bat` for development
- PyInstaller packaging will be added later

## License
Proprietary  all rights reserved.
# IndiePulseNew

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