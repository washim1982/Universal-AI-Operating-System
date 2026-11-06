"""The threat model stays honest: what it names has to exist (WP 2.3)."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODEL = (ROOT / "docs" / "threat-model.md").read_text("utf-8")
CASES = yaml.safe_load((ROOT / "tests" / "redteam" / "corpus.yaml").read_text("utf-8"))["cases"]
TESTS = "\n".join(path.read_text("utf-8") for path in (ROOT / "tests").rglob("test_*.py"))


def test_every_case_it_names_exists() -> None:
    ids = {case["id"] for case in CASES}
    named = set(re.findall(r"\b(?:DI|II|TR|MP|EX|DC|PL)-\d\d\b", MODEL))
    assert named
    assert named - ids == set()


def test_every_case_is_named_in_it_or_covered_by_its_family() -> None:
    families = {case["id"].split("-")[0] for case in CASES}
    missing = {family for family in families if f"{family}-" not in MODEL}
    assert missing == set()


def test_every_test_it_names_exists() -> None:
    named = set(re.findall(r"`(test_[a-z0-9_]+)`", MODEL))
    assert named
    assert {name for name in named if f"def {name}(" not in TESTS} == set()


def test_every_corpus_case_has_an_owasp_id_it_explains() -> None:
    used = {case["owasp"] for case in CASES}
    assert {owasp for owasp in used if f"| {owasp} |" not in MODEL} == set()
