"""MkDocs hook: a link from a page to a file of the repository outside ``docs/`` (the plan, a test,
an App) goes to that file in the repository, as it does when the page is read on GitHub."""

from __future__ import annotations

import posixpath
import re
from typing import Any

LINK = re.compile(r"\]\((\.\./[^)#\s]+)(#[^)\s]*)?\)")


def on_page_markdown(markdown: str, *, page: Any, config: Any, files: Any) -> str:
    repo = str(config.get("repo_url") or "").rstrip("/")
    if not repo:
        return markdown
    folder = posixpath.dirname(page.file.src_uri)

    def outside(match: re.Match[str]) -> str:
        target = posixpath.normpath(posixpath.join("docs", folder, match[1]))
        if target.startswith("docs/") or target.startswith(".."):
            return match[0]
        return f"]({repo}/blob/main/{target}{match[2] or ''})"

    return LINK.sub(outside, markdown)
