"""Allowlisted shell execution (Section 6.1 SEC-005)."""

from pathlib import Path

import pytest

from agentic.tools.shell import AllowlistedShell, ShellPolicyViolation


def test_allowlisted_command_runs(tmp_path: Path) -> None:
    shell = AllowlistedShell(tmp_path)
    result = shell.run(["python", "-c", "print('ok')"])
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_disallowed_command_raises(tmp_path: Path) -> None:
    shell = AllowlistedShell(tmp_path)
    with pytest.raises(ShellPolicyViolation):
        shell.run(["curl", "http://example.com"])


def test_empty_command_raises(tmp_path: Path) -> None:
    shell = AllowlistedShell(tmp_path)
    with pytest.raises(ShellPolicyViolation):
        shell.run([])


def test_custom_allowlist_is_respected(tmp_path: Path) -> None:
    shell = AllowlistedShell(tmp_path, allowlist=frozenset({"python"}))
    with pytest.raises(ShellPolicyViolation):
        shell.run(["git", "status"])
