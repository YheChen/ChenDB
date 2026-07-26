"""SQL front-end endpoints.

Milestone 2 exposes **parsing only**.  There is no ``POST /query``, because
there is no executor: a query endpoint that returned an AST instead of rows
would be worse than no query endpoint.  Execution arrives in Milestone 3.

Why this is nested under a database when parsing needs no database: it is
purely syntactic today, but Milestone 4 adds *binding* — resolving table and
column names against the catalog — which does. Nesting now avoids moving the
endpoint later and breaking every client.
"""

from __future__ import annotations

from fastapi import APIRouter

from engine.parser.analyze import analyze_sql
from engine.server import mappers
from engine.server.deps import DatabaseDep
from engine.server.schemas.sql import ParseRequest, ParseResponse

router = APIRouter(prefix="/databases/{database_id}", tags=["sql"])


@router.post(
    "/parse",
    response_model=ParseResponse,
    summary="Tokenize and parse SQL, returning tokens, AST and any error",
)
def parse_sql(payload: ParseRequest, managed: DatabaseDep) -> ParseResponse:
    """Parse ``sql`` without executing it.

    Always 200, even for invalid SQL: a syntax error is a *result* here, not a
    failed request. The editor needs the tokens that did scan and the position
    of the failure, and an HTTP error status would throw both away.

    The database's tracer is used, so parser events appear in the same timeline
    as storage events. Parsing touches no pages, so the engine lock is not held.
    """
    outcome = analyze_sql(payload.sql, tracer=managed.tracer)
    return mappers.parse_outcome_to_api(outcome)
