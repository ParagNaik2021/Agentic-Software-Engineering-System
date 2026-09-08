"""Git-backed checkpoint/revert (Section 6.4 rollback). Before any
workspace-mutating node runs, checkpoint() commits the current tree;
revert_to() hard-resets to that commit and removes anything created
since — including untracked files — so rollback is a full, byte-for-byte
restoration rather than a partial undo.
"""

from __future__ import annotations

from pathlib import Path

import git


class GitCheckpointer:
    def __init__(self, repo_path: Path) -> None:
        self.repo_path = repo_path
        repo_path.mkdir(parents=True, exist_ok=True)
        git_dir = repo_path / ".git"
        self.repo = git.Repo(repo_path) if git_dir.exists() else git.Repo.init(repo_path)

    def checkpoint(self, label: str) -> str:
        """Commit the current tree (including untracked files) and
        return the commit sha. If nothing changed since the last
        checkpoint, returns the current HEAD sha without an empty
        commit."""
        self.repo.git.add(A=True)
        if self.repo.is_dirty(index=True, working_tree=True, untracked_files=True):
            commit = self.repo.index.commit(f"checkpoint: {label}")
            return commit.hexsha
        if self._has_commits():
            return self.repo.head.commit.hexsha
        commit = self.repo.index.commit(f"checkpoint: {label}")
        return commit.hexsha

    def revert_to(self, commit_sha: str) -> None:
        """Hard-reset to commit_sha and remove every untracked file/dir
        created since, restoring the workspace byte-identically."""
        self.repo.git.reset("--hard", commit_sha)
        self.repo.git.clean("-fd")

    def _has_commits(self) -> bool:
        try:
            _ = self.repo.head.commit
            return True
        except ValueError:
            return False

    def current_sha(self) -> str | None:
        return self.repo.head.commit.hexsha if self._has_commits() else None
