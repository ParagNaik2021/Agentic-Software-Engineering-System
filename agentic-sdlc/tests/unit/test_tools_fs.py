"""P3 acceptance: path traversal outside the jail raises."""

from pathlib import Path

import pytest

from agentic.tools.fs import JailedFS, PathJailViolation


def test_write_and_read_within_jail_round_trips(tmp_path: Path) -> None:
    fs = JailedFS(tmp_path)
    fs.write_text("src/app.py", "print('hi')")
    assert fs.read_text("src/app.py") == "print('hi')"
    assert fs.exists("src/app.py") is True


def test_path_traversal_with_dotdot_raises(tmp_path: Path) -> None:
    fs = JailedFS(tmp_path / "workspace")
    with pytest.raises(PathJailViolation):
        fs.write_text("../../outside.txt", "escape")


def test_absolute_path_traversal_raises(tmp_path: Path) -> None:
    fs = JailedFS(tmp_path / "workspace")
    outside = tmp_path / "outside.txt"
    with pytest.raises(PathJailViolation):
        fs.write_text(str(outside), "escape")


def test_exists_returns_false_rather_than_raising_for_traversal(tmp_path: Path) -> None:
    fs = JailedFS(tmp_path / "workspace")
    assert fs.exists("../outside.txt") is False


def test_delete_outside_jail_raises(tmp_path: Path) -> None:
    fs = JailedFS(tmp_path / "workspace")
    with pytest.raises(PathJailViolation):
        fs.delete("../../etc/passwd")


def test_list_files_only_reports_within_jail(tmp_path: Path) -> None:
    fs = JailedFS(tmp_path)
    fs.write_text("a.py", "1")
    fs.write_text("sub/b.py", "2")
    assert {p.replace("\\", "/") for p in fs.list_files()} == {"a.py", "sub/b.py"}
