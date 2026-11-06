"""Keeping outside content apart from instructions (FR-SEC-09).

A model reads a prompt as one stream of text, so nothing here can make it ignore what a document
says. What this can do is make the boundary visible and impossible to forge: content can't close
its own wrapper and carry on as if it were the prompt. What actually stops a fooled model is
outside the model: taint, policy and approvals.
"""

from __future__ import annotations

import re

_WRAPPER = re.compile(r"<\s*/?\s*untrusted", re.IGNORECASE)


def quoted(text: str, *, source: str) -> str:
    """``text`` marked as data from ``source``, for a prompt."""
    label = (
        " ".join(source.split()).replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return f'<untrusted source="{label}">\n{_WRAPPER.sub("(untrusted", text)}\n</untrusted>'
