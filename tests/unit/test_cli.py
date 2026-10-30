from pathlib import Path

import pytest

from aios import __version__
from aios.cli.main import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--version"])
    assert exited.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_specs_export_then_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["specs", "check", "--dir", str(tmp_path)]) == 1
    assert "stale" in capsys.readouterr().err

    assert main(["specs", "export", "--dir", str(tmp_path)]) == 0
    assert (tmp_path / "schemas" / "envelope.v1.json").exists()
    assert main(["specs", "check", "--dir", str(tmp_path)]) == 0

    (tmp_path / "event-catalog.md").write_text("edited", encoding="utf-8")
    assert main(["specs", "check", "--dir", str(tmp_path)]) == 1
    assert main(["specs", "export", "--dir", str(tmp_path)]) == 0
    assert "wrote" in capsys.readouterr().out


def test_specs_flags_and_removes_orphaned_schemas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["specs", "export", "--dir", str(tmp_path)]) == 0
    orphan = tmp_path / "schemas" / "events" / "renamed.event.v1.json"
    orphan.write_text("{}", encoding="utf-8")
    capsys.readouterr()

    assert main(["specs", "check", "--dir", str(tmp_path)]) == 1
    assert "renamed.event.v1.json" in capsys.readouterr().err
    assert main(["specs", "export", "--dir", str(tmp_path)]) == 0
    assert "removed" in capsys.readouterr().out
    assert not orphan.exists()
    assert main(["specs", "check", "--dir", str(tmp_path)]) == 0
