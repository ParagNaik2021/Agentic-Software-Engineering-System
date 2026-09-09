"""`agentic watch <run_id>`: a Tkinter window that replaces the manual
`agentic approvals show` / `agentic approve` / `agentic resume` sequence
with a pop-up showing the decision package and Approve/Reject buttons.

Two kinds of checkpoint get two different panels, because they ask the
human for different things even though both pause the node at
AWAITING_APPROVAL:

  * an approval gate (design.review, release.readiness) -> the decision
    package plus Approve / Reject;
  * a CLARIFICATION-stage node (workflows/ambiguous.py's req.clarify) ->
    the ambiguity agent's actual questions, a text box, and Submit.

Which panel a node gets is decided by WatchController.is_clarification
(the node's graph stage), never by its status.

Stdlib only (tkinter ships with CPython) — no new dependency. All engine
interaction goes through WatchController (watch_controller.py), which
calls the same runtime.approve_and_save / reject_and_save /
submit_clarification_and_save functions the CLI commands use; this module
is presentation only.

Display pacing (GUI_DISPLAY_DELAY_MS below) falls under that
presentation-only remit: in replay mode a whole design fan-out settles in
milliseconds, so this window reveals the run's *already recorded*
transitions one at a time instead of snapping each node to its final
status. Nothing here slows the engine, the event log or any recorded
timing — the delay governs only how fast the GUI shows what it already
knows.
"""

from __future__ import annotations

import asyncio
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import scrolledtext, ttk

from agentic.config import Settings
from agentic.core.engine import Engine
from agentic.governance.approvals import ApprovalNotPermitted
from agentic.governance.rendering import render_text
from agentic.gui.watch_controller import WatchController, WatchSnapshot
from agentic.runtime import load_engine

POLL_INTERVAL_MS = 1500

# --- cosmetic display pacing -------------------------------------------
# Confined to this module. The engine, the event log and every metric
# (MTTR, latency, token counts) are untouched: the run executes at full
# speed and the recorded timestamps are the real ones. Set
# GUI_DISPLAY_DELAY_MS = 0 to disable staggering entirely and have the
# tree jump straight to current state, as it did before.
GUI_DISPLAY_DELAY_MS = 800       # between two revealed node-status changes
GUI_PROCESSING_DELAY_MS = 1200   # "Processing…" dwell after Approve/Reject/Submit

# A long run banks up more recorded transitions than are worth walking
# one-by-one (a full ambiguous run records ~83). Anything beyond this
# backlog is revealed at once, so attaching to a finished run catches up
# instead of lagging minutes behind it.
GUI_MAX_REVEAL_BACKLOG = 10


def _format_questions(questions: list[dict]) -> str:
    """Render the clarification_questions artifact for the read-only pane.
    Kept a module-level function so it can be checked without a display."""
    if not questions:
        return "(no clarification_questions artifact found - answer in free text)"
    lines: list[str] = []
    for q in questions:
        lines.append(f"[{q.get('id', '?')}] {q.get('question', '')}")
        if q.get("proposed_default"):
            lines.append(f"      proposed default: {q['proposed_default']}")
        impact, uncertainty = q.get("impact"), q.get("uncertainty")
        if impact is not None and uncertainty is not None:
            lines.append(f"      impact {impact} / uncertainty {uncertainty}")
        lines.append("")
    return "\n".join(lines)


