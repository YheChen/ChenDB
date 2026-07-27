"""Volcano operators and the step controller."""

from __future__ import annotations

import threading

import pytest

from engine.database import Database
from engine.diagnostics import RingBufferSink, TraceLevel, Tracer
from engine.errors import QueryCancelledError
from engine.executor import (
    ExecutionContext,
    ExecutionState,
    Filter,
    Project,
    ResumeMode,
    SeqScan,
    StepController,
    StepKind,
    describe_plan,
    execute_script,
)
from engine.executor.binder import bind_expression, bind_select
from engine.parser.parser import parse_statement
from engine.serialization.schema import Column, Schema
from engine.serialization.types import DataType

PAGE_SIZE = 256

SCHEMA = Schema.of(
    Column("id", DataType.INTEGER, nullable=False),
    Column("name", DataType.TEXT, nullable=False),
    Column("age", DataType.INTEGER),
)

ROWS: list[tuple[object, ...]] = [
    (1, "ada", 36),
    (2, "alan", None),
    (3, "grace", 45),
    (4, "edgar", 17),
]


@pytest.fixture
def db(db_path):
    with Database.open(db_path, page_size=PAGE_SIZE) as instance:
        instance.create_table("users", SCHEMA)
        instance.insert_many("users", ROWS)
        yield instance


def scan_of(db: Database, context: ExecutionContext | None = None) -> SeqScan:
    return SeqScan(
        "scan_1",
        context or ExecutionContext(),
        heap=db.heap_for("users"),
        schema=SCHEMA,
        table_name="users",
    )


def predicate(sql: str):
    statement = parse_statement(f"SELECT * FROM t WHERE {sql}")
    return bind_expression(statement.where, SCHEMA)


# -- the iterator protocol -------------------------------------------------


def test_a_scan_yields_every_row_then_none_forever(db: Database):
    scan = scan_of(db)
    scan.open()
    rows = []
    while (row := scan.next()) is not None:
        rows.append(row)
    assert rows == [tuple(row) for row in ROWS]
    # None must be sticky: an exhausted operator stays exhausted.
    assert scan.next() is None
    assert scan.next() is None
    scan.close()


def test_next_before_open_is_a_programming_error(db: Database):
    with pytest.raises(RuntimeError, match="before open"):
        scan_of(db).next()


def test_close_is_idempotent_and_open_is_too(db: Database):
    scan = scan_of(db)
    scan.open()
    scan.open()
    scan.close()
    scan.close()


def test_iterating_opens_and_closes_automatically(db: Database):
    scan = scan_of(db)
    assert len(list(scan)) == len(ROWS)


def test_a_scan_is_lazy(db: Database):
    """One next() must not read the whole table.

    This is the property the whole model exists for: `LIMIT 1` over a huge table
    should cost one page, not all of them.
    """
    scan = scan_of(db)
    reads_before = db.stats.page_reads
    scan.open()
    scan.next()
    assert db.stats.page_reads - reads_before == 1
    scan.close()


def test_a_scan_reports_where_each_row_came_from(db: Database):
    scan = scan_of(db)
    scan.open()
    scan.next()
    first = scan.last_record_id
    scan.next()
    assert first is not None
    assert scan.last_record_id != first
    scan.close()


# -- filter ----------------------------------------------------------------


def test_filter_passes_only_rows_whose_predicate_is_true(db: Database):
    context = ExecutionContext()
    plan = Filter(
        "f", context, child=scan_of(db, context), predicate=predicate("age >= 18")
    )
    assert [row[0] for row in plan] == [1, 3]


def test_filter_drops_a_null_predicate_exactly_like_a_false_one(db: Database):
    # Row 2 has a NULL age, so `age >= 18` is unknown for it. Unknown does not
    # pass, which is required SQL behaviour and the most common source of
    # surprise for people reading query results.
    context = ExecutionContext()
    plan = Filter(
        "f", context, child=scan_of(db, context), predicate=predicate("age >= 18")
    )
    ids = [row[0] for row in plan]
    assert 2 not in ids
    assert plan.rows_rejected == 2  # the NULL row and edgar, who is 17


def test_filter_counts_what_it_rejected(db: Database):
    context = ExecutionContext()
    plan = Filter(
        "f", context, child=scan_of(db, context), predicate=predicate("age > 100")
    )
    assert list(plan) == []
    assert plan.stats.input_rows == len(ROWS)
    assert plan.rows_rejected == len(ROWS)
    assert plan.stats.output_rows == 0


def test_one_next_on_a_filter_can_cost_many_on_its_child(db: Database):
    context = ExecutionContext()
    scan = scan_of(db, context)
    plan = Filter("f", context, child=scan, predicate=predicate("age = 45"))
    plan.open()
    plan.next()  # must walk past rows 1 and 2 to reach row 3
    assert scan.stats.next_calls == 3
    assert plan.stats.next_calls == 1
    plan.close()


