"""The app icon (same pulse mark as the web favicon), with a status dot for the tray."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BLUE = (37, 99, 235, 255)
STATUS_COLORS = {
	"ok": (34, 197, 94, 255),
	"busy": (245, 158, 11, 255),
	"error": (239, 68, 68, 255),
	"off": (148, 163, 184, 255),
}

_S = 256  # draw big, downscale for smooth edges


def _base() -> Image.Image:
	img = Image.new("RGBA", (_S, _S), (0, 0, 0, 0))
	d = ImageDraw.Draw(img)
	d.rounded_rectangle((0, 0, _S - 1, _S - 1), radius=56, fill=BLUE)
	# favicon path "M4 18h6l3-8 5 13 4-9h6" on a 32px grid
	pts = [(4, 18), (10, 18), (13, 10), (18, 23), (22, 14), (28, 14)]
	k = _S / 32
	d.line([(x * k, y * k) for x, y in pts], fill="white", width=int(2.6 * k), joint="curve")
	for x, y in (pts[0], pts[-1]):
		r = 1.3 * k
		d.ellipse((x * k - r, y * k - r, x * k + r, y * k + r), fill="white")
	return img


def tray_image(status: str, size: int = 64) -> Image.Image:
	img = _base()
	d = ImageDraw.Draw(img)
	r, cx, cy = 58, _S - 62, _S - 62
	d.ellipse((cx - r - 14, cy - r - 14, cx + r + 14, cy + r + 14), fill=(255, 255, 255, 255))
	d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=STATUS_COLORS.get(status, STATUS_COLORS["off"]))
	return img.resize((size, size), Image.LANCZOS)


def ensure_ico(path: Path) -> Path:
	if not path.exists():
		path.parent.mkdir(parents=True, exist_ok=True)
		_base().save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
	return path
