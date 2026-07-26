"""Query execution endpoints.

Two modes, as the spec calls for.

**Normal.** ``POST /query`` runs the statement inside the request and returns the
rows plus the plan with its actual statistics.

**Step.** ``POST /query/step`` starts the query on its own thread, runs it to the
first checkpoint, and returns an execution id. The client then drives it with
``/next``, ``/continue``, ``/until`` and ``/cancel``. No sleeps are involved: the
engine thread genuinely blocks on a condition variable until told to continue.

A stepped execution holds its database's lock for as long as it is alive, which
is inherent — it is a query suspended mid-operation. Two things keep that from
becoming a hang: every step call has a timeout, and idle executions are reaped.
``/cancel`` deliberately needs no lock, so it always works.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from engine.errors import ChenDBError, SqlError
from engine.executor.controller import ResumeMode
from engine.executor.engine import execute_script
from engine.server import mappers
from engine.server.deps import DatabaseDep, http_status_for
from engine.server.executions import Execution, ExecutionNotFound, ExecutionStore
from engine.server.schemas.query import (
    ExecutionDetail,
    ExecutionListResponse,
    QueryRequest,
    QueryResultModel,
    ResumeRequest,
    StepRequest,
)

router = APIRouter(prefix="/databases/{database_id}", tags=["query"])
executions_router = APIRouter(prefix="/executions", tags=["query"])


def _store(request: Request) -> ExecutionStore:
    return request.app.state.executions


def _fail(exc: Exception) -> HTTPException:
    """Translate an engine error, keeping a SQL error's source position."""
    detail: dict[str, object] = {"error": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, SqlError):
        detail["sql_error"] = mappers.sql_error_to_api(exc).model_dump()
    return HTTPException(status_code=http_status_for(exc), detail=detail)


# -- normal mode -----------------------------------------------------------


@router.post(
    "/query",
    response_model=list[QueryResultModel],
    summary="Run SQL and return the results",
)
def run_query(
    payload: QueryRequest, managed: DatabaseDep, request: Request
) -> list[QueryResultModel]:
    """Execute every statement in ``sql``, returning one result each.

    A list rather than a single object because a script is a normal thing to
    send: ``CREATE TABLE …; INSERT …; SELECT …`` is one request and three results.

    Unlike ``/parse``, a failure here *is* a failed request — 422 with the source
    position attached — because there is no useful partial answer to return.
    """
    max_rows = payload.max_rows or request.app.state.config.max_rows_per_query
    try:
        with managed.use() as db:
            results = execute_script(
                payload.sql, db, tracer=managed.tracer, max_rows=max_rows
            )
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return [mappers.query_result_to_api(result) for result in results]


# -- step mode -------------------------------------------------------------


@router.post(
    "/query/step",
    response_model=ExecutionDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Start a stepped execution, paused at its first checkpoint",
)
def start_stepped_query(
    payload: StepRequest, managed: DatabaseDep, request: Request
) -> ExecutionDetail:
    try:
        execution = _store(request).start(managed, payload.sql)
    except ChenDBError as exc:
        raise _fail(exc) from exc
    return mappers.execution_detail_to_api(execution)


@executions_router.get(
    "", response_model=ExecutionListResponse, summary="List stepped executions"
)
def list_executions(
    request: Request,
    database_id: Annotated[str | None, Query()] = None,
) -> ExecutionListResponse:
    store = _store(request)
    return ExecutionListResponse(
        executions=[
            mappers.execution_summary_to_api(execution)
            for execution in store.list(database_id)
        ],
        max_executions=request.app.state.config.max_executions,
    )


@executions_router.get(
    "/{execution_id}",
    response_model=ExecutionDetail,
    summary="Current state of an execution, without advancing it",
)
def get_execution(execution_id: str, request: Request) -> ExecutionDetail:
    return mappers.execution_detail_to_api(_lookup(request, execution_id))


@executions_router.post(
    "/{execution_id}/next",
    response_model=ExecutionDetail,
    summary="Advance to the next checkpoint",
)
def step_execution(execution_id: str, request: Request) -> ExecutionDetail:
    return mappers.execution_detail_to_api(
        _resume(request, execution_id, ResumeMode.STEP)
    )


@executions_router.post(
    "/{execution_id}/continue",
    response_model=ExecutionDetail,
    summary="Run to completion without pausing",
)
def continue_execution(execution_id: str, request: Request) -> ExecutionDetail:
    return mappers.execution_detail_to_api(
        _resume(request, execution_id, ResumeMode.CONTINUE)
    )


@executions_router.post(
    "/{execution_id}/resume",
    response_model=ExecutionDetail,
    summary="Resume with an explicit mode: step, continue, until_row, until_page_read, until_operator",
)
def resume_execution(
    execution_id: str, payload: ResumeRequest, request: Request
) -> ExecutionDetail:
    mode = ResumeMode(payload.mode)
    if mode is ResumeMode.UNTIL_OPERATOR and not payload.operator_id:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "MissingOperatorId",
                "message": "until_operator needs an operator_id to stop at",
            },
        )
    execution = _resume(request, execution_id, mode, payload.operator_id)
    return mappers.execution_detail_to_api(execution)


@executions_router.post(
    "/{execution_id}/cancel",
    response_model=ExecutionDetail,
    summary="Cancel an execution and wait for it to release its lock",
)
def cancel_execution(execution_id: str, request: Request) -> ExecutionDetail:
    try:
        execution = _store(request).cancel(execution_id)
    except ExecutionNotFound as exc:
        raise _fail(exc) from exc
    return mappers.execution_detail_to_api(execution)


def _lookup(request: Request, execution_id: str) -> Execution:
    try:
        return _store(request).get(execution_id)
    except ExecutionNotFound as exc:
        raise _fail(exc) from exc


def _resume(
    request: Request,
    execution_id: str,
    mode: ResumeMode,
    operator_id: str | None = None,
) -> Execution:
    try:
        return _store(request).resume(execution_id, mode, operator_id=operator_id)
    except ExecutionNotFound as exc:
        raise _fail(exc) from exc
