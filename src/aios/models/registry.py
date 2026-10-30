"""The model registry: configuration, not code (ADR-008, FR-MOD-02, FR-MOD-07).

Each entry says which provider serves a model, the provider's model id, where it runs
(``public`` or ``private`` deployment, for FR-SEC-11), and its prices, so the kernel can
attribute every token and dollar (NFR-14). ``<…>`` placeholder entries stand in for models a
deployment chooses later (requirements §7.10); they can be routed to in replay mode, but a live
call to one fails until it is filled in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aios.core.catalog import Usage


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class Prices(_Model):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    long_context_threshold: int | None = Field(
        default=None, ge=1, alias="longContextThreshold", description="Prompt tokens"
    )
    long_context_input: float | None = Field(default=None, ge=0, alias="longContextInput")
    long_context_output: float | None = Field(default=None, ge=0, alias="longContextOutput")


class ModelEntry(_Model):
    id: str = Field(description="<provider>/<name>, as policies name it")
    model: str = Field(description="The provider's model id")
    deployment: Literal["public", "private"] = "public"
    context_window: int | None = Field(default=None, ge=1, alias="contextWindow")
    prices: Prices | None = None
    placeholder: bool = False
    refusal_fallback: bool = Field(
        default=False,
        alias="refusalFallback",
        description="Use the provider's server-side refusal fallback (Anthropic)",
    )

    @property
    def provider(self) -> str:
        return self.id.split("/", 1)[0]

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        prices = self.prices
        if prices is None:
            return 0.0
        input_price, output_price = prices.input, prices.output
        threshold = prices.long_context_threshold
        if threshold is not None and input_tokens > threshold:
            input_price = prices.long_context_input or input_price
            output_price = prices.long_context_output or output_price
        return (input_tokens * input_price + output_tokens * output_price) / 1_000_000

    def usage(self, input_tokens: int, output_tokens: int) -> Usage:
        return Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=round(self.cost(input_tokens, output_tokens), 8),
        )


class ModelRegistry:
    def __init__(self, entries: Iterable[ModelEntry]) -> None:
        self._entries = {entry.id: entry for entry in entries}

    @classmethod
    def from_yaml(cls, source: str | Path) -> ModelRegistry:
        text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        document: Mapping[str, Any] = yaml.safe_load(text) or {}
        return cls(ModelEntry.model_validate(item) for item in document.get("models", []))

    @classmethod
    def default(cls) -> ModelRegistry:
        """The registry shipped with AIOS (``aios/models/defaults/registry.yaml``)."""
        text = resources.files("aios.models").joinpath("defaults/registry.yaml").read_text("utf-8")
        return cls.from_yaml(text)

    def get(self, model_id: str) -> ModelEntry | None:
        return self._entries.get(model_id)

    def add(self, entry: ModelEntry) -> None:
        self._entries[entry.id] = entry

    def __iter__(self) -> Any:
        return iter(self._entries.values())
