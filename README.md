# IndiePulse

Local-first Windows app to write, schedule and cross-post news posts and short videos
(Telegram, Discord, VK, X, YouTube, Facebook, Instagram Reels, Reddit, TikTok, Bluesky).
Free and open source (MIT); donations welcome: [paypal.me/sargoniusgames](https://paypal.me/sargoniusgames).

## Install / update

1. Download `IndiePulse-<version>.zip` from the repository's **Releases** page (under Assets).
2. Unzip it wherever it should live (`D:\Apps`, a USB drive…): you get an `IndiePulse` folder.
3. Double-click `IndiePulse.bat` in it. It needs Python 3.12 from python.org (with the `py` launcher) or
   [uv](https://docs.astral.sh/uv/) on the computer — with uv it fetches Python 3.12 by itself, no admin. The first
   run makes `.venv` and installs the packages (a minute or two), then the tray icon appears and the browser opens
   http://127.0.0.1:8000.

Your settings, posts, media and logs live apart from the code, in `%LOCALAPPDATA%\IndiePulse` — an update or a new
unzip never touches them. To keep everything inside the folder instead (a drive you carry around), put an empty
`portable.txt` next to `IndiePulse.bat` before the first run: the data then goes to `IndiePulse\data`.

**Updates.** IndiePulse looks at the releases 30 seconds after it starts and then every 6 hours. When there is a newer
version, every page shows *IndiePulse vX is available — What's new · Install*, and the tray shows a balloon and an
**Install update vX** item. Install downloads the zip, checks its SHA-256 against the release's `latest.json`, waits
until nothing is publishing, and restarts IndiePulse on the new version (about half a minute). If the new version
doesn't come up within 4 minutes, the previous one is put back by itself and the banner says why (details in
`logs\update.log`). The previous version stays in `updates\previous` of the data folder until the next update.

⚙️ Settings → **Updates**: *Tell me* (the default) / *Install it by itself when nothing is publishing* / *Don't
check*; **Check now**. The tray menu has **Check for updates**. (Running your own build from a private fork? Point
*Repository* at it and add a fine-grained GitHub token with Contents: Read-only.)

An update replaces the code — `app\`, the launchers, `requirements.*`, `README.md` — and nothing else: not `.venv`,
`.env`, `portable.txt` or the data. A **git checkout** is never updated in place and doesn't check by itself (Check now
still says what's released): update it with `git pull`. By hand: download the new zip, Quit IndiePulse from the tray,
unzip over the old folder, start it.

## First run

A fresh install — no settings yet — opens the **setup wizard** (`/setup`) instead of the home page, one step at a
time, each of which can be skipped:

1. **Languages**: the one you write in, the ones you publish in.
2. **Networks & features**: the 🧩 modules you want; the rest is hidden everywhere.
3. **Accounts**: the Settings sections of the networks you picked (keys, connect buttons, their instructions), and a
   channel / webhook / Page / subreddit per language if you have several.
4. **AI & translation**: optional OpenAI / Claude / DeepL keys.
5. **Updates**: how to handle a new version.

The step reached is remembered, so a restart in the middle comes back to it. **Skip setup** leaves it for good;
⚙️ Settings → **Run setup again** goes through it again (without taking over the home page). An install that already
has settings is never sent there: its first start of a version with the wizard marks it set up.

**Where settings live.** Everything you enter in ⚙️ Settings or the wizard is in `.settings.json` in the data folder,
next to the databases: `%LOCALAPPDATA%\IndiePulse`, `data\` of a portable copy, or the IndiePulse folder itself for
a copy that has always kept its data there (`app/core/paths.py`). An update never touches it. Keys and tokens are
stored there in plain text — keep that folder to yourself; the logs mask them — and the app shows them as `•••` +
the last 4 characters. What each setting is (its section, label, type and checks, or that the app writes it itself:
OAuth tokens, expiry times) is declared in `app/core/settings_schema.py`; the Settings page and the wizard are
rendered from it, and a test makes every key of `DEFAULTS` in `app/services/settings.py` belong to exactly one
section.

## Quick start (Windows)

1. Optional: copy `.env.example` to `.env` (host/port, DB path).
2. Double-click `IndiePulse.pyw` (needs Python 3.12 with the `py` launcher) or `IndiePulse.bat` (works on a
   computer without Python too: it takes uv from PATH or from `ClubKit\tools\uv` on the same drive, and uv
   fetches Python 3.12 once into `ClubKit\data\uv` — no install, no admin). The first run on a computer creates
   `.venv`, installs `requirements.lock` and adds **IndiePulse** shortcuts to the Desktop and Start menu.
3. The browser opens http://127.0.0.1:8000: the setup wizard on a fresh install (*First run* above), then the
   ⚙️ Settings page for anything else.

**From the drive on different computers.** A `.venv` made on another computer (its Python in another user's
profile or on the drive under another letter) is recognised by `pyvenv.cfg` and rebuilt (`logs/venv.log`). With
uv it is built on the Python on the drive, packages come from uv's cache there: seconds, and on any computer with
the same drive letter it then just works. With ClubKit next to it, shortcuts and the "Start at Windows sign-in"
item exist only on a trusted computer (`ClubKit\data\trusted-pcs.txt`) — a club computer gets nothing left behind.
Paths saved where the drive had another letter (`E:\Music` in the settings, music of presets and
clips) are found under this one. Scheduled posts go out only while IndiePulse runs: one that is more than 2 hours
late (the drive was unplugged) isn't published on the next start but marked `missed_schedule` — Retry publishes it.
Dependencies: `requirements.txt` says what the app needs, `requirements.lock` the exact tested versions (what gets
installed; regenerate it after editing `requirements.txt`, see its header).

There are no console windows: IndiePulse lives in the tray. The icon runs two processes —
the web server and the Huey worker (the one that actually publishes; scheduled posts don't
go out without it) — and its colour shows their state: green ok, amber starting/restarting,
red broken (hover or open the menu for details). Tray menu: open the UI, restart, logs folder,
check for updates, start with Windows, **Выход** (stops both; the worker finishes the task in progress first).

- Launching again while it runs just opens the browser.
- A crashed process is restarted automatically; a hung server (no `/health` answer for 2 min) too.
- Closing or killing the tray always takes the server and worker down with it (Windows job
  object), so the port is never left busy; leftovers of old `start.bat` windows are killed on start.
- Open pages show a banner while the server is unreachable and reload themselves when it's back.
- `requirements.txt` / `requirements.lock` changes are installed automatically on the next launch (`logs/pip.log`).
- Logs: `logs/launcher.log` (tray), `logs/server.log`, `logs/worker.log` (+ `.prev.log` of the
  previous run), `logs/app.log` (the app itself).
- Dev: `python -m app.serve` / `python -m app.worker` still run standalone in a console.

## Modules

🧩 (next to ⚙️) turns off what you don't use — any of the 10 platforms, the Shorts factory, the TikTok pipeline,
creator outreach, Steam, the guide. A module that is off disappears: its menu items, pages (404), Settings section
and platform checkboxes; the worker neither publishes there nor collects its numbers. Nothing is deleted. The list
lives in `app/core/modules.py` (which platforms, URLs and requirements belong to each); what's off is
`modules_disabled` in the settings.

## Languages & destinations

⚙️ Settings → Languages & destinations. **Languages**: an ordered list of codes (`ru`, `en`, `ja`…); the first is
the one you write in. 📷 Post and 🎬 Video show a text field per language and **Translate missing**, which fills the
empty ones from it (the AI from Settings, else machine translation). **Destinations**: every place posts go, each
with its language — a Telegram channel, Discord webhook, Facebook Page or subreddit per language (as many as you
like), and one for each single-account network (VK, X, Bluesky, YouTube, TikTok, Instagram), where you pick the
language. A post sends each destination the text in its language, else the first language's, else any; a destination
marked "only its language" gets nothing instead (the RU/EN Discord webhooks are, as they always behaved). Every
place that publishes ticks destinations, not platforms:

- 📷 Post and 🎬 Video: one post per ticked destination.
- 🎞️ Shorts factory: a title, description and hashtags per language on every clip (the shared description and
  hashtags fill the first language; **Translate missing** per clip or for the selected ones). After the render each
  ticked destination gets a post with that language's texts (none written: the first language's). One "only its
  language" without a text in it is left out and named on the board.
- 📣 Cross-post a short: a card per destination (Instagram, X, Bluesky, Reddit, Facebook, VK, Telegram, Discord) in its
  language, switchable to any language of Settings; "already live" means on that destination, so a short live on the
  RU channel can still go to the JA one.
- /tiktok: a clip goes to the TikTok destination when that is in the clip's language (otherwise as before).
- Posts → ✎ Edit (and the calendar's Edit) edits the text of every language.
- 🩺 checks every destination's channel, webhook, Page and subreddit (a turned-off one is only noted), and the
  follower counts read each of them: the targets of the old keys keep their metrics and history (`tg_subscribers`,
  `fb_ru_followers`…), any other destination is counted as `<metric>:<destination id>` with a row of its own.

Details:

- Stored in the settings (`languages`, `destinations`); texts in `Brief.texts_json`, with `text_ru` / `text_en` kept
  as mirrors (`app/services/languages.py`, `app/services/destinations.py`). A post knows its destination by
  `Post.destination_id`; a post's own texts (a factory title in another language, a cross-post caption) are in
  `Post.overrides_json`.
- On the first start they are built from the old per-language keys (Telegram chat, Discord RU/EN webhooks, Facebook
  RU/EN pages, subreddits — the RU subreddit comes in off: it was never posted to) and stay **linked** to them:
  editing either changes both, and the old keys are never deleted, so a rollback keeps working. A target set up
  later through its old Settings section (the Telegram chat id, the Discord webhook field, Facebook "Use as RU/EN",
  the subreddit) reaches a destination too: one is added, linked, when none uses that key.
- Posts queued before (and Shorts renders queued before, `/news` tabs left open, scripts that send platforms) have
  no destination and publish exactly as before. A destination removed or turned off while a post waits for it fails
  that post (`destination_off`, Retry after fixing) instead of sending it somewhere else; a module that is off in 🧩
  hides its destinations. The old `/news` page redirects to 📷 Post.

## AI & translation

⚙️ Settings → AI & translation: your own key for OpenAI or Claude (pick one, or "auto"), plus an optional DeepL key.
Every translate button (posts, Steam events, Shorts and reposts, TikTok frame text) and the outreach letters ask the
AI picked there (`app/services/llm.py`; Claude through the official `anthropic` SDK, `anthropic_client.py`); without
a key or when it fails, machine translation (`machine_translate.py`): DeepL with a key, otherwise the free public
translators. 🩺 checks the keys (and the balance).

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
- `.settings.json` (in the data folder) — every setting and credential (local only, git-ignored); what each one is:
  `app/core/settings_schema.py`

## Security notes
The app has no login and must stay bound to 127.0.0.1. It rejects cross-site POSTs
and unknown Host headers so other websites can't drive it from your browser.

## Development
```bash
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe tools\lint.py   # pyflakes, as CI runs it
```
CI (`.github/workflows/ci.yml`) runs both on Windows for every push and pull request to `main`.

## Releasing

The version is `__version__` in `app/__init__.py`, nowhere else (the footer, `/version`, the updater and the release
build read it). `main` carries the next version with `-dev` (`2.0.0-dev`), so a checkout is never taken for a
release; the order is `2.0.0-dev` < `2.0.0-rc.1` < `2.0.0` < `2.0.1-dev`.

To release, from an up-to-date `main` with nothing uncommitted and the `.venv` in place:

```powershell
.\tools\release.ps1                                  # the current version without -dev
.\tools\release.ps1 2.1.0 -Notes "What's new, for the banner and the release page"
.\tools\release.ps1 2.1.0-rc.1                       # a prerelease: installs don't take it
```

It checks the version (newer than every `v*` tag, the tag free on GitHub), runs pyflakes and the tests, shows what it
will do and asks. On `yes` it commits `__version__ = "2.1.0"`, tags that commit `v2.1.0` with the notes as the tag
message (default: the commit subjects since the last tag), commits `2.1.1-dev`, and pushes `main` and the tag
together; if any of that fails, it undoes its commits and the tag.

The tag starts `.github/workflows/release.yml`: it fails unless the tag is `v` + `__version__`, runs pyflakes and the
tests, builds `IndiePulse-2.1.0.zip` and `latest.json` with `tools/build_release.py` (only git-tracked files: `app/`,
the launchers, requirements, README, `.env.example`, a LICENSE if there is one — no tests, tools or `.github`), and
creates the GitHub Release with both attached, using only `GITHUB_TOKEN`. Installed copies see it at their next check
(Install / update above). To look at a build first:
`python tools\build_release.py --tag v<version> --out dist` (the version must match).

A broken release: release a fixed, higher version. Installs that took the broken one went back by themselves only if
it didn't start; the others update to the fix at their next check.

## Support and license
IndiePulse is free (MIT license, see `LICENSE`). If it saves you time, you can support it with a donation:
[paypal.me/sargoniusgames](https://paypal.me/sargoniusgames) — also the ❤ link at the bottom of every page and the
Sponsor button on GitHub.


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