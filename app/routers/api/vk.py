"""VK OAuth, token checks and test post."""

import httpx
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi.responses import HTMLResponse
from urllib.parse import quote_plus
from app.routers.api.common import _e

router = APIRouter()

# VK OAuth: start and callback
@router.get("/accounts/connect/vk")
def vk_connect_start():
    cfg = load_settings()
    client_id = (cfg.get("vk_client_id") or "").strip()
    redirect_uri = (cfg.get("vk_redirect_uri") or "").strip()
    if not client_id or not redirect_uri:
        return HTMLResponse("<span style='color:orange'>Заполните Client ID и Redirect URI, затем сохраните настройки</span>", status_code=400)
    scope = "wall,offline,groups,photos,video"
    auth_url = (
        "https://oauth.vk.com/authorize?"
        f"client_id={quote_plus(client_id)}&display=page&redirect_uri={quote_plus(redirect_uri)}"
        f"&response_type=code&v=5.199&scope={quote_plus(scope)}"
    )
    return HTMLResponse(f"<a href='{_e(auth_url)}' target='_blank'>Открыть VK авторизацию</a>")


@router.get("/accounts/callback/vk")
def vk_connect_callback(code: str):
    cfg = load_settings()
    client_id = cfg.get("vk_client_id")
    client_secret = cfg.get("vk_client_secret")
    redirect_uri = cfg.get("vk_redirect_uri")
    token_url = (
        "https://oauth.vk.com/access_token?"
        f"client_id={client_id}&client_secret={client_secret}&redirect_uri={redirect_uri}&code={code}"
    )
    with httpx.Client(timeout=10) as client:
        r = client.get(token_url)
        r.raise_for_status()
        data = r.json()
    access_token = data.get("access_token", "")
    user_id = str(data.get("user_id", ""))
    if not access_token:
        return HTMLResponse("<span style='color:red'>VK: не удалось получить токен</span>", status_code=400)
    save_settings({"vk_access_token": access_token, "vk_user_id": user_id})
    return HTMLResponse("<span style='color:green'>VK подключён</span>")


def _sanitize_group_id(group_raw: str) -> str:
    s = (group_raw or "").strip()
    s = s.lower().replace("club", "").replace("public", "").replace("event", "")
    if s.startswith("-"):
        s = s[1:]
    # keep only digits
    s = "".join(ch for ch in s if ch.isdigit())
    return s


