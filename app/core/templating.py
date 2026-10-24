"""The one Jinja environment setup for every router: templates from the code folder, plus what base.html needs to
show only the modules that are on."""
from fastapi.templating import Jinja2Templates

from app.core import modules
from app.core.paths import TEMPLATES_DIR


def make_templates() -> Jinja2Templates:
	templates = Jinja2Templates(directory=TEMPLATES_DIR)
	templates.env.globals.update(module_on=modules.on, modules_ui=modules.for_templates)
	return templates
