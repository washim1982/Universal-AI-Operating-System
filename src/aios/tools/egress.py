"""Where a tool call may send data (FR-SEC-10).

An operator can set a ceiling for the installation (``AIOS_HTTP_ALLOWED_DOMAINS``), an App can
declare a list (``spec.egress.allow``) and name a list for one tool (``spec.egress.tools``). A host
has to be on every list that exists, and with no list at all nothing is reachable.

Once an App declares a list, every URL anywhere in a tool's arguments is checked, not just the
ones the tool will fetch: that is how a fooled agent leaks data, in a link or a Markdown image it
writes into an email. A tool that takes a bare host name isn't covered, and has to check it itself.

The parsing is deliberately strict. A URL is refused if two parsers could disagree about its host,
which is the classic way past an allowlist, and an address written as a number is refused however
it is spelled.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

from aios.core.errors import ToolDeniedError, ToolInputError

_URL = re.compile(r"https?://[^\s\"'<>)\]}]+", re.IGNORECASE)
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*$")


def host_of(url: str) -> str:
    """The host ``url`` connects to, or a refusal if that can't be said for certain."""
    if "\\" in url or any(ord(char) < 0x21 for char in url):
        raise ToolDeniedError(f"this URL can be read more than one way: {url!r}")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ToolDeniedError(f"not a usable URL: {url!r}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise ToolInputError(f"only http(s) URLs can be fetched: {url!r}")
    if parts.username is not None or parts.password is not None:
        raise ToolDeniedError(f"URLs can't carry a user name: {url!r}")
    if _NUMERIC_HOST.match(host):
        raise ToolDeniedError(f"IP addresses can't be fetched: {host}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ToolDeniedError(f"IP addresses can't be fetched: {host}")


def urls_in(value: Any) -> Iterator[str]:
    """Every URL in a tool's arguments, at any depth."""
    if isinstance(value, str):
        yield from _URL.findall(value)
    elif isinstance(value, Mapping):
        for item in cast(Mapping[str, Any], value).values():
            yield from urls_in(item)
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for item in cast(Sequence[Any], value):
            yield from urls_in(item)


def listed(host: str, domains: Sequence[str]) -> bool:
    """``host`` is one of ``domains`` or below one of them."""
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


@dataclass(frozen=True, slots=True)
class Egress:
    """The lists that apply to one tool of one App. ``None`` means there isn't one."""

    app: tuple[str, ...] | None = None
    tool: tuple[str, ...] | None = None

    @property
    def declared(self) -> bool:
        return self.app is not None or self.tool is not None

    def permits(self, host: str, ceiling: Sequence[str] = ()) -> bool:
        lists = [found for found in (self.app, self.tool) if found is not None]
        if ceiling:
            lists.append(tuple(ceiling))
        return bool(lists) and all(listed(host, domains) for domains in lists)

    def check(self, url: str, ceiling: Sequence[str] = ()) -> None:
        host = host_of(url)
        if not self.permits(host, ceiling):
            raise ToolDeniedError(f"{host} is not on the allowlist for this tool")
