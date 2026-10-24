"""Shared helpers for the /api routers: HTML escaping, OAuth state, templates."""

from app.services.settings import load_settings
from app.services.settings import save_settings
from app.core.templating import make_templates
from fastapi import Request

def _oauth_new_state(provider: str) -> str:
    """Random one-time `state` for an OAuth connect flow (stops forged callbacks
    from swapping in someone else's account)."""
    import secrets as _secrets
    state = _secrets.token_urlsafe(24)
    save_settings({f"oauth_state_{provider}": state})
    return state


def _oauth_check_state(provider: str, state: str) -> bool:
    import hmac as _hmac
    expected = (load_settings().get(f"oauth_state_{provider}") or "").strip()
    ok = bool(expected) and _hmac.compare_digest(expected, (state or "").strip())
    if ok:
        save_settings({f"oauth_state_{provider}": ""})  # single use
    return ok


def _e(value) -> str:
    """Escape anything interpolated into an HTML snippet (API error bodies, query
    params, exception texts) and strip credentials from it."""
    import html as _h
    from app.core.redact import redact
    return _h.escape(redact(str(value)), quote=True)


templates_api = make_templates()


async def form_texts(request: Request) -> dict:
    """The text_<lang> fields of a post form → {lang: text}. The forms have one per language of Settings →
    Languages, so the names aren't known in advance (a FastAPI dependency: the form is parsed once and cached)."""
    from app.services.languages import normalize_code
    form = await request.form()
    out = {}
    for key, value in form.multi_items():
        if key.startswith("text_") and isinstance(value, str):
            lang = normalize_code(key[len("text_"):])
            if lang:
                out[lang] = value
    return out
