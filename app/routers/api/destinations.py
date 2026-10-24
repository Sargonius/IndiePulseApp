"""Settings → Languages & destinations: the languages list and every publishing target with its language."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from app.services import destinations

router = APIRouter()


@router.get("/destinations")
def destinations_list():
	"""The editor's data: languages and, per network, its destinations (secrets masked)."""
	return destinations.editor_context()


@router.post("/destinations/save")
def destinations_save(data: dict = Body(...)):
	"""{"languages": [...], "destinations": [...]} from the editor; nothing is saved when anything is wrong."""
	res = destinations.save(data.get("languages"), data.get("destinations"))
	if not res["ok"]:
		return JSONResponse(res, status_code=400)
	return {**res, "editor": destinations.editor_context()}
