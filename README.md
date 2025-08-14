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
