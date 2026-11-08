"""``aios docs reference``: reference pages made from the code, and checked (WP 3.6)."""

from pathlib import Path

import pytest

from aios.cli import docs
from aios.cli.main import _parser, main

ROOT = Path(__file__).resolve().parents[3]


def test_the_pages_say_what_the_code_does(tmp_path: Path) -> None:
    pages = docs.pages(_parser())
    assert set(pages) == {"cli.md", "api.md", "events.md", "manifests.md"}
    assert "## aios init" in pages["cli.md"]
    assert "| `--template` |" in pages["cli.md"]
    assert "| `GET` | `/v1/schemas` |" in pages["api.md"]
    assert "| `human.task.assigned` |" in pages["events.md"]
    assert "| `humans` | list of HumanCapability |" in pages["manifests.md"]


def test_a_stale_page_is_found(tmp_path: Path) -> None:
    parser = _parser()
    assert len(docs.write(tmp_path, parser, check=True)) == 4  # none written yet
    assert not (tmp_path / "cli.md").exists()  # a check writes nothing
    docs.write(tmp_path, parser, check=False)
    assert docs.write(tmp_path, parser, check=True) == []
    (tmp_path / "api.md").write_text("# old\n", encoding="utf-8")
    assert docs.write(tmp_path, parser, check=True) == [tmp_path / "api.md"]


def test_the_committed_pages_are_current(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["docs", "reference", "--check", "--dir", str(ROOT / "docs" / "reference")]) == 0
