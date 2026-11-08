"""The stand-in model an App's tests use (WP 3.6)."""

import pytest

from aios.core.ports import Message, ModelRequest
from aios.models import ModelEntry, ProviderError
from aios.testing.models import Reply, ScriptedModel

ENTRY = ModelEntry(id="any/model", model="model")


def _asked(text: str = "hi") -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=text)])


async def test_it_says_what_it_is_told_in_order() -> None:
    model = ScriptedModel(
        "plain", {"answer": 42}, Reply.call("kb.article.find", q="x"), Reply.call("kb.a.b")
    )
    assert (await model.complete(ENTRY, _asked())).text == "plain"
    assert (await model.complete(ENTRY, _asked())).text == '{"answer": 42}'
    [call] = (await model.complete(ENTRY, _asked())).tool_calls
    assert (call.name, call.arguments) == ("kb__article__find", {"q": "x"})
    [other] = (await model.complete(ENTRY, _asked())).tool_calls
    assert call.id != other.id  # each call has an id of its own
    assert len(model.requests) == 4
    with pytest.raises(ProviderError, match="no reply left for call 5") as raised:
        await model.complete(ENTRY, _asked())
    assert raised.value.retryable is False
