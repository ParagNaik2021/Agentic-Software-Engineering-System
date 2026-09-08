"""P0 smoke tests: the package imports, settings load, and the CLI is wired."""

from typer.testing import CliRunner

from agentic.cli import app
from agentic.config import LLMMode, Settings, get_settings

runner = CliRunner()


def test_settings_load_with_defaults() -> None:
    settings = Settings()
    assert settings.llm_mode == LLMMode.REPLAY
    assert settings.max_concurrent_nodes == 4
    assert 0 < settings.coverage_floor <= 1


def test_get_settings_is_cached_singleton() -> None:
    assert get_settings() is get_settings()


def test_cli_app_registers_expected_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "run",
        "resume",
        "approve",
        "reject",
        "report",
        "lineage",
        "replan",
        "halt",
        "verify-audit",
        "approvals",
    ):
        assert name in result.output


def test_approvals_subcommands_registered() -> None:
    result = runner.invoke(app, ["approvals", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "show" in result.output
