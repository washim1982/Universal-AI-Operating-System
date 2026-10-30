"""Built-in tools (WP 1.5): ``search.web.query``, ``http.page.fetch``, ``filesystem.file.*``.

* **search** calls a web search API (Brave Search's by default; ``AIOS_SEARCH_API_KEY``).
* **http** fetches pages only from allowlisted domains (``AIOS_HTTP_ALLOWED_DOMAINS``), refuses
  IP literals, re-checks every redirect and returns text, truncated.
* **filesystem** reads and writes inside one sandbox directory; paths can't escape it.
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

from aios.core.errors import ToolDeniedError, ToolError, ToolInputError
from aios.core.policy import SideEffect
from aios.tools.providers import NativeProvider, NativeTool, tool

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
MAX_REDIRECTS = 3
_TAGS = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.DOTALL | re.IGNORECASE)
_SPACE = re.compile(r"\s+")


def search_provider(
    *,
    api_key: str | None = None,
    endpoint: str = BRAVE_SEARCH_URL,
    http_client: httpx2.AsyncClient | None = None,
) -> NativeProvider:
    """``search.web.query`` over a web search API."""
    key = api_key or os.environ.get("AIOS_SEARCH_API_KEY")

    @tool("search.web.query", side_effect=SideEffect.READ, timeout_s=20)
    async def query(q: str, limit: int = 5) -> list[dict[str, str]]:
        """Search the web. Returns results with a title, URL and snippet."""
        if not key:
            raise ToolError("search.web.query needs AIOS_SEARCH_API_KEY")
        client = http_client or httpx2.AsyncClient(timeout=15)
        try:
            response = await client.get(
                endpoint,
                params={"q": q, "count": max(1, min(limit, 20))},
                headers={"Accept": "application/json", "X-Subscription-Token": key},
            )
        except httpx2.HTTPError as exc:
            raise ToolError(f"search failed: {exc}", retryable=True) from exc
        finally:
            if http_client is None:
                await client.aclose()
        if response.status_code >= 400:
            raise ToolError(
                f"search API {response.status_code}", retryable=response.status_code >= 500
            )
        results: list[dict[str, Any]] = response.json().get("web", {}).get("results", [])
        return [
            {
                "title": str(r.get("title", "")),
                "url": str(r.get("url", "")),
                "snippet": _text(str(r.get("description", ""))),
            }
            for r in results[:limit]
        ]

    return NativeProvider("search", [query], replayable=True)


def http_provider(
    allowed_domains: Sequence[str] | None = None,
    *,
    http_client: httpx2.AsyncClient | None = None,
) -> NativeProvider:
    """``http.page.fetch`` with a domain allowlist."""
    configured = os.environ.get("AIOS_HTTP_ALLOWED_DOMAINS", "")
    domains = [d.strip().lower() for d in (allowed_domains or configured.split(",")) if d.strip()]

    @tool("http.page.fetch", side_effect=SideEffect.READ, timeout_s=30)
    async def fetch(url: str, max_chars: int = 20_000) -> dict[str, Any]:
        """Fetch a web page from an allowed domain and return its text."""
        client = http_client or httpx2.AsyncClient(timeout=20, follow_redirects=False)
        try:
            current = url
            for _ in range(MAX_REDIRECTS + 1):
                check_url(current, domains)
                response = await client.get(current)
                if response.is_redirect and "location" in response.headers:
                    current = urljoin(current, response.headers["location"])
                    continue
                break
            else:
                raise ToolError(f"too many redirects from {url}")
        except httpx2.HTTPError as exc:
            raise ToolError(f"fetch failed: {exc}", retryable=True) from exc
        finally:
            if http_client is None:
                await client.aclose()
        content_type = response.headers.get("content-type", "")
        body = response.text
        text = _text(body) if "html" in content_type else body
        return {
            "url": current,
            "status": response.status_code,
            "content_type": content_type,
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    return NativeProvider("http", [fetch], replayable=True)


def check_url(url: str, allowed_domains: Sequence[str]) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise ToolInputError(f"only http(s) URLs can be fetched: {url!r}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ToolDeniedError(f"IP addresses can't be fetched: {host}")
    if not any(host == d or host.endswith(f".{d}") for d in allowed_domains):
        raise ToolDeniedError(f"{host} is not in the http.page.fetch allowlist")


def _text(markup: str) -> str:
    return _SPACE.sub(" ", html.unescape(_TAGS.sub(" ", markup))).strip()


def filesystem_provider(root: str | Path) -> NativeProvider:
    """``filesystem.file.read`` / ``list`` / ``write`` confined to ``root``."""
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True)

    def resolve(path: str) -> Path:
        target = (base / path).resolve()
        if target != base and base not in target.parents:
            raise ToolDeniedError(f"{path!r} is outside the sandbox")
        return target

    @tool("filesystem.file.read", side_effect=SideEffect.READ)
    async def read(path: str, max_chars: int = 100_000) -> str:
        """Read a text file from the sandbox."""
        target = resolve(path)
        if not target.is_file():
            raise ToolInputError(f"no such file: {path}")
        return target.read_text(encoding="utf-8", errors="replace")[:max_chars]

    @tool("filesystem.file.list", side_effect=SideEffect.READ)
    async def list_files(path: str = ".") -> list[str]:
        """List files under a sandbox directory."""
        target = resolve(path)
        if not target.is_dir():
            raise ToolInputError(f"no such directory: {path}")
        return sorted(p.relative_to(base).as_posix() for p in target.rglob("*") if p.is_file())

    @tool("filesystem.file.write", side_effect=SideEffect.WRITE)
    async def write(path: str, content: str) -> dict[str, Any]:
        """Write a text file in the sandbox."""
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": target.relative_to(base).as_posix(), "bytes": len(content.encode())}

    tools: list[NativeTool] = [read, list_files, write]
    return NativeProvider("filesystem", tools)
