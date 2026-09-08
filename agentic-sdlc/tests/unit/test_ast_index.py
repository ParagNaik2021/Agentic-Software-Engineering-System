"""Python AST index used by the codebase_analyst agent (Section 8.2)."""

from pathlib import Path

from agentic.tools.ast_index import AstIndex


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_build_indexes_imports_functions_classes_and_calls(tmp_path: Path) -> None:
    _write(
        tmp_path, "src/app/services/link_service.py",
        "from app.repositories.link_repository import LinkRepository\n\n"
        "class LinkService:\n"
        "    def create(self, url):\n"
        "        repo = LinkRepository()\n"
        "        return repo.save(url)\n",
    )
    _write(
        tmp_path, "src/app/repositories/link_repository.py",
        "class LinkRepository:\n"
        "    def save(self, url):\n"
        "        return url\n",
    )

    index = AstIndex(tmp_path)
    index.build()

    service = index.modules["src/app/services/link_service.py"]
    assert "app.repositories.link_repository" in service.imports
    assert "LinkService" in service.classes
    assert "create" in service.functions
    assert "LinkRepository" in service.calls or "save" in service.calls


def test_modules_importing_finds_direct_dependents(tmp_path: Path) -> None:
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "x = 1\n")
    _write(tmp_path, "c.py", "x = 2\n")  # unrelated

    index = AstIndex(tmp_path)
    index.build()

    assert index.modules_importing("b") == ["a.py"]


def test_blast_radius_reflects_direct_importers(tmp_path: Path) -> None:
    _write(tmp_path, "app/api/links.py", "from app.services.link_service import LinkService\n")
    _write(tmp_path, "app/services/link_service.py", "class LinkService:\n    pass\n")

    index = AstIndex(tmp_path)
    index.build()

    radius = index.blast_radius("app/services/link_service.py")
    assert "app/api/links.py" in radius


def test_build_skips_files_with_syntax_errors(tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", "def f(:\n")  # invalid syntax
    _write(tmp_path, "fine.py", "def f(): pass\n")

    index = AstIndex(tmp_path)
    index.build()  # must not raise

    assert "broken.py" not in index.modules
    assert "fine.py" in index.modules


def test_build_on_missing_root_produces_empty_index(tmp_path: Path) -> None:
    index = AstIndex(tmp_path / "does_not_exist")
    index.build()
    assert index.modules == {}
