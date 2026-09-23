"""Discord webhook settings and test."""

from app.services.discord_client import DiscordClient
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from app.routers.api.common import _e

router = APIRouter()

@router.post("/discord/webhook")
def save_discord_webhook(url: str = Form(...)):
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Empty webhook URL")
    save_settings({"discord_webhook_url": url})
    # Return small HTML snippet for htmx
    return HTMLResponse("<span style='color:green'>Discord webhook сохранён</span>")


@router.post("/discord/webhooks/save")
def save_discord_webhooks(url_ru: str = Form(""), url_en: str = Form("")):
    updates = {}
    if (url_ru or "").strip():
        updates["discord_webhook_ru"] = url_ru.strip()
    if (url_en or "").strip():
        updates["discord_webhook_en"] = url_en.strip()
    if updates:
        save_settings(updates)
    return HTMLResponse("<span style='color:green'>Webhooks updated</span>")


@router.post("/discord/test")
def discord_send_test():
    url = load_settings().get("discord_webhook_url")
    if not url:
        return HTMLResponse("<span style='color:orange'>Discord webhook не настроен</span>", status_code=400)
    client = DiscordClient(url)
    try:
        mid, murl = client.send_message("Test from IndiePulse")
        link = f"<a href='{_e(murl)}' target='_blank'>сообщение</a>" if murl else ""
        return HTMLResponse(f"<span style='color:green'>Отправлено {link}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Ошибка: {_e(str(exc))}</span>", status_code=500)
