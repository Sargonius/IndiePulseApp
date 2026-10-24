"""🧩 Modules: turn parts of IndiePulse on and off (app.core.modules)."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core import modules
from app.core.templating import make_templates
from app.services.settings import load_settings, save_settings

router = APIRouter()
templates = make_templates()


def _rows() -> list[dict]:
	disabled = set(load_settings().get(modules.SETTINGS_KEY) or ())
	off = modules.resolve_off(disabled)
	rows = []
	for m in modules.MODULES:
		blocked_by = [modules.BY_ID[r].name for r in m.requires if r in off]
		rows.append({"m": m, "on": m.id not in disabled, "effective": m.id not in off, "blocked_by": blocked_by,
					 "requires": [modules.BY_ID[r].name for r in m.requires]})
	return rows


def _list(request: Request) -> HTMLResponse:
	return templates.TemplateResponse("partials/modules_list.html", {"request": request, "rows": _rows()})


@router.get("/modules", response_class=HTMLResponse)
def modules_page(request: Request):
	return templates.TemplateResponse("modules.html", {"request": request, "rows": _rows()})


@router.post("/api/modules/set", response_class=HTMLResponse)
def modules_set(request: Request, module: str = Form(...), on: bool = Form(False)):
	if module not in modules.BY_ID:
		return HTMLResponse("unknown module", status_code=404)
	disabled = [m for m in (load_settings().get(modules.SETTINGS_KEY) or ()) if m in modules.BY_ID and m != module]
	if not on:
		disabled.append(module)
	save_settings({modules.SETTINGS_KEY: sorted(disabled)})
	return _list(request)


def module_off_response(request: Request, module: modules.Module):
	"""What a page or API of a module that is off answers."""
	if request.url.path.startswith("/api/"):
		return JSONResponse({"detail": "module_off", "module": module.id, "name": module.name}, status_code=404)
	return templates.TemplateResponse("module_off.html", {"request": request, "module": module}, status_code=404)