class WatchWindow:
    def __init__(self, root: tk.Tk, controller: WatchController) -> None:
        self.root = root
        self.controller = controller
        self._busy = False
        self._shown_for: str | None = None  # node_id the approval panel currently displays
        self._clarify_shown_for: str | None = None  # node_id the clarification panel displays

        # Display state: what the tree currently shows, which deliberately
        # lags the real RunState by up to GUI_DISPLAY_DELAY_MS per step.
        self._displayed: dict[str, tuple[str, int]] = {}  # node_id -> (status, attempt)
        self._revealed = 0  # how many recorded transitions have been shown
        self._snapshot: WatchSnapshot | None = None
        self._processing = False  # a human decision is being applied

        root.title(f"agentic watch — {controller.engine.run_id}")
        root.geometry("820x660")

        self.status_var = tk.StringVar(value="loading…")
        ttk.Label(root, textvariable=self.status_var, font=("TkDefaultFont", 11, "bold")).pack(
            anchor="w", padx=10, pady=(10, 4)
        )

        self.error_var = tk.StringVar(value="")
        self.error_label = ttk.Label(
            root, textvariable=self.error_var, foreground="#b91c1c", wraplength=780
        )
        self.error_label.pack(anchor="w", padx=10)

        self.replan_var = tk.StringVar(value="")
        self.replan_label = ttk.Label(
            root, textvariable=self.replan_var, foreground="#b45309", wraplength=780
        )
        self.replan_label.pack(anchor="w", padx=10, pady=(0, 4))

        self.tree = ttk.Treeview(root, columns=("status",), show="tree headings", height=10)
        self.tree.heading("#0", text="Node")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=260)
        self.tree.column("status", width=160)
        self.tree.tag_configure("invalidated", foreground="#b45309")
        self.tree.tag_configure("running", foreground="#1d4ed8")
        self.tree.pack(fill="x", padx=10)

        # --- approval-gate panel -------------------------------------------
        self.approval_frame = ttk.Frame(root)
        self.approval_label = ttk.Label(self.approval_frame, font=("TkDefaultFont", 10, "bold"))
        self.approval_label.pack(anchor="w")
        self.package_text = scrolledtext.ScrolledText(self.approval_frame, height=16, wrap="word")
        self.package_text.pack(fill="both", expand=True, pady=(4, 4))
        note_row = ttk.Frame(self.approval_frame)
        note_row.pack(fill="x")
        ttk.Label(note_row, text="Note:").pack(side="left")
        self.note_var = tk.StringVar()
        ttk.Entry(note_row, textvariable=self.note_var).pack(side="left", fill="x", expand=True, padx=6)
        button_row = ttk.Frame(self.approval_frame)
        button_row.pack(fill="x", pady=(6, 0))
        self.approve_button = ttk.Button(button_row, text="Approve", command=self._on_approve)
        self.approve_button.pack(side="left")
        self.reject_button = ttk.Button(button_row, text="Reject", command=self._on_reject)
        self.reject_button.pack(side="left", padx=6)

        # --- clarification panel (different question, so different controls) --
        self.clarify_frame = ttk.Frame(root)
        self.clarify_label = ttk.Label(
            self.clarify_frame, font=("TkDefaultFont", 10, "bold"), foreground="#b45309"
        )
        self.clarify_label.pack(anchor="w")
        ttk.Label(
            self.clarify_frame,
            text=(
                "The requirement is underspecified. These questions were raised by the ambiguity "
                "agent; nodes downstream already ran on the proposed defaults and will be "
                "re-planned against your answer."
            ),
            wraplength=760,
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 4))
        self.questions_text = scrolledtext.ScrolledText(self.clarify_frame, height=10, wrap="word")
        self.questions_text.pack(fill="both", expand=True, pady=(0, 6))
        ttk.Label(self.clarify_frame, text="Your answer:").pack(anchor="w")
        self.answer_text = tk.Text(self.clarify_frame, height=3, wrap="word")
        self.answer_text.pack(fill="x", pady=(2, 6))
        clarify_buttons = ttk.Frame(self.clarify_frame)
        clarify_buttons.pack(fill="x")
        self.submit_button = ttk.Button(
            clarify_buttons, text="Submit", command=self._on_submit_clarification
        )
        self.submit_button.pack(side="left")
        ttk.Button(
            clarify_buttons, text="Use proposed defaults", command=self._on_use_defaults
        ).pack(side="left", padx=6)

        self.root.after(0, self._advance_async)
        self.root.after(POLL_INTERVAL_MS, self._tick)
        self.root.after(GUI_DISPLAY_DELAY_MS, self._reveal_tick)

    # ------------------------------------------------------------------
    # display pacing (cosmetic only)
    # ------------------------------------------------------------------
    def _reveal_tick(self) -> None:
        """Reveals the next recorded transition(s), then re-renders. On its
        own timer, so the reveal cadence is independent of how often the
        run state is polled."""
        self._reveal_next()
        if self._snapshot is not None:
            self._render()
        self.root.after(max(GUI_DISPLAY_DELAY_MS, 50), self._reveal_tick)

    def _reveal_next(self) -> None:
        """Advances the display by one recorded transition — the real
        NODE_STATE_CHANGED sequence from the log, not interpolated
        guesses, so a staggered tree still shows what actually happened."""
        transitions = self.controller.state_transitions()
        backlog = len(transitions) - self._revealed
        if backlog <= 0:
            return
        if GUI_DISPLAY_DELAY_MS <= 0:
            step = backlog
        elif backlog <= GUI_MAX_REVEAL_BACKLOG:
            step = 1
        else:
            step = backlog - GUI_MAX_REVEAL_BACKLOG + 1
        for transition in transitions[self._revealed : self._revealed + step]:
            self._displayed[transition["node_id"]] = (transition["to"], transition["attempt"])
        self._revealed += step

    def _caught_up(self) -> bool:
        """True once every recorded transition has been shown. The decision
        panels wait for this, so a checkpoint is never offered while the
        tree is still mid-progression."""
        return self._revealed >= len(self.controller.state_transitions())

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    def _render_snapshot(self, snapshot: WatchSnapshot) -> None:
        """Accepts a freshly polled snapshot. What reaches the screen is
        decided by _render, which the reveal timer also drives."""
        self._snapshot = snapshot
        self._render()

    def _render(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return

        run_id = snapshot.state.run_id
        if self._processing:
            self.status_var.set(f"Run {run_id} — Processing decision…")
        elif not self._caught_up():
            self.status_var.set(f"Run {run_id} — {snapshot.state.status.value} (advancing…)")
        else:
            self.status_var.set(f"Run {run_id} — {snapshot.state.status.value}")

        self.tree.delete(*self.tree.get_children())
        for node_id in snapshot.state.nodes:
            status, attempt = self._displayed.get(node_id, ("PENDING", 0))
            tags: tuple[str, ...] = ()
            if status in ("INVALIDATED", "PENDING") and attempt >= 1:
                # a PENDING node that has already run once is re-queued
                # work, not work that never started
                tags = ("invalidated",)
            elif status in ("RUNNING", "READY"):
                tags = ("running",)
            suffix = f"  (pass {attempt})" if attempt > 1 else ""
            self.tree.insert(
                "", "end", iid=node_id, text=node_id, values=(status + suffix,), tags=tags
            )
        self._render_replan_banner()

        # Hold the panels back while a decision is being applied or the
        # display is still catching up.
        if self._processing or not self._caught_up():
            return

        # Clarification takes precedence: if both kinds are pending, the
        # question that wants the human's own words is the more useful
        # panel to surface first.
        if snapshot.is_terminal:
            # A HALTED/FAILED/SUCCEEDED run cannot accept a decision, and a
            # halted run leaves its gate node sitting at AWAITING_APPROVAL
            # forever — so re-offering Approve here is what produced
            # repeated grants against an already-stopped run.
            self._hide_approval_panel()
            self._hide_clarification_panel()
            self._busy = False
        elif snapshot.awaiting_clarification:
            self._hide_approval_panel()
            self._show_clarification_panel(snapshot.awaiting_clarification[0])
        elif snapshot.awaiting_approval:
            self._hide_clarification_panel()
            self._show_approval_panel(snapshot.awaiting_approval[0])
        else:
            self._hide_approval_panel()
            self._hide_clarification_panel()

    def _render_replan_banner(self) -> None:
        """Surfaces the re-plan in this window even when it completed
        between two polls — which is the norm in replay mode, where
        re-executing the invalidated nodes takes milliseconds."""
        replan = self.controller.last_replan()
        if replan is None:
            self.replan_var.set("")
            return
        invalidated = ", ".join(replan.get("invalidated", []))
        self.replan_var.set(
            f"RE-PLANNED from {replan.get('changed_node')} "
            f"({replan.get('trigger')}) - invalidated and re-ran: {invalidated}"
        )

    def _show_approval_panel(self, node_id: str) -> None:
        if self._shown_for != node_id:
            package = self.controller.decision_package(node_id)
            self.package_text.delete("1.0", "end")
            self.package_text.insert("1.0", render_text(package))
            self.note_var.set("")
            self._shown_for = node_id
            self._set_decision_buttons("normal")
        self.approval_label.config(text=f"Awaiting approval: {node_id}")
        if not self.approval_frame.winfo_ismapped():
            self.approval_frame.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    def _hide_approval_panel(self) -> None:
        self._shown_for = None
        if self.approval_frame.winfo_ismapped():
            self.approval_frame.pack_forget()

    def _show_clarification_panel(self, node_id: str) -> None:
        if self._clarify_shown_for != node_id:
            questions = self.controller.clarification_questions(node_id)
            self.questions_text.delete("1.0", "end")
            self.questions_text.insert("1.0", _format_questions(questions))
            self.answer_text.delete("1.0", "end")
            self._clarify_shown_for = node_id
            self._set_decision_buttons("normal")
        self.clarify_label.config(text=f"Clarification needed: {node_id}")
        if not self.clarify_frame.winfo_ismapped():
            self.clarify_frame.pack(fill="both", expand=True, padx=10, pady=(6, 10))

    def _hide_clarification_panel(self) -> None:
        self._clarify_shown_for = None
        if self.clarify_frame.winfo_ismapped():
            self.clarify_frame.pack_forget()

    # ------------------------------------------------------------------
    # engine advancement (background thread; async work never touches Tk)
    # ------------------------------------------------------------------
    def _advance_async(self) -> None:
        if self._busy:
            return
        self._busy = True
        if not self._processing:
            self.status_var.set(f"Run {self.controller.engine.run_id} — running…")

        def worker() -> None:
            state = asyncio.run(self.controller.advance())
            self.root.after(0, self._on_advanced, state)

        threading.Thread(target=worker, daemon=True).start()

    def _on_advanced(self, _state: object) -> None:
        self._busy = False
        self._processing = False
        self._render_snapshot(self.controller.poll())

    def _tick(self) -> None:
        if not self._busy:
            self._render_snapshot(self.controller.poll())
        self.root.after(POLL_INTERVAL_MS, self._tick)

    # ------------------------------------------------------------------
    # button handlers
    # ------------------------------------------------------------------
    def _set_decision_buttons(self, state: str) -> None:
        """Approve/Reject/Submit are disabled the moment one is
        clicked and re-enabled only when a *new* checkpoint is rendered, so
        a second click cannot post a second decision for the same gate
        while the engine is still advancing."""
        for button in (self.approve_button, self.reject_button, self.submit_button):
            button.configure(state=state)

    def _begin_processing(self, apply_decision: Callable[[], None]) -> None:
        """Shows the "Processing…" state for GUI_PROCESSING_DELAY_MS, then
        records the decision and advances the run.

        The dwell deliberately happens *before* the decision is recorded,
        not after: that places it between APPROVAL_REQUESTED and
        APPROVAL_GRANTED — inside the approval-wait window, which is
        genuinely human-decision time and which
        compute_end_to_end_latency already subtracts out. Dwelling after
        the grant instead would have pushed a cosmetic 1.2s into
        end_to_end_latency_excluding_approval_seconds, i.e. into a
        reported metric.

        Nothing can be double-submitted during the dwell: the buttons are
        already disabled and the panel hidden by the caller, and the
        engine's own guard refuses a repeat decision regardless.
        """
        self._processing = True
        self.error_var.set("")
        if self._snapshot is not None:
            self.status_var.set(f"Run {self._snapshot.state.run_id} — Processing decision…")

        def _apply() -> None:
            try:
                apply_decision()
            except ApprovalNotPermitted as exc:
                self._processing = False
                self.error_var.set(str(exc))
                return
            self._advance_async()

        self.root.after(max(GUI_PROCESSING_DELAY_MS, 0), _apply)

    def _on_approve(self) -> None:
        node_id = self._shown_for
        if node_id is None:
            return
        note = self.note_var.get()
        self._set_decision_buttons("disabled")
        self._hide_approval_panel()
        self._begin_processing(lambda: self.controller.approve(node_id, note=note))

    def _on_reject(self) -> None:
        node_id = self._shown_for
        if node_id is None:
            return
        note = self.note_var.get()
        self._set_decision_buttons("disabled")
        self._hide_approval_panel()
        self._begin_processing(lambda: self.controller.reject(node_id, note=note))

    def _on_submit_clarification(self) -> None:
        node_id = self._clarify_shown_for
        if node_id is None:
            return
        answer = self.answer_text.get("1.0", "end").strip()
        if not answer:
            self.clarify_label.config(text=f"Clarification needed: {node_id} - answer cannot be empty")
            return
        self._set_decision_buttons("disabled")
        self._hide_clarification_panel()
        # the poll loop keeps running underneath, so the re-plan this answer
        # triggers is revealed step by step in the tree: the nodes
        # downstream of req.clarify flip SUCCEEDED -> INVALIDATED -> PENDING
        # -> RUNNING without the operator touching the terminal.
        self._begin_processing(lambda: self.controller.submit_clarification(node_id, answer))

    def _on_use_defaults(self) -> None:
        """Fills the box with the ambiguity agent's own proposed defaults
        rather than making the operator retype them — the GUI equivalent
        of accepting what the run already assumed."""
        questions = self.controller.clarification_questions(self._clarify_shown_for or "")
        defaults = "; ".join(
            str(q.get("proposed_default", "")).rstrip(".") for q in questions if q.get("proposed_default")
        )
        self.answer_text.delete("1.0", "end")
        self.answer_text.insert("1.0", defaults)


def launch_watch_window(engine: Engine) -> None:
    """Opens the watch GUI against an already-constructed, already-started
    Engine — the shared core both entry points below use. `agentic run
    --watch` calls this directly with the Engine it just built (no need
    to round-trip it through disk and Engine.resume() first); `agentic
    watch <run_id>` reconstructs one via load_engine() and calls this."""
    controller = WatchController(engine)
    root = tk.Tk()
    WatchWindow(root, controller)
    root.mainloop()


def run_watch_gui(run_id: str, mode: str = "replay", settings: Settings | None = None) -> None:
    """`agentic watch <run_id>`: attach to a run already in progress
    (or paused at an approval checkpoint) by reconstructing its Engine
    from disk."""
    engine = load_engine(run_id, mode=mode, settings=settings)
    launch_watch_window(engine)
