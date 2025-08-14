from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.routers import pages, api
from app.db.session import create_db_and_tables
from app.core.logging import setup_logging, request_logger
from dotenv import load_dotenv

setup_logging()
load_dotenv()
app = FastAPI(title="IndiePulse")
app.middleware("http")(request_logger())

@app.on_event("startup")
def on_startup():
    create_db_and_tables()

app.include_router(pages.router)
app.include_router(api.router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/media", StaticFiles(directory="media"), name="media")
templates = Jinja2Templates(directory="app/templates")

@app.get("/health")
def health():
    return {"status": "ok"}
