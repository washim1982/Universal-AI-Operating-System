"""Record/replay cassettes for model and tool calls (implementation plan §8; WP 1.4, 1.5).

A cassette maps a **call site** (``CallSite.key``: step, fan-out item, agent, loop, attempt,
call index) to the response recorded there. Keying by site rather than by request content
lets a recorded run replay after routing changes (another provider answers the same call) or
when a tool returns slightly different data; each interaction also stores a hash of the
request, and a mismatch is logged as drift.

Modes: ``replay`` serves only recorded responses (tests and CI), ``record`` calls the real
model or tool and saves the result, ``live`` does not use a cassette.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from aios.core.errors import AIOSError

logger = logging.getLogger("aios.cassettes")

CassetteMode = Literal["replay", "record", "live"]
CASSETTE_FILE = "cassette.json"


class CassetteMissError(AIOSError):
    """Replay mode found no recorded response for a call site."""

    code = "cassette.miss"


class Interaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str
    kind: Literal["model", "tool"]
    request_hash: str | None = None
    response: dict[str, JsonValue]


def request_hash(request: Any) -> str:
    """A stable hash of a request (a Pydantic model or JSON-compatible data)."""
    data = request.model_dump(mode="json") if isinstance(request, BaseModel) else request
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class Cassette:
    def __init__(self, path: Path | None = None, mode: CassetteMode = "replay") -> None:
        self.path = path
        self.mode: CassetteMode = mode
        self._interactions: dict[str, Interaction] = {}
        self._plays: Counter[str] = Counter()
        self._lock = threading.Lock()
        if path is not None and path.exists():
            document = json.loads(path.read_text(encoding="utf-8"))
            for item in document.get("interactions", []):
                interaction = Interaction.model_validate(item)
                self._interactions[interaction.site] = interaction

    @classmethod
    def open(cls, location: str | Path, mode: CassetteMode = "replay") -> Cassette:
        """A cassette file, or the ``cassette.json`` inside a directory."""
        path = Path(location)
        if path.suffix != ".json":
            path = path / CASSETTE_FILE
        return cls(path, mode)

    @property
    def replaying(self) -> bool:
        return self.mode == "replay"

    @property
    def recording(self) -> bool:
        return self.mode == "record"

    def play(self, site: str, kind: Literal["model", "tool"], hashed: str | None) -> dict[str, Any]:
        interaction = self._interactions.get(site)
        if interaction is None or interaction.kind != kind:
            raise CassetteMissError(
                f"no recorded {kind} response for {site!r} in {self.path}; record it with "
                "AIOS_CASSETTE_MODE=record"
            )
        if hashed is not None and interaction.request_hash not in (None, hashed):
            logger.info("cassette drift at %s: the request differs from the recording", site)
        with self._lock:
            self._plays[site] += 1
        return dict(interaction.response)

    def record(
        self,
        site: str,
        kind: Literal["model", "tool"],
        hashed: str | None,
        response: dict[str, JsonValue],
    ) -> None:
        with self._lock:
            self._interactions[site] = Interaction(
                site=site, kind=kind, request_hash=hashed, response=response
            )
            self._plays[site] += 1

    @property
    def repeated_calls(self) -> list[str]:
        """Sites served more than once: calls that were made again instead of being reused."""
        return sorted(site for site, count in self._plays.items() if count > 1)

    @property
    def plays(self) -> dict[str, int]:
        return dict(self._plays)

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": 1,
            "interactions": [
                self._interactions[site].model_dump(mode="json")
                for site in sorted(self._interactions)
            ],
        }
        self.path.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )
