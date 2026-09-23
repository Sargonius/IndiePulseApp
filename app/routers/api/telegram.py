"""Telegram settings tests."""

from app.services.settings import load_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi.responses import HTMLResponse
from app.routers.api.common import _e

router = APIRouter()

@router.post("/telegram/test")
def telegram_test(message: str = Form("Test from IndiePulse")):
    try:
        cfg = load_settings()
        token = (cfg.get("tg_bot_token") or "").strip()
        chat_id = (cfg.get("tg_chat_id_ru") or "").strip()
        if not token or not chat_id:
            return HTMLResponse("<span style='color:orange'>Telegram не настроен</span>")
        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        with httpx.Client(timeout=15) as client:
            r = client.post(url, data=data)
            jd = r.json()
        if r.status_code >= 400 or not jd.get("ok"):
            return HTMLResponse(f"<span style='color:red'>Ошибка Telegram: {_e(jd)}</span>")
        mid = ((jd.get("result") or {}).get("message_id"))
        return HTMLResponse(f"<span style='color:green'>Отправлено (message_id={_e(mid)})</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Ошибка: {_e(str(exc))}</span>", status_code=500)


@router.post("/telegram/check")
def telegram_check():
    try:
        cfg = load_settings()
        token = (cfg.get("tg_bot_token") or "").strip()
        chat_id = (cfg.get("tg_chat_id_ru") or "").strip()
        if not token:
            return HTMLResponse("<span style='color:orange'>Нет Bot Token</span>")
        import httpx
        with httpx.Client(timeout=10) as client:
            me = client.get(f"https://api.telegram.org/bot{token}/getMe")
            mjs = me.json()
            if not mjs.get("ok"):
                return HTMLResponse(f"<span style='color:red'>getMe: {_e(mjs)}</span>")
            chat_status = ""
            if chat_id:
                ch = client.get(f"https://api.telegram.org/bot{token}/getChat", params={"chat_id": chat_id})
                cjs = ch.json()
                chat_status = " ok" if cjs.get("ok") else f" error: {cjs}"
        return HTMLResponse(f"<span style='color:green'>Bot OK.{_e(chat_status)}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Ошибка: {_e(str(exc))}</span>")
