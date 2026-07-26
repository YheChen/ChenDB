#!/usr/bin/env python3
"""A narrated tour of the Milestone 3 execution engine.

    python examples/milestone3_execution.py

Shows the volcano iterator model, three-valued logic, and step-through
execution with real cancellation.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import Database
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.executor import (
    ExecutionState,
    ResumeMode,
    StepController,
    describe_plan,
    execute_script,
)

SETUP = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, age INTEGER);
INSERT INTO users VALUES
  (1, 'ada@example.com',   36),
  (2, 'alan@example.com',  NULL),
  (3, 'grace@example.com', 45),
  (4, 'edgar@example.com', 17);
"""


def heading(number: int, text: str) -> None:
    print(f"\n\033[1m{number}. {text}\033[0m")
    print("─" * 78)


def main() -> int:
    with tempfile.TemporaryDirectory() as workdir:
        path = Path(workdir) / "demo.chendb"
        sink = RingBufferSink()
        tracer = Tracer(sink, TraceLevel.OPERATOR)

        with Database.open(path, page_size=256, tracer=tracer) as db:
            # ----------------------------------------------------------
            heading(1, "Run a script")
            for result in execute_script(SETUP, db, tracer=tracer):
                print(f"   {result.statement_kind:<22} {result.message}")

            # ----------------------------------------------------------
            heading(2, "The operator tree, with what it actually did")
            result = execute_script(
                "SELECT email, age * 2 AS doubled FROM users WHERE age >= 18",
                db,
                tracer=tracer,
            )[0]
            print(describe_plan(result.plan))
            print()
            for operator in _walk(result.plan):
                stats = operator.stats
                print(
                    f"   {operator.operator_id:<11} {operator.operator_type:<8} "
                    f"in={stats.input_rows} out={stats.output_rows} "
                    f"calls={stats.next_calls}"
                )
            print(f"\n   {[c.name for c in result.columns]}")
            for row in result.rows:
                print(f"   {row}")
            print(f"\n   scanned {result.stats.rows_scanned}, "
                  f"rejected {result.stats.rows_rejected}, "
                  f"returned {result.stats.rows_returned}, "
                  f"{result.stats.pages_read} page read(s)")

            # ----------------------------------------------------------
            heading(3, "Three-valued logic: alan's NULL age")
            for sql, note in [
                ("SELECT id FROM users WHERE age >= 18", "NULL is not >= 18 …"),
                ("SELECT id FROM users WHERE age < 18", "… and not < 18 either"),
                ("SELECT id FROM users WHERE age IS NULL", "only IS NULL finds it"),
                ("SELECT id FROM users WHERE age = age", "even x = x is unknown"),
                ("SELECT id FROM users", "every row"),
            ]:
                ids = [row[0] for row in execute_script(sql, db)[0].rows]
                print(f"   {sql:<44} → {ids!s:<14} {note}")
            print("\n   A row passes WHERE only when the predicate is exactly TRUE.")
            print("   NULL is unknown, and unknown does not pass.")

            # ----------------------------------------------------------
            heading(4, "SELECT * needs no projection at all")
            plan = execute_script("SELECT * FROM users", db)[0].plan
            print(f"   SELECT *          → {describe_plan(plan)}")
            plan = execute_script("SELECT age, id, email FROM users", db)[0].plan
            print(f"   reordered columns → {describe_plan(plan).splitlines()[0]}")

            # ----------------------------------------------------------
            heading(5, "Step through a query, one operation at a time")
            controller = StepController(stepping=True)
            captured: list[object] = []

            def run() -> None:
                captured.append(
                    execute_script(
                        "SELECT email FROM users WHERE age >= 18",
                        db,
                        controller=controller,
                    )[0]
                )
                controller.mark_finished(ExecutionState.FINISHED)

            thread = threading.Thread(target=run, daemon=True)
            thread.start()

            for index in range(16):
                if controller.wait_for_pause_or_end(timeout=5).is_terminal:
                    break
                reason = controller.pause_reason
                assert reason is not None
                marker = ""
                if reason.kind.value == "operator_next":
                    marker = "  ← next() travels DOWN"
                elif reason.kind.value == "row_emitted":
                    marker = "  ← rows travel UP"
                print(
                    f"   {index:>2}. {reason.kind.value:<14} "
                    f"{reason.operator_id:<11} {reason.detail}{marker}"
                )
                controller.resume(ResumeMode.STEP)
            controller.resume(ResumeMode.CONTINUE)
            thread.join(timeout=5)
            print("\n   Watch for a scan row_emitted followed by another scan")
            print("   operator_next with no filter emit between them: that is the")
            print("   filter dropping alan, whose NULL age makes the predicate unknown.")

            # ----------------------------------------------------------
            heading(6, "Cancellation unwinds the tree")
            controller = StepController(stepping=True)
            out: list[object] = []

            def run_again() -> None:
                out.append(
                    execute_script("SELECT email FROM users", db, controller=controller)[0]
                )
                controller.mark_finished(ExecutionState.FINISHED)

            thread = threading.Thread(target=run_again, daemon=True)
            thread.start()
            controller.wait_for_pause_or_end(timeout=5)
            controller.resume(ResumeMode.UNTIL_ROW)
            controller.wait_for_pause_or_end(timeout=5)
            print(f"   paused at: {controller.pause_reason}")
            controller.cancel()
            thread.join(timeout=5)
            print(f"   thread finished: {not thread.is_alive()}")
            print(f"   result.cancelled: {out[0].cancelled}")  # type: ignore[attr-defined]
            print("   The exception was raised inside the engine thread at the next")
            print("   checkpoint, so every operator unwound through its own close().")

            # ----------------------------------------------------------
            heading(7, "What the executor reported")
            counts: dict[str, int] = {}
            for item in sink.snapshot():
                counts[item.event_type] = counts.get(item.event_type, 0) + 1
            for event_type, count in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"   {event_type:<24} {count:>5}")

    print("\n" + "─" * 78)
    print("Try it in the browser: python -m engine.server, then the Execution tab.")
    return 0


def _walk(operator):
    out = [operator]
    for child in operator.children:
        out.extend(_walk(child))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