@router.post("/vk/test_post")
def vk_test_post(text: str = Form("Test from IndiePulse")):
    cfg = load_settings()
    token = (cfg.get("vk_access_token") or "").strip()
    group_raw = (cfg.get("vk_group_id") or "").strip()
    if not token:
        return HTMLResponse("<span style='color:orange'>VK Access Token не задан</span>")
    group_id = _sanitize_group_id(group_raw)
    if not group_id:
        return HTMLResponse("<span style='color:orange'>VK Group ID не задан или некорректен</span>")
    owner_id = f"-{group_id}"  # negative for group wall
    params = {
        "owner_id": owner_id,
        "message": text,
        "from_group": 1,
        "v": "5.199",
        "access_token": token,
    }
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post("https://api.vk.com/method/wall.post", data=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Сетевой сбой: {_e(str(exc))}</span>")
    if isinstance(data, dict) and "error" in data:
        err = data.get("error", {})
        msg = err.get("error_msg") or str(err)
        code = err.get("error_code")
        return HTMLResponse(f"<span style='color:red'>Ошибка VK [{_e(code)}]: {_e(msg)}</span>")
    post_id = (data.get("response") or {}).get("post_id") if isinstance(data, dict) else None
    link = f"https://vk.com/wall{owner_id}_{post_id}" if post_id else ""
    if not post_id:
        return HTMLResponse("<span style='color:orange'>Ответ VK без post_id</span>")
    return HTMLResponse(f"<span style='color:green'>Опубликовано: <a href='{_e(link)}' target='_blank'>ссылка</a></span>")


@router.get("/vk/implicit_url")
def vk_implicit_url():
    cfg = load_settings()
    client_id = (cfg.get("vk_client_id") or "").strip()
    if not client_id:
        return HTMLResponse("<span style='color:orange'>Укажите VK Client ID и сохраните настройки</span>", status_code=400)
    # Use VK's blank.html so the token is visible in the URL fragment for copy-paste
    redirect_uri = "https://oauth.vk.com/blank.html"
    scope = "wall,groups,offline,photos,video"
    url = (
        "https://oauth.vk.com/authorize?"
        f"client_id={quote_plus(client_id)}&display=page&redirect_uri={quote_plus(redirect_uri)}"
        f"&response_type=token&v=5.199&scope={quote_plus(scope)}"
    )
    html = (
        f"<div style='padding:12px; background:#fff3cd; border:1px solid #ffc107; border-radius:4px;'>\n"
        f"<p style='margin:0 0 8px 0; font-weight:600; color:#856404;'>⚠️ ВАЖНО: Токен должен иметь права</p>\n"
        f"<p style='margin:0 0 12px 0; font-family:monospace; background:#fff; padding:6px; border-radius:3px;'>{_e(scope)}</p>\n"
        f"<a class='btn btn-primary' href='{_e(url)}' target='_blank' style='display:inline-block; margin-bottom:8px;'>Открыть ссылку для получения Access Token</a><br/>\n"
        f"<p style='margin:8px 0 0 0; font-size:13px; color:#666;'>После авторизации скопируйте значение <b>access_token</b> из адресной строки (после #access_token=...). Вставьте его в поле <b>Access Token</b> и <b>User Access Token</b> (оба поля!) и сохраните.</p>\n"
        f"</div>"
    )
    return HTMLResponse(html)


@router.post("/vk/check_token")
def vk_check_token(token: str = Form("")):
    cfg = load_settings()
    tok = (token or cfg.get("vk_access_token") or "").strip()
    if not tok:
        return HTMLResponse("<span style='color:orange'>Токен не задан</span>")
    try:
        with httpx.Client(timeout=10) as client:
            # Проверяем валидность токена и получаем информацию о правах
            r = client.get("https://api.vk.com/method/users.get", params={"access_token": tok, "v": "5.199"})
            data = r.json()
            
            if isinstance(data, dict) and "error" in data:
                err = data.get("error", {})
                return HTMLResponse(f"<span style='color:red'>Ошибка VK [{_e(err.get('error_code'))}]: {_e(err.get('error_msg'))}</span>")
            
            # Проверяем права токена через apps.getScopes
            r2 = client.get("https://api.vk.com/method/apps.getScopes", params={
                "access_token": tok,
                "type": "user",
                "v": "5.199"
            })
            scopes_data = r2.json()
            
            scopes_info = ""
            if isinstance(scopes_data, dict) and "response" in scopes_data:
                items = scopes_data.get("response", {}).get("items", [])
                scope_names = [item.get("name") for item in items if item.get("name")]
                
                required = {"photos", "wall", "groups", "video", "offline"}
                has_scopes = set(scope_names)
                missing = required - has_scopes
                
                if missing:
                    scopes_info = f"<br/><span style='color:#ff9800;'>⚠️ Отсутствуют права: {_e(', '.join(missing))}</span>"
                    scopes_info += f"<br/><span style='font-size:11px; color:#666;'>Текущие права: {_e(', '.join(scope_names) if scope_names else 'нет данных')}</span>"
                else:
                    scopes_info = f"<br/><span style='color:#4caf50;'>✓ Все необходимые права присутствуют</span>"
                    scopes_info += f"<br/><span style='font-size:11px; color:#666;'>Права: {_e(', '.join(scope_names))}</span>"
            
            return HTMLResponse(f"<span style='color:green'>Токен валиден</span>{scopes_info}")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Сетевой сбой: {_e(str(exc))}</span>")