# -- projection ------------------------------------------------------------


def test_projection_narrows_and_computes(db: Database):
    context = ExecutionContext()
    statement = parse_statement("SELECT name, age * 2 AS doubled FROM users")
    bound = bind_select(statement, db.catalog)
    plan = Project(
        "p",
        context,
        child=scan_of(db, context),
        projections=bound.projections,
        output_columns=bound.output_columns,
    )
    rows = list(plan)
    assert rows[0] == ("ada", 72)
    assert rows[1] == ("alan", None), "NULL * 2 is NULL, not 0"
    assert [column.name for column in plan.output_columns] == ["name", "doubled"]


def test_projection_can_reorder_and_repeat_columns(db: Database):
    context = ExecutionContext()
    statement = parse_statement("SELECT age, id, id FROM users")
    bound = bind_select(statement, db.catalog)
    plan = Project(
        "p",
        context,
        child=scan_of(db, context),
        projections=bound.projections,
        output_columns=bound.output_columns,
    )
    assert next(iter(plan)) == (36, 1, 1)


# -- the tree --------------------------------------------------------------


def test_the_plan_tree_reads_top_down(db: Database):
    result = execute_script("SELECT name FROM users WHERE age >= 18", db)[0]
    rendered = describe_plan(result.plan)
    assert rendered.splitlines()[0].startswith("Project")
    assert "Filter" in rendered
    assert rendered.strip().endswith("table=users")


def test_an_identity_projection_is_removed_from_the_plan(db: Database):
    # SELECT * over every column in order needs no projection at all: removing
    # it saves a method call and a tuple build per row.
    result = execute_script("SELECT * FROM users", db)[0]
    assert result.plan.operator_type == "SeqScan"
    assert result.rows[0] == (1, "ada", 36)


def test_reordering_makes_the_projection_necessary_again(db: Database):
    result = execute_script("SELECT age, id, name FROM users", db)[0]
    assert result.plan.operator_type == "Project"


def test_closing_the_root_closes_the_whole_tree(db: Database):
    context = ExecutionContext()
    scan = scan_of(db, context)
    plan = Filter("f", context, child=scan, predicate=predicate("id > 0"))
    plan.open()
    plan.next()
    plan.close()
    # The scan's generator must be released, or the file handle leaks.
    assert scan.last_record_id is None


# -- diagnostics -----------------------------------------------------------


def test_operator_events_show_next_going_down_and_rows_coming_up(db: Database):
    sink = RingBufferSink()
    tracer = Tracer(sink, TraceLevel.OPERATOR)
    execute_script("SELECT name FROM users WHERE age >= 18", db, tracer=tracer)

    events = [item.event for item in sink.snapshot() if item.event_type == "OperatorEvent"]
    opens = [e.operator_id for e in events if e.action == "opened"]
    # open() recurses into children first, so the leaf opens first.
    assert opens == ["scan_1", "filter_1", "project_1"]

    # A next() at the root drives a next() down each level before any row exists.
    sequence = [(e.operator_id, e.action) for e in events]
    first_next = sequence.index(("project_1", "next"))
    assert sequence[first_next : first_next + 3] == [
        ("project_1", "next"),
        ("filter_1", "next"),
        ("scan_1", "next"),
    ]
    # Then the row travels back up.
    first_emit = sequence.index(("scan_1", "row_emitted"))
    assert sequence[first_emit : first_emit + 3] == [
        ("scan_1", "row_emitted"),
        ("filter_1", "row_emitted"),
        ("project_1", "row_emitted"),
    ]


def test_expression_events_only_appear_at_verbose(db: Database):
    for level, expected in ((TraceLevel.OPERATOR, 0), (TraceLevel.VERBOSE, 1)):
        sink = RingBufferSink()
        execute_script(
            "SELECT id FROM users WHERE age >= 18", db, tracer=Tracer(sink, level)
        )
        count = sum(
            1 for item in sink.snapshot() if item.event_type == "ExpressionEvalEvent"
        )
        assert (count > 0) == bool(expected)


# -- the step controller ---------------------------------------------------


