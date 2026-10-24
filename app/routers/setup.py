"""The setup wizard (/setup): languages → networks & features → accounts → AI & translation → updates → done.

Every step can be skipped; the step reached is kept in the settings (app/services/setup.py), so a restart in the
middle comes back to it. The account, AI and update steps are the Settings sections themselves (the same
partials, saving in place through the same endpoints); the first two have forms of their own here.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core import modules
from app.core import settings_schema as schema
from app.core.templating import make_templates
from app.routers import settings_page
from app.services import languages, setup
from app.services.settings import load_settings

router = APIRouter()
templates = make_templates()


def _see(path: str = "/setup") -> RedirectResponse:
	return RedirectResponse(path, status_code=303)


def _step_context(step: str, cfg: dict) -> dict:
	if step == "languages":
		langs = setup.suggested_languages(cfg)
		names = dict(languages.NAMES)
		for c in langs:
			names.setdefault(c, languages.name(c))
		return {"langs": langs, "known": sorted(names.items(), key=lambda kv: kv[1])}
	if step == "modules":
		off = set(cfg.get(modules.SETTINGS_KEY) or ())
		return {"module_rows": [{"m": m, "on": m.id not in off,
								 "requires": [modules.BY_ID[r].name for r in m.requires]} for m in modules.MODULES]}
	if step == "accounts":
		return {"sections": settings_page.sections_view(cfg, setup.account_sections(cfg), only_on=True),
				"destinations": settings_page.sections_view(cfg, ["languages"]),
				**settings_page.custom_context(cfg)}
	if step == "ai":
		return {"sections": settings_page.sections_view(cfg, ["openai"])}
	if step == "updates":
		from app.services import updates
		s = schema.BY_ID["updates"]
		form = schema.form_view(s, cfg, keys=schema.group_keys(s, "setup"), group="setup")
		return {"sections": [{"section": s, "form": form, "badge": None}], "wizard_repo": updates.config(cfg)["repo"]}
	if step == "done":
		on = [m for m in modules.MODULES if modules.on(m.id)]
		return {"langs": languages.configured(cfg), "on_modules": on,
				"status_rows": [(s.id, s.title) for s in schema.SECTIONS if s.status and schema.on(s)]}
	return {}


def _page(request: Request, step: str, errors=None, status_code: int = 200):
	cfg = load_settings()
	state = setup.current(cfg)
	ids = setup.STEP_IDS
	i = ids.index(step)
	ctx = {
		"request": request,
		"ui_language": cfg.get("ui_language", "en"),
		"steps": setup.STEPS,
		"step": step,
		"title": dict(setup.STEPS)[step],
		"index": i,
		"reached": ids.index(state["step"]),
		"prev": ids[i - 1] if i > 0 else "",
		"state": state,
		"errors": errors or [],
		"ld_wizard": True,
		**_step_context(step, cfg),
	}
	return templates.TemplateResponse("setup.html", ctx, status_code=status_code)


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, step: str = ""):
	"""The step asked for (the list at the top goes back and forth), else the one reached."""
	if step not in setup.STEP_IDS:
		step = setup.current()["step"]
	return _page(request, step)


@router.post("/setup/languages", response_class=HTMLResponse)
async def setup_languages(request: Request):
	form = await request.form()
	errors = setup.save_languages(form.get("write"), form.getlist("publish"), form.get("other"))
	if errors:
		return _page(request, "languages", errors, status_code=422)
	setup.go_to(setup.after("languages"))
	return _see()


@router.post("/setup/modules", response_class=HTMLResponse)
async def setup_modules(request: Request):
	form = await request.form()
	setup.save_modules(form.getlist("on"))
	setup.go_to(setup.after("modules"))
	return _see()


@router.post("/setup/next")
async def setup_next(request: Request):
	"""Next (the step's sections saved themselves) or "Skip this step": on to the one after it."""
	form = await request.form()
	step = str(form.get("step") or "")
	if step not in setup.STEP_IDS:
		return _see()
	setup.go_to(setup.after(step))
	return _see()


@router.post("/setup/skip")
def setup_skip():
	"""Skip the rest: setup is done (Settings → "Run setup again" brings it back)."""
	setup.finish()
	return _see("/")


@router.post("/setup/restart")
def setup_restart():
	setup.restart()
	return _see()
