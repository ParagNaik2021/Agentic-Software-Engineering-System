"""RunStore: SQLite-backed persistence for the RunState projection
(Section 4.1's runs/<run_id>/state.db).

The event log (events.py) is the source of truth; RunStore caches the
current projection so the CLI can answer `agentic report` / `agentic
approvals list` without replaying the full log on every call, and gives
`resume` a place to rebuild into after a process restart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
    update,
)

from agentic.core.events import EventLog
from agentic.core.models import RunState


class RunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{db_path}", future=True)
        self.metadata = MetaData()
        self.state_table = Table(
            "run_state",
            self.metadata,
            Column("run_id", String, primary_key=True),
            Column("state_json", Text, nullable=False),
            Column("updated_at", String, nullable=False),
        )
        self.checkpoints_table = Table(
            "checkpoints",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("run_id", String, nullable=False),
            Column("label", String, nullable=False),
            Column("state_json", Text, nullable=False),
            Column("created_at", String, nullable=False),
        )
        self.metadata.create_all(self.engine)

    def save(self, state: RunState) -> None:
        payload = state.model_dump_json()
        now = datetime.now(UTC).isoformat()
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(self.state_table.c.run_id).where(
                    self.state_table.c.run_id == state.run_id
                )
            ).first()
            if existing:
                conn.execute(
                    update(self.state_table)
                    .where(self.state_table.c.run_id == state.run_id)
                    .values(state_json=payload, updated_at=now)
                )
            else:
                conn.execute(
                    insert(self.state_table).values(
                        run_id=state.run_id, state_json=payload, updated_at=now
                    )
                )

    def load(self, run_id: str) -> RunState | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.state_table.c.state_json).where(
                    self.state_table.c.run_id == run_id
                )
            ).first()
        if row is None:
            return None
        return RunState.model_validate_json(row[0])

    def checkpoint(self, state: RunState, label: str) -> None:
        payload = state.model_dump_json()
        now = datetime.now(UTC).isoformat()
        with self.engine.begin() as conn:
            conn.execute(
                insert(self.checkpoints_table).values(
                    run_id=state.run_id,
                    label=label,
                    state_json=payload,
                    created_at=now,
                )
            )

    def resume(self, run_id: str, event_log: EventLog) -> RunState:
        """Rebuild the authoritative state from the event log (not from
        whatever snapshot happens to be cached) and persist it."""
        state = event_log.replay_to_state()
        self.save(state)
        return state
