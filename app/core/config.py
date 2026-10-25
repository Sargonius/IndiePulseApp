import os
from typing import Optional

def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, default)
    return value

APP_NAME = get_env("APP_NAME", "IndiePulse")
APP_HOST = get_env("APP_HOST", "127.0.0.1")
# 8870, not 8000: plenty of dev servers and tools take 8000 (2026-10-25). A copy whose OAuth apps are
# registered for 127.0.0.1:8000 keeps APP_PORT=8000 in its .env.
APP_PORT = int(get_env("APP_PORT", "8870"))
# Public base of this app — OAuth redirect URIs are built from it. Must match what is
# registered in each provider's console.
APP_BASE_URL = (get_env("APP_BASE_URL") or f"http://127.0.0.1:{APP_PORT}").rstrip("/")
DATABASE_URL = get_env("DATABASE_URL", "sqlite:///./indiepulse.db")
LOG_LEVEL = get_env("LOG_LEVEL", "INFO")
# Only a fallback for Settings → Steam → "Steam App ID for the store news export" (app.services.steam_links.app_id).
# Its own name: plain STEAM_APP_ID is what ClubKit puts in the Windows user environment for another game's Steam
# builds, and IndiePulse read it — the export opened that game's event editor on those computers (found 2026-10-24).
STEAM_APP_ID = get_env("INDIEPULSE_STEAM_APP_ID", "")
