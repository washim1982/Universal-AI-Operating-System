"""``aios init``: every template makes an App whose own tests pass (WP 3.6; FR-CP-06, M3 exit 1)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from aios.cli.main import main
from aios.cli.scaffold import TEMPLATES, add_agent, add_tool, init
from aios.controlplane import load_app

TIMEOUT = 240


def _pytest(app: Path) -> subprocess.CompletedProcess[str]:
    """The App's own tests, run as its developer would: ``pytest``, in its folder."""
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("AIOS_CASSETTE_MODE", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


@pytest.mark.timeout(TIMEOUT + 30)
@pytest.mark.parametrize("template", TEMPLATES)
def test_every_template_makes_an_app_whose_tests_pass(tmp_path: Path, template: str) -> None:
    root = init("my-shop", tmp_path, template)
    assert (root / "src" / "my_shop" / "__init__.py").is_file()
    text = "".join(p.read_text("utf-8") for p in root.rglob("*") if p.is_file())
    assert "aios_template_app" not in text
    assert "aios-template-app" not in text
    assert "Aios Template App" not in text
    app = load_app(root)  # it loads, as `aios dev --app` would
    assert app[0].metadata.name == "my-shop"  # type: ignore[union-attr]
    done = _pytest(root)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-2000:]
    assert " passed" in done.stdout


def test_a_template_must_exist(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no template 'helpdesk'"):
        init("my-shop", tmp_path, "helpdesk")
    with pytest.raises(SystemExit, match="lowercase"):
        init("My Shop", tmp_path)


@pytest.mark.parametrize("template", ["python", "langgraph", "openai-agents"])
def test_an_agent_is_added_to_an_app(tmp_path: Path, template: str) -> None:
    root = init("my-shop", tmp_path)
    done = add_agent("review-agent", root, template)
    assert "added review-agent to spec.agents in manifests/app.yaml" in done
    app_yaml = (root / "manifests" / "app.yaml").read_text("utf-8")
    assert "agents: [assistant-agent, review-agent]" in app_yaml
    manifest = (root / "manifests" / "review-agent.yaml").read_text("utf-8")
    assert "capabilities: [{name: review}]" in manifest
    module = root / "src" / "my_shop" / "review_agent.py"
    compile(module.read_text("utf-8"), str(module), "exec")  # it is Python
    names = [m.metadata.name for m in load_app(root)]
    assert "review-agent" in names
    with pytest.raises(SystemExit, match="already exists"):
        add_agent("review-agent", root, template)


def test_a_tool_is_added_to_an_app(tmp_path: Path) -> None:
    root = init("my-shop", tmp_path)
    done = add_tool("orders.status.find", root)
    tools = root / "src" / "my_shop" / "tools.py"
    assert done[0] == f"created {tools.as_posix()}"
    assert any("AIOS_TOOL_PROVIDERS=my_shop.tools:providers" in line for line in done)
    more = add_tool("orders.refund.create", root)
    assert "add create to a provider in providers() there" in more
    text = tools.read_text("utf-8")
    compile(text, str(tools), "exec")
    assert '"orders.status.find"' in text
    assert '"orders.refund.create"' in text
    with pytest.raises(SystemExit, match="already"):
        add_tool("orders.refund.create", root)
    with pytest.raises(SystemExit, match="three parts"):
        add_tool("refund", root)


def test_outside_an_app_folder_it_says_so(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"no manifests/app.yaml"):
        add_agent("review-agent", tmp_path)


def test_the_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["init", "my-shop", "--dir", str(tmp_path), "--template", "rag"]) == 0
    assert "from the rag template" in capsys.readouterr().out
    app = str(tmp_path / "my-shop")
    assert main(["init", "agent", "summary-agent", "--app", app, "--capability", "summing"]) == 0
    assert "summary-agent" in capsys.readouterr().out
    assert main(["init", "tool", "orders.status.find", "--app", app]) == 0
    with pytest.raises(SystemExit, match="needs a name"):
        main(["init", "agent", "--app", app])
