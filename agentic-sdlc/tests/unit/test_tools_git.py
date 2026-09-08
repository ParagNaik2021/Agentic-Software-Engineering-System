"""P3 acceptance: rollback restores a workspace byte-identically after a
partial mutation."""

from pathlib import Path

from agentic.tools.git import GitCheckpointer


def test_checkpoint_then_revert_restores_workspace_byte_identically(tmp_path: Path) -> None:
    checkpointer = GitCheckpointer(tmp_path)

    (tmp_path / "app.py").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    checkpoint_sha = checkpointer.checkpoint("initial state")

    original_app = (tmp_path / "app.py").read_bytes()
    original_readme = (tmp_path / "README.md").read_bytes()

    # Simulate a partial mutation: modify an existing file, add a new
    # (untracked) file, and crash before completing — no second checkpoint.
    (tmp_path / "app.py").write_text("version = 2\nBROKEN\n", encoding="utf-8")
    (tmp_path / "new_untracked_file.py").write_text("oops\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "also_new.py").write_text("oops2\n", encoding="utf-8")

    checkpointer.revert_to(checkpoint_sha)

    assert (tmp_path / "app.py").read_bytes() == original_app
    assert (tmp_path / "README.md").read_bytes() == original_readme
    assert not (tmp_path / "new_untracked_file.py").exists()
    assert not (tmp_path / "sub").exists()


def test_checkpoint_is_idempotent_when_nothing_changed(tmp_path: Path) -> None:
    checkpointer = GitCheckpointer(tmp_path)
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    sha1 = checkpointer.checkpoint("first")
    sha2 = checkpointer.checkpoint("second call, nothing changed")

    assert sha1 == sha2


def test_current_sha_reflects_latest_checkpoint(tmp_path: Path) -> None:
    checkpointer = GitCheckpointer(tmp_path)
    assert checkpointer.current_sha() is None

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    sha = checkpointer.checkpoint("first")

    assert checkpointer.current_sha() == sha
