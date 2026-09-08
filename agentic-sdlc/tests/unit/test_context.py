"""P1 acceptance: input_hash changes iff an upstream artifact changes."""

from datetime import UTC, datetime

from agentic.core.context import ContextStore
from agentic.core.graph import WorkflowGraph
from agentic.core.models import Artifact, Decision, NodeSpec, SDLCStage, compute_content_hash

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _graph() -> WorkflowGraph:
    graph = WorkflowGraph(root="a")
    graph.add_node(NodeSpec(node_id="a", stage=SDLCStage.REQUIREMENTS))
    graph.add_node(NodeSpec(node_id="b", stage=SDLCStage.ARCHITECTURE, depends_on=["a"]))
    graph.add_node(NodeSpec(node_id="sibling", stage=SDLCStage.DATA_DESIGN, depends_on=["a"]))
    graph.validate()
    return graph


def _artifact(name: str, payload: dict, node: str, version: int = 1, supersedes: str | None = None) -> Artifact:
    return Artifact(
        artifact_id=f"{name}-v{version}",
        name=name,
        kind="spec",
        content_hash=compute_content_hash(payload),
        payload=payload,
        produced_by_node=node,
        produced_by_agent="requirements",
        run_id="run-1",
        created_at=NOW,
        version=version,
        supersedes=supersedes,
    )


def test_view_for_only_includes_transitive_upstream() -> None:
    graph = _graph()
    store = ContextStore(graph)
    store.put(_artifact("normalized_spec", {"x": 1}, node="a"))
    store.put(_artifact("unrelated", {"y": 1}, node="sibling"))

    view = store.view_for("b")

    assert "normalized_spec" in view
    assert "unrelated" not in view


def test_input_hash_stable_when_nothing_changes() -> None:
    graph = _graph()
    store = ContextStore(graph)
    store.put(_artifact("normalized_spec", {"x": 1}, node="a"))

    h1 = store.input_hash("b")
    h2 = store.input_hash("b")
    assert h1 == h2


def test_input_hash_changes_when_upstream_artifact_changes() -> None:
    graph = _graph()
    store = ContextStore(graph)
    v1 = store.put(_artifact("normalized_spec", {"x": 1}, node="a", version=1))
    before = store.input_hash("b")

    store.put(_artifact("normalized_spec", {"x": 2}, node="a", version=2, supersedes=v1))
    after = store.input_hash("b")

    assert before != after


def test_input_hash_unaffected_by_unrelated_sibling_artifact() -> None:
    graph = _graph()
    store = ContextStore(graph)
    store.put(_artifact("normalized_spec", {"x": 1}, node="a"))
    before = store.input_hash("b")

    store.put(_artifact("unrelated", {"y": 999}, node="sibling"))
    after = store.input_hash("b")

    assert before == after


def test_lineage_walks_versions_and_decisions() -> None:
    graph = _graph()
    store = ContextStore(graph)
    v1 = store.put(_artifact("normalized_spec", {"x": 1}, node="a", version=1))
    v2_artifact = _artifact("normalized_spec", {"x": 2}, node="a", version=2, supersedes=v1)
    v2 = store.put(v2_artifact)
    store.record_decision(
        Decision(
            decision_id="d1",
            node_id="a",
            agent="requirements",
            statement="widened scope",
            rationale="clarification answer changed the spec",
            inputs=[v1],
            created_at=NOW,
        )
    )

    lineage = store.lineage(v2)

    artifact_ids = {a.artifact_id for a in lineage.artifacts}
    assert artifact_ids == {v1, v2}
    assert {d.decision_id for d in lineage.decisions} == {"d1"}
    assert lineage.nodes == ["a"]
    assert lineage.agents == ["requirements"]
