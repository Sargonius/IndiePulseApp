from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.routers import pages, api
from app.db.session import create_db_and_tables, migrate_db
from app.core.logging import setup_logging, request_logger
from dotenv import load_dotenv
import os, time

setup_logging()
load_dotenv()
app = FastAPI(title="IndiePulse")
app.middleware("http")(request_logger())

@app.on_event("startup")
def on_startup():
    # Ensure runtime dirs exist
    os.makedirs("media", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    create_db_and_tables()
    migrate_db()
    # simple cache-busting version for static assets
    try:
        app.state.asset_version = str(int(time.time()))
    except Exception:
        app.state.asset_version = "1"

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
    return {"status": "ok"}
