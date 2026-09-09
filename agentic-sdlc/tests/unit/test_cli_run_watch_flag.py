"""`agentic run <workflow> --watch`: starts the run then opens the same
GUI `agentic watch <run_id>` would attach to separately, instead of
running headless. Tests the CLI wiring only — launch_watch_window itself
opens a real Tk window and is deliberately untested here (no display in
CI); its logic is covered by test_watch_controller.py.
"""

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

import agentic.config as config_module
from agentic import cli
from agentic.core.models import RunStatus
from agentic.core.states import NodeStatus

runner = CliRunner()


def test_run_with_watch_flag_starts_the_run_and_hands_off_to_the_gui(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("AGENTIC_WORKSPACE_DIR", str(tmp_path / "workspace"))
    config_module._settings = None  # force Settings() to re-read the env vars above

    captured = {}

    def fake_launch(engine):  # noqa: ANN001
        captured["engine"] = engine

    with patch("agentic.gui.watch.launch_watch_window", side_effect=fake_launch):
        result = runner.invoke(cli.app, ["run", "greenfield", "--mode", "replay", "--watch"])

    config_module._settings = None  # don't leak the tmp_path-scoped Settings to later tests

    assert result.exit_code == 0, result.output
    assert "opened watch GUI" in result.output

    engine = captured["engine"]
    assert engine.state is not None
    assert engine.state.status == RunStatus.RUNNING
    # engine.start() only initializes state — --watch must hand off before
    # any node executes, exactly like a fresh `agentic run` would pause
    # for the GUI to drive, not run the whole workflow headless first.
    assert all(nr.status == NodeStatus.PENDING for nr in engine.state.nodes.values())
    assert all(nr.attempt == 0 for nr in engine.state.nodes.values())


def test_run_without_watch_flag_still_runs_headless(tmp_path: Path, monkeypatch) -> None:
    """--watch must be strictly additive: plain `agentic run` (no flag)
    keeps running the workflow to completion/pause and printing the
    table, unchanged."""
    monkeypatch.setenv("AGENTIC_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("AGENTIC_WORKSPACE_DIR", str(tmp_path / "workspace"))
    config_module._settings = None

    with patch("agentic.gui.watch.launch_watch_window") as fake_launch:
        result = runner.invoke(cli.app, ["run", "greenfield", "--mode", "replay"])

    config_module._settings = None

    assert result.exit_code == 0, result.output
    fake_launch.assert_not_called()
    assert "AWAITING_APPROVAL" in result.output  # greenfield pauses at design.review
