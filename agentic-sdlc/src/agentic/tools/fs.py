"""Path-jailed file operations (Section 6.1 SEC-004 / 15.1 risk register:
"Agent writes outside its intended scope"). Every path an agent supplies
is resolved and checked against the jail root before any read, write or
delete — a relative path that resolves outside the root raises
PathJailViolation rather than silently clamping or ignoring it.
"""

from __future__ import annotations

from pathlib import Path


class PathJailViolation(Exception):
    pass


class JailedFS:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            raise PathJailViolation(
                f"path escapes workspace jail ({self.root}): {relative_path}"
            ) from None
        return candidate

    def write_text(self, relative_path: str, content: str) -> Path:
        path = self.resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_text(self, relative_path: str) -> str:
        return self.resolve(relative_path).read_text(encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        try:
            return self.resolve(relative_path).exists()
        except PathJailViolation:
            return False

    def delete(self, relative_path: str) -> None:
        self.resolve(relative_path).unlink()

    def list_files(self, relative_dir: str = ".") -> list[str]:
        base = self.resolve(relative_dir)
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file()
        )
