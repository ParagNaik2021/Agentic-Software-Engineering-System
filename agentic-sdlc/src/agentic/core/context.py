"""ContextStore: namespaced, versioned blackboard (Section 5.4).

Agents never read each other directly; they read and write artifacts
through this store, which enforces provenance and makes input_hash a
meaningful signal for re-plan invalidation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, Decision


@dataclass
class ContextView:
    """Only artifacts reachable from one node's transitive upstream."""

    artifacts: dict[str, Artifact] = field(default_factory=dict)

    def get(self, name: str) -> Artifact | None:
        return self.artifacts.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.artifacts


@dataclass
class LineageGraph:
    """Full ancestry of one artifact: every artifact, decision, node and
    agent that contributed to it, walked back through Decision.inputs
    and Artifact.supersedes."""

    artifacts: list[Artifact]
    decisions: list[Decision]
    nodes: list[str]
    agents: list[str]


class ContextStore:
    """`persist_dir`, when given, mirrors every put() to
    runs/<run_id>/artifacts/ (Section 4.1) so a fresh process can
    rehydrate the same artifact content via load_from_disk() instead of
    only recovering artifact *ids* from the event log."""

    def __init__(self, graph: WorkflowGraph, persist_dir: Path | None = None) -> None:
        self._graph = graph
        self._by_id: dict[str, Artifact] = {}
        self._by_name: dict[str, list[Artifact]] = {}
        self._decisions: dict[str, Decision] = {}
        self._decisions_by_node: dict[str, list[Decision]] = {}
        self.persist_dir = persist_dir
        if persist_dir is not None:
            persist_dir.mkdir(parents=True, exist_ok=True)

    def put(self, artifact: Artifact) -> str:
        self._by_id[artifact.artifact_id] = artifact
        self._by_name.setdefault(artifact.name, []).append(artifact)
        if self.persist_dir is not None:
            path = self.persist_dir / f"{artifact.artifact_id}.json"
            path.write_text(artifact.model_dump_json(), encoding="utf-8")
        return artifact.artifact_id

    def get_by_id(self, artifact_id: str) -> Artifact | None:
        return self._by_id.get(artifact_id)

    def get_decision(self, decision_id: str) -> Decision | None:
        return self._decisions.get(decision_id)

    def load_from_disk(self) -> None:
        """Rehydrate from persist_dir. Used when resuming a run in a
        fresh process: rebuild the ContextStore before replaying events."""
        if self.persist_dir is None or not self.persist_dir.exists():
            return
        for path in sorted(self.persist_dir.glob("*.json")):
            artifact = Artifact.model_validate_json(path.read_text(encoding="utf-8"))
            self._by_id[artifact.artifact_id] = artifact
            self._by_name.setdefault(artifact.name, []).append(artifact)

    def get(self, name: str, version: int | None = None) -> Artifact:
        versions = self._by_name.get(name)
        if not versions:
            raise KeyError(f"no artifact named '{name}'")
        if version is None:
            return max(versions, key=lambda a: a.version)
        for a in versions:
            if a.version == version:
                return a
        raise KeyError(f"no version {version} of artifact '{name}'")

    def view_for(self, node_id: str) -> ContextView:
        """Only artifacts produced by nodes in node_id's transitive
        upstream, latest version per name. Prevents context bleed and
        makes input_hash meaningful."""
        upstream = self._graph.upstream_of(node_id)
        view: dict[str, Artifact] = {}
        for name, versions in self._by_name.items():
            candidates = [a for a in versions if a.produced_by_node in upstream]
            if candidates:
                view[name] = max(candidates, key=lambda a: a.version)
        return ContextView(artifacts=view)

    def record_decision(self, d: Decision) -> str:
        self._decisions[d.decision_id] = d
        self._decisions_by_node.setdefault(d.node_id, []).append(d)
        return d.decision_id

    def lineage(self, artifact_id: str) -> LineageGraph:
        visited_artifacts: dict[str, Artifact] = {}
        visited_decisions: dict[str, Decision] = {}
        nodes: set[str] = set()
        agents: set[str] = set()
        stack = [artifact_id]

        while stack:
            aid = stack.pop()
            if aid in visited_artifacts or aid not in self._by_id:
                continue
            artifact = self._by_id[aid]
            visited_artifacts[aid] = artifact
            nodes.add(artifact.produced_by_node)
            agents.add(artifact.produced_by_agent)
            if artifact.supersedes:
                stack.append(artifact.supersedes)
            for d in self._decisions_by_node.get(artifact.produced_by_node, []):
                visited_decisions[d.decision_id] = d
                stack.extend(d.inputs)

        return LineageGraph(
            artifacts=list(visited_artifacts.values()),
            decisions=list(visited_decisions.values()),
            nodes=sorted(nodes),
            agents=sorted(agents),
        )

    def input_hash(self, node_id: str) -> str:
        """sha256 over sorted (name, version, content_hash) of the node's
        context view. Drives invalidation in core/replan.py."""
        view = self.view_for(node_id)
        parts = sorted(
            f"{name}:{a.version}:{a.content_hash}" for name, a in view.artifacts.items()
        )
        canonical = "|".join(parts)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
