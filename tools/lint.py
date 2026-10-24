"""pyflakes over app/ and tests/ as CI runs it: any warning fails the build (`python tools/lint.py`).

One exception: a line marked `# noqa` (the convention the code already uses for imports kept on purpose: a re-export,
a module imported for its side effects, a pytest fixture). pyflakes itself ignores noqa; "redefinition of unused X"
counts as the import's own warning, so the noqa on the import line covers it too (a fixture imported, then taken as an
argument).
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

from pyflakes import api, messages
from pyflakes.reporter import Reporter

ROOT = Path(__file__).resolve().parents[1]
PATHS = ("app", "tests")
_NOQA = re.compile(r"#\s*noqa\b", re.I)


class _Collector(Reporter):
	def __init__(self) -> None:
		super().__init__(io.StringIO(), io.StringIO())
		self.found: list[messages.Message] = []
		self.broken: list[str] = []

	def flake(self, message: messages.Message) -> None:
		self.found.append(message)

	def syntaxError(self, filename, msg, lineno, offset, text) -> None:
		self.broken.append(f"{filename}:{lineno}: {msg}")

	def unexpectedError(self, filename, msg) -> None:
		self.broken.append(f"{filename}: {msg}")


def _noqa(message: messages.Message, lines: dict[str, list[str]]) -> bool:
	src = lines.setdefault(message.filename, Path(message.filename).read_text(encoding="utf-8-sig").splitlines())
	marked = [message.lineno]
	if isinstance(message, messages.RedefinedWhileUnused):
		marked.append(message.message_args[1])  # the line of the import being redefined
	return any(0 < n <= len(src) and _NOQA.search(src[n - 1]) for n in marked)


def problems(paths=PATHS) -> list[str]:
	"""Every warning that should fail the check, as 'path:line: message'."""
	reporter = _Collector()
	api.checkRecursive([str(ROOT / p) for p in paths], reporter)
	lines: dict[str, list[str]] = {}
	out = list(reporter.broken)
	for m in reporter.found:
		rel = Path(m.filename).resolve().relative_to(ROOT).as_posix()
		text = m.message % m.message_args
		if not _noqa(m, lines):
			out.append(f"{rel}:{m.lineno}: {text}")
	return out


def main() -> int:
	found = problems()
	for line in found:
		print(line)
	print(f"pyflakes: {len(found)} problem(s)" if found else "pyflakes: clean")
	return 1 if found else 0


if __name__ == "__main__":
	sys.exit(main())
