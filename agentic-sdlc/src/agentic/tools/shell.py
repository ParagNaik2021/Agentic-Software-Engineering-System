"""Allowlisted command execution (Section 6.1 SEC-005). Every shell
invocation is run without a shell (shell=False, argv list — never a
string), which also keeps this tool itself clear of SEC-002's "dangerous
constructs" ban on shell=True.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DEFAULT_ALLOWLIST = frozenset({"pytest", "ruff", "mypy", "bandit", "git", "python"})


class ShellPolicyViolation(Exception):
    pass


class AllowlistedShell:
    def __init__(self, workspace_root: Path, allowlist: frozenset[str] | None = None) -> None:
        self.workspace_root = workspace_root
        self.allowlist = allowlist if allowlist is not None else DEFAULT_ALLOWLIST

    def run(self, args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        if not args:
            raise ShellPolicyViolation("empty command")
        program = Path(args[0]).name
        if program not in self.allowlist:
            raise ShellPolicyViolation(f"command not in allowlist: {program}")
        return subprocess.run(
            args,
            cwd=self.workspace_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
