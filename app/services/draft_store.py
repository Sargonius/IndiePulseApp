from __future__ import annotations

import json
import os
from typing import Any, Dict


DRAFT_PATH = ".draft_news.json"


def load_draft() -> Dict[str, Any]:
	if not os.path.exists(DRAFT_PATH):
		return {"text_ru": "", "text_en": "", "tags": "", "platforms": [], "scheduled_at": "", "media_path": ""}
	try:
		with open(DRAFT_PATH, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		return {"text_ru": "", "text_en": "", "tags": "", "platforms": [], "scheduled_at": "", "media_path": ""}


def save_draft(data: Dict[str, Any]) -> None:
	state = load_draft()
	state.update({k: v for k, v in data.items() if v is not None})
	with open(DRAFT_PATH, "w", encoding="utf-8") as f:
		json.dump(state, f, ensure_ascii=False, indent=2)


def clear_draft() -> None:
	if os.path.exists(DRAFT_PATH):
		os.remove(DRAFT_PATH)


