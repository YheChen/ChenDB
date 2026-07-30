"""Query execution API models.

Like the AST, the operator tree crosses the wire **flat** (a node list plus a
root id) for the same reasons: recursive Pydantic makes awkward OpenAPI, and the
visualizer needs random access by ``operator_id`` to highlight whichever operator
is currently active.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from engine.server.schemas.common import ApiModel, RequestModel
from engine.server.schemas.database import RecordIdModel

__all__ = [
    "ExecutionDetail",
    "ExecutionListResponse",
    "ExecutionSummary",
    "OperatorNodeModel",
    "PlanAlternativeModel",
    "PlanModel",
    "PlanStatisticsModel",
    "QueryRequest",
    "QueryResultModel",
    "ResultColumnModel",
    "ResumeRequest",
    "StepRequest",
]

ResumeModeName = Literal[
    "step", "continue", "until_row", "until_page_read", "until_operator"
]

ExecutionStateName = Literal[
    "pending", "running", "paused", "finished", "cancelled", "failed"
]


class ResultColumnModel(ApiModel):
    name: str
    type: str | None = Field(
        description="SQL type, or null when an expression's type is not statically known"
    )


class OperatorNodeModel(ApiModel):
    """One node of the physical plan: what it will cost, and what it did.

    Estimated and actual sit side by side because the gap between them is the
    single most useful thing a plan view can show. A plan that is slow is almost
    always a plan whose row estimate was wrong, and no amount of staring at the
    chosen operators reveals that, only the comparison does.
    """

    operator_id: str
    operator_type: str = Field(description="SeqScan, IndexScan, Filter or Project")
    detail: str = Field(description="Predicate, table, index condition or projection")
    children: list[str] = Field(description="operator_ids of inputs, left to right")
    output_columns: list[ResultColumnModel]

    estimated_rows: float | None = Field(
        description="Rows the planner expected out; null if the plan was not costed"
    )
    estimated_cost: float | None = Field(
        description="Cumulative estimated cost of this node and everything below it"
    )
    estimated_io_cost: float | None = Field(
        description="The I/O half of this node's own cost"
    )
    estimated_cpu_cost: float | None = Field(
        description="The CPU half of this node's own cost"
    )

    next_calls: int = Field(description="Times this operator was asked for a row")
    input_rows: int = Field(description="Rows it consumed from its children")
    output_rows: int = Field(description="Rows it produced")
    rows_rejected: int = Field(
        description="Filter only: rows whose predicate was not TRUE; 0 elsewhere"
    )
    duration_ns: int = Field(description="Time spent inside this operator's own work")

    @property
    def estimate_ratio(self) -> float | None:
        """Actual over estimated. 1.0 is perfect; 100 is a planner disaster."""
        if not self.estimated_rows:
            return None
        return self.output_rows / self.estimated_rows


class PlanAlternativeModel(ApiModel):
    """One option the planner considered, chosen or not."""

    description: str
    access_path: str = Field(description="The node type, e.g. PhysicalIndexScan")
    estimated_cost: float
    estimated_rows: float
    chosen: bool
    rejected_because: str = Field(
        description="Why this lost, e.g. '3.6x the cost of the chosen plan'. "
        "Empty for the winner."
    )
    index_name: str | None
    decision: str = Field(
        default="access path",
        description=(
            "Which question this answered: 'how to read users', 'what order to "
            "join in'. A query over several tables makes several independent "
            "decisions, and a flat list of winners reads as a contradiction."
        ),
    )


class PlanStatisticsModel(ApiModel):
    """The statistics the estimates were computed from."""

    table_name: str
    row_count: int
    page_count: int
    stale: bool = Field(
        description="True when the table was written to after it was last "
        "analyzed, so every estimate above is based on old numbers"
    )
    gathered_at_ns: int


class PlanModel(ApiModel):
    nodes: list[OperatorNodeModel]
    root_id: str
    alternatives: list[PlanAlternativeModel] = Field(
        description="Every access path considered, with the cost of each"
    )
    rewrites: list[str] = Field(
        description="Rewrite rules that changed the plan, in the order they ran"
    )
    estimated_cost: float | None = Field(description="Total cost of the chosen plan")
    statistics: PlanStatisticsModel | None


class QueryResultModel(ApiModel):
    """The outcome of one statement."""

    statement_kind: str = Field(
        description=(
            "The AST node type: SelectStatement, InsertStatement, "
            "UpdateStatement, DeleteStatement, CreateTableStatement, "
            "CreateIndexStatement, ExplainStatement, AnalyzeStatement, or one "
            "of the transaction statements"
        )
    )
    returns_rows: bool
    message: str = Field(description="Summary for statements that return no rows")

    columns: list[ResultColumnModel]
    rows: list[list[Any]]
    record_ids: list[RecordIdModel] = Field(
        description="Where each row lives; empty when the projection computes values"
    )
    plan: PlanModel | None = Field(description="Null for statements with no operator tree")

    rows_returned: int
    rows_affected: int = Field(description="Rows written, for INSERT")
    rows_scanned: int = Field(description="Rows the scan produced before filtering")
    rows_rejected: int = Field(description="Rows a filter dropped")
    pages_read: int
    pages_written: int
    duration_ns: int
    truncated: bool = Field(description="True when the row ceiling cut the result short")
    cancelled: bool


class QueryRequest(RequestModel):
    sql: str = Field(
        max_length=100_000, description="One or more statements, separated by semicolons"
    )
    max_rows: int | None = Field(
        default=None, ge=1, le=100_000, description="Override the row ceiling"
    )


class StepRequest(RequestModel):
    sql: str = Field(
        max_length=100_000,
        description="Exactly one statement. Stepping a script is refused.",
    )


class ResumeRequest(RequestModel):
    mode: ResumeModeName = "step"
    operator_id: str | None = Field(
        default=None, description="Required by until_operator; ignored otherwise"
    )


class ExecutionSummary(ApiModel):
    execution_id: str
    database_id: str
    statement_kind: str
    state: ExecutionStateName
    steps_taken: int
    age_seconds: float
    idle_seconds: float


class ExecutionDetail(ApiModel):
    """A stepped execution's current state, including where it is paused."""

    execution_id: str
    database_id: str
    sql: str
    statement_kind: str
    state: ExecutionStateName
    steps_taken: int

    pause_kind: str | None = Field(
        description="operator_open, operator_next, row_emitted, operator_close or page_read"
    )
    pause_operator_id: str | None = Field(description="Which operator is at the checkpoint")
    pause_detail: str = Field(description="The row or page involved, rendered for display")

    plan: PlanModel | None = Field(
        description="Available once the operators have been built"
    )
    current_row: list[Any] | None = Field(
        description="The row at the checkpoint, when paused on one"
    )
    rows_so_far: int = Field(description="Rows emitted from the root up to this point")
    result: QueryResultModel | None = Field(
        description="Null until the execution has finished"
    )
    error: str
    age_seconds: float
    idle_seconds: float


class ExecutionListResponse(ApiModel):
    executions: list[ExecutionSummary]
    max_executions: int
