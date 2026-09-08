"""Python AST index (Section 8.2, codebase_analyst): a lightweight,
read-only index of a workspace's Python modules — imports, definitions
and a call slice — used for brownfield impact analysis. Read-only by
construction: it has no write method, matching the L0_OBSERVE ceiling
of the only agent that uses it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ModuleInfo:
    path: str
    imports: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)


class AstIndex:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: dict[str, ModuleInfo] = {}

    def build(self) -> None:
        self.modules = {}
        if not self.root.exists():
            return
        for path in sorted(self.root.rglob("*.py")):
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except (SyntaxError, UnicodeDecodeError):
                continue
            self.modules[rel] = self._index_module(rel, tree)

    @staticmethod
    def _index_module(rel: str, tree: ast.Module) -> ModuleInfo:
        info = ModuleInfo(path=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                info.imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                info.imports.append(node.module)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                info.functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                info.classes.append(node.name)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    info.calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    info.calls.append(node.func.attr)
        return info

    def modules_importing(self, target_module: str) -> list[str]:
        return [
            m.path for m in self.modules.values()
            if any(target_module == imp or imp.startswith(target_module + ".") for imp in m.imports)
        ]

    def modules_calling(self, name: str) -> list[str]:
        return [m.path for m in self.modules.values() if name in m.calls]

    def blast_radius(self, changed_module_path: str) -> set[str]:
        """Modules that (directly) import the changed module — a one-hop
        impact estimate, sufficient at the scale of the generated
        shortener service. Matches on the module's own name as the
        final dotted component of an import (e.g. changing
        app/services/link_service.py matches an import of
        "app.services.link_service" or a bare "link_service")."""
        stem = Path(changed_module_path).stem
        return {
            m.path for m in self.modules.values()
            if any(imp == stem or imp.endswith("." + stem) for imp in m.imports)
        }
