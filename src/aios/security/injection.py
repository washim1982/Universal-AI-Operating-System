"""A heuristic prompt-injection classifier (FR-SEC-09).

This is a signal, and nothing relies on it. Taint and policy stop an injected instruction whether
or not anything noticed it, because a pattern list can always be worded around (risk R5). What the
classifier adds is that a person gets told, the audit log gets an entry, and content that arrived
through a door marked trusted, such as a chat message or a trusted tool, taints the run anyway.

Text is normalised first, so that spacing, case, compatibility forms and invisible characters
don't hide a phrase. Hidden characters are themselves a finding: they have no business in a
document that an agent is about to read.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import re
import unicodedata
from collections.abc import Iterator

from aios.core.ports import InjectionClassifier, NoClassifier, Verdict

MAX_CHARS = 100_000
MAX_ENCODED_TOKENS = 20
_HIDDEN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\U000e0000-\U000e007f]")
_ENCODED = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_RULES = {
    "override": re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}"
        r"\b(instruction|rule|prompt|guideline|polic|restriction|safeguard)"
    ),
    "role-change": re.compile(
        r"\byou are now\b|\bact as (an? |the )?(admin|administrator|root|system|developer)\b"
        r"|\bpretend (to be|you are)\b|\bnew instructions?\s*:|\bdeveloper mode\b|\bjailbreak"
    ),
    "system-marker": re.compile(
        r"<\|?\s*(im_start|im_end|system|endoftext)|\[/?inst\]|\bsystem prompt\b"
        r"|</?\s*(system|untrusted|memory)\b|^\s*#+\s*system\b"
    ),
    "hide-from-a-person": re.compile(
        r"\b(do not|don'?t|without|never)\b.{0,20}\b(ask(ing)?( for)?|seek(ing)?|wait(ing)? for)\b"
        r".{0,20}\b(approval|permission|confirmation)"
        r"|\b(do not|don'?t|never)\b.{0,10}\b(tell|inform|notify|mention|alert)\b"
    ),
    "tool-directive": re.compile(
        r"\b(call|invoke|execute|run|use)\b.{0,30}\b[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+\b"
    ),
    "exfiltration": re.compile(
        r"\b(send|post|upload|forward|exfiltrate|leak|append)\b.{0,60}"
        r"(https?://|\bthis (url|link)\b)"
        r"|!\[[^\]]*\]\(https?://"
    ),
}


def _normalised(text: str) -> str:
    plain = unicodedata.normalize("NFKC", _HIDDEN.sub("", text))
    return " ".join(plain.lower().split())


def _decoded(text: str) -> Iterator[str]:
    """What the base64 runs in ``text`` say, if they say anything."""
    for token in _ENCODED.findall(text)[:MAX_ENCODED_TOKENS]:
        try:
            yield base64.b64decode(token, validate=False).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue


class HeuristicClassifier(InjectionClassifier):
    name = "heuristic"

    async def classify(self, text: str, *, source: str) -> Verdict:
        sample = text[:MAX_CHARS]
        plain = _normalised(sample)
        rules = [name for name, rule in _RULES.items() if rule.search(plain)]
        if _HIDDEN.search(sample):
            rules.append("hidden-text")
        for decoded in _decoded(sample):
            if any(rule.search(_normalised(decoded)) for rule in _RULES.values()):
                rules.append("encoded")
                break
        return Verdict(bool(rules), min(1.0, 0.25 + 0.25 * len(rules)), tuple(rules))


def classifier_from(setting: str | None) -> InjectionClassifier:
    """``heuristic``, ``none`` or ``module:attribute`` for a classifier of your own."""
    name = setting or HeuristicClassifier.name
    if name == HeuristicClassifier.name:
        return HeuristicClassifier()
    if name == NoClassifier.name:
        return NoClassifier()
    module, _, attribute = name.partition(":")
    if not attribute:
        raise ValueError(f"unknown injection classifier {name!r}: use heuristic, none or a:b")
    return getattr(importlib.import_module(module), attribute)()
