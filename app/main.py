import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()  # before app.core.config reads the environment

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.core import config
from app.core.logging import setup_logging, request_logger
from app.db.session import create_db_and_tables, migrate_db
from app.routers import pages, api

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure runtime dirs exist
    os.makedirs("media", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    create_db_and_tables()
    migrate_db()
    # simple cache-busting version for static assets
    app.state.asset_version = str(int(time.time()))
    # App versioning for footer
    app.state.app_version = "1.2.0"
    app.state.app_last_change = "Version 1.2.0 - API status page, publish reliability & security fixes"
    yield


app = FastAPI(title="IndiePulse", lifespan=lifespan)


# ── Local-app hardening ──────────────────────────────────────────────────────
# The app has no login: anything that can reach it can post as you and read your
# tokens. Two cheap guards keep other websites out:
#  * Host allow-list — blocks DNS-rebinding (evil.com resolving to 127.0.0.1);
#  * cross-site write block — a page you visit can't submit forms/fetches to
#    /api/* (e.g. swap your Discord webhook). Browsers always send Sec-Fetch-Site
#    and/or Origin on such requests; curl/scripts send neither and are allowed.
def _allowed_hosts() -> list[str]:
    extra = [h.strip() for h in (os.getenv("ALLOWED_HOSTS") or "").split(",") if h.strip()]
    if config.APP_HOST in ("0.0.0.0", "::") and not extra:
        return ["*"]  # explicitly exposed on the LAN — can't know the hostname
    return ["127.0.0.1", "localhost", "::1", config.APP_HOST, *extra]


_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def block_cross_site_writes(request: Request, call_next):
    if request.method in _UNSAFE:
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        cross = site == "cross-site"
        if origin is not None and not cross:
            cross = origin == "null" or urlsplit(origin).netloc != request.headers.get("host", "")
        if cross:
            return PlainTextResponse("cross-site request blocked", status_code=403)
    return await call_next(request)


app.middleware("http")(request_logger())
# added last = runs first
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts())

app.include_router(pages.router)
app.include_router(api.router)

# Ensure directories exist before mounting
os.makedirs("app/static", exist_ok=True)
os.makedirs("media", exist_ok=True)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/media", StaticFiles(directory="media"), name="media")
templates = Jinja2Templates(directory="app/templates")


@app.get("/health")
def health():
    # "boot" changes on every server start: open pages use it to notice a restart
    return {"status": "ok", "boot": getattr(app.state, "asset_version", "")}


@app.get("/version")
def version():
    return {
        "app_version": getattr(app.state, "app_version", "1.0.0"),
        "last_change": getattr(app.state, "app_last_change", ""),
        "asset_version": getattr(app.state, "asset_version", "1"),
    }
