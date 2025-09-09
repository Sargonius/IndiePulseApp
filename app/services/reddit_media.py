from __future__ import annotations

import os
import subprocess
import time
from typing import Tuple, Optional

from app.services.video_editor import _ffmpeg_path


def prepare_video_for_reddit(src_path: str) -> Tuple[str, Optional[str]]:
	"""Transcode an input video into a Reddit-friendly MP4 and generate a thumbnail.
	Returns (video_path, thumbnail_path). If transcoding fails, returns the original path and None.
	"""
	try:
		ffmpeg = _ffmpeg_path()
		base = str(int(time.time()))
		os.makedirs('render', exist_ok=True)
		# Output mp4 under render
		out_path = os.path.abspath(os.path.join('render', f"reddit_{base}.mp4"))
		# Limit width to 1280, keep aspect; 30 fps; H.264 yuv420p; AAC audio; faststart
		vf = "scale='min(1280,iw)':-2:flags=lanczos,fps=30"
		cmd = [
			ffmpeg, '-y', '-i', src_path,
			'-vf', vf,
			'-c:v', 'libx264', '-profile:v', 'high', '-level', '4.1', '-pix_fmt', 'yuv420p',
			'-preset', 'veryfast', '-crf', '22',
			'-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
			'-movflags', '+faststart',
			out_path,
		]
		proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
		if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
			return src_path, None
		# Generate a small thumbnail at 640px width
		thumb_path = os.path.abspath(os.path.join('render', f"reddit_{base}_thumb.jpg"))
		cmd2 = [ffmpeg, '-y', '-ss', '00:00:01', '-i', out_path, '-frames:v', '1', '-vf', 'scale=640:-2', thumb_path]
		proc2 = subprocess.run(cmd2, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
		if proc2.returncode != 0 or not os.path.exists(thumb_path) or os.path.getsize(thumb_path) == 0:
			thumb_path = None
		return out_path, thumb_path
	except Exception:
		return src_path, None


