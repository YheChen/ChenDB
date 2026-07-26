"""Query execution: the volcano iterator model.

    binder.py       resolve names against a schema; ColumnRef -> BoundColumnRef
    expression.py   evaluate an expression against a row, with 3-valued logic
    operators.py    SeqScan, Filter, Project — open()/next()/close()
    controller.py   pause, step and cancel a running query
    engine.py       statement -> plan -> QueryResult

    from engine import Database
    from engine.executor import execute_script

    with Database.open("demo.chendb") as db:
        for result in execute_script("SELECT * FROM users WHERE age >= 18", db):
            print(result.rows)

Milestone 3 has one access path (a full scan) and no cost model, so planning is
a rule-based translation. Milestone 5 adds indexes and Milestone 6 the optimiser
that chooses between them.
"""

from engine.executor.binder import (
    BoundColumnRef,
    BoundInsert,
    BoundSelect,
    BoundStatement,
    ResultColumn,
    bind_create_table,
    bind_expression,
    bind_insert,
    bind_select,
)
from engine.executor.controller import (
    NULL_CONTROLLER,
    ExecutionState,
    PauseReason,
    ResumeMode,
    StepController,
    StepKind,
)
from engine.executor.engine import (
    DEFAULT_MAX_ROWS,
    ExecutionStats,
    QueryResult,
    build_select_plan,
    execute_script,
    execute_statement,
)
from engine.executor.expression import describe_expression, evaluate, is_true
from engine.executor.operators import (
    ExecutionContext,
    Filter,
    Operator,
    OperatorStats,
    Project,
    SeqScan,
    describe_plan,
)

__all__ = [
    "DEFAULT_MAX_ROWS",
    "NULL_CONTROLLER",
    "BoundColumnRef",
    "BoundInsert",
    "BoundSelect",
    "BoundStatement",
    "ExecutionContext",
    "ExecutionState",
    "ExecutionStats",
    "Filter",
    "Operator",
    "OperatorStats",
    "PauseReason",
    "Project",
    "QueryResult",
    "ResultColumn",
    "ResumeMode",
    "SeqScan",
    "StepController",
    "StepKind",
    "bind_create_table",
    "bind_expression",
    "bind_insert",
    "bind_select",
    "build_select_plan",
    "describe_expression",
    "describe_plan",
    "evaluate",
    "execute_script",
    "execute_statement",
    "is_true",
]
