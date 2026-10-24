"""Shorts Factory uploads.

A clip is saved to media/ the moment it is dropped on /shorts: the video editor works
on media/ files, and the submit then sends only settings. A clip that was dropped and
removed again before the submit is deleted; a file that a brief points at never is.
"""
from __future__ import annotations

import os
import re
import shutil
from uuid import uuid4

from app.services import video_edit

PREFIX = "short_"


def safe_filename(name: str) -> str:
    base = (name or "clip.mp4").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "clip.mp4"
    return base[:80]


def save(fileobj, filename: str) -> str:
    """Write an uploaded clip to media/ → its rel name (short_<id>_<name>)."""
    media = video_edit.MEDIA_DIR
    os.makedirs(media, exist_ok=True)
    rel = f"{PREFIX}{uuid4().hex[:12]}_{safe_filename(filename)}"
    path = os.path.join(media, rel)
    with open(path + ".part", "wb") as fh:
        shutil.copyfileobj(fileobj, fh, 1024 * 1024)
    os.replace(path + ".part", path)
    return rel


def uploaded_rel(rel: str) -> str:
    """The media/ name of a factory upload; EditError for anything else."""
    rel = video_edit.media_rel(video_edit.media_abs(rel))
    if "/" in rel or not rel.startswith(PREFIX):
        raise video_edit.EditError("not a Shorts Factory upload")
    return rel


def remove(rel: str) -> None:
    try:
        os.remove(video_edit.media_abs(rel))
    except (OSError, video_edit.EditError):
        pass


def delete(session, rel: str) -> bool:
    """Delete a dropped clip that never made it into a brief. False when the file is
    not a factory upload or a brief uses it."""
    from sqlmodel import select
    from app.models.models import Brief
    try:
        rel = uploaded_rel(rel)
    except video_edit.EditError:
        return False
    if session.exec(select(Brief.id).where(Brief.media_path == rel)).first() is not None:
        return False
    remove(rel)
    return True