class SteppedQuery:
    """Runs a query on a thread so the test can drive its controller."""

    def __init__(self, db: Database, sql: str) -> None:
        self.controller = StepController(stepping=True)
        self.result = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, args=(db, sql), daemon=True)

    def _run(self, db: Database, sql: str) -> None:
        try:
            self.result = execute_script(sql, db, controller=self.controller)[0]
            self.controller.mark_finished(ExecutionState.FINISHED)
        except BaseException as exc:
            self.error = exc
            self.controller.mark_finished(ExecutionState.FAILED)

    def __enter__(self) -> SteppedQuery:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.controller.cancel()
        self.controller.resume(ResumeMode.CONTINUE)
        self._thread.join(timeout=5)
        assert not self._thread.is_alive(), "the engine thread did not finish"

    def pauses(self, limit: int = 40) -> list[str]:
        """Step repeatedly, collecting each pause reason until the query ends."""
        seen: list[str] = []
        for _ in range(limit):
            state = self.controller.wait_for_pause_or_end(timeout=5)
            if state.is_terminal:
                break
            reason = self.controller.pause_reason
            assert reason is not None
            seen.append(f"{reason.kind.value}:{reason.operator_id}")
            self.controller.resume(ResumeMode.STEP)
        return seen


def test_stepping_pauses_at_every_checkpoint(db: Database):
    with SteppedQuery(db, "SELECT name FROM users WHERE age >= 18") as query:
        pauses = query.pauses()

    assert pauses[:3] == [
        "operator_open:scan_1",
        "operator_open:filter_1",
        "operator_open:project_1",
    ]
    # The row emitted by the scan for the NULL-age row never reaches the filter's
    # output, which is visible as a missing filter emit between two scan calls.
    assert "row_emitted:scan_1" in pauses
    assert "row_emitted:project_1" in pauses


def test_a_stepped_query_still_returns_the_right_rows(db: Database):
    with SteppedQuery(db, "SELECT id FROM users WHERE age >= 18") as query:
        query.pauses()
        query.controller.resume(ResumeMode.CONTINUE)
        query.controller.wait_for_pause_or_end(timeout=5)
    assert query.error is None
    assert [row[0] for row in query.result.rows] == [1, 3]


def test_run_until_row_skips_the_intermediate_checkpoints(db: Database):
    with SteppedQuery(db, "SELECT id FROM users") as query:
        query.controller.wait_for_pause_or_end(timeout=5)
        query.controller.resume(ResumeMode.UNTIL_ROW)
        assert query.controller.wait_for_pause_or_end(timeout=5) is ExecutionState.PAUSED
        reason = query.controller.pause_reason
        assert reason is not None
        assert reason.kind is StepKind.ROW_EMITTED


def test_continue_runs_to_completion_without_pausing(db: Database):
    with SteppedQuery(db, "SELECT id FROM users") as query:
        query.controller.wait_for_pause_or_end(timeout=5)
        steps_before = query.controller.steps_taken
        query.controller.resume(ResumeMode.CONTINUE)
        assert query.controller.wait_for_pause_or_end(timeout=5) is ExecutionState.FINISHED
        assert query.controller.steps_taken == steps_before


def test_cancelling_unwinds_the_operator_tree(db: Database):
    query = SteppedQuery(db, "SELECT id FROM users")
    with query:
        query.controller.wait_for_pause_or_end(timeout=5)
        query.controller.cancel()
    # The result is a partial answer, not an exception: the client asked to stop.
    assert query.error is None
    assert query.result is not None
    assert query.result.cancelled is True


def test_cancelling_a_paused_query_wakes_it(db: Database):
    """The important failure mode: a paused query must not hang forever."""
    query = SteppedQuery(db, "SELECT id FROM users")
    with query:
        assert query.controller.wait_for_pause_or_end(timeout=5) is ExecutionState.PAUSED
        query.controller.cancel()
        # __exit__ joins with a timeout and asserts the thread finished.
    assert query.controller.cancelled


def test_resuming_after_the_end_is_harmless(db: Database):
    with SteppedQuery(db, "SELECT id FROM users") as query:
        query.controller.resume(ResumeMode.CONTINUE)
        query.controller.wait_for_pause_or_end(timeout=5)
        query.controller.resume(ResumeMode.STEP)  # must not revive it
    assert query.controller.state.is_terminal


def test_a_non_stepping_controller_never_pauses(db: Database):
    controller = StepController(stepping=False)
    result = execute_script("SELECT id FROM users", db, controller=controller)[0]
    assert len(result.rows) == len(ROWS)
    assert controller.steps_taken == 0


def test_the_null_controller_costs_nothing(db: Database):
    from engine.executor.controller import NULL_CONTROLLER

    # It must be safe to call unconditionally from every operator, and must not
    # be cancellable — a shared singleton that could be cancelled would poison
    # every other query.
    NULL_CONTROLLER.checkpoint(StepKind.ROW_EMITTED, operator_id="x")
    NULL_CONTROLLER.cancel()
    assert execute_script("SELECT id FROM users", db)[0].cancelled is False


def test_checkpoint_raises_once_cancelled(db: Database):
    controller = StepController(stepping=True)
    controller.cancel()
    with pytest.raises(QueryCancelledError):
        controller.checkpoint(StepKind.OPERATOR_NEXT, operator_id="x")
