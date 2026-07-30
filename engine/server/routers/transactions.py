"""Transaction endpoints.

    GET  /databases/{db}/transactions            the timeline and the undo log
    POST /databases/{db}/transactions            BEGIN
    POST /databases/{db}/transactions/commit     COMMIT
    POST /databases/{db}/transactions/rollback   ROLLBACK

The three verbs exist as endpoints as well as SQL because the explorer's
transaction panel has buttons, and a button that secretly submits ``BEGIN;``
through the SQL endpoint would make the query history lie about what the user
ran. They are the same three manager calls either way.

Why POST and not PUT
--------------------
``BEGIN`` is not idempotent: sending it twice is an error, not a no-op, because
ChenDB has no savepoints and will not silently pretend to nest. POST is the
honest verb for "do this once".

Statelessness, and why that is fine here
----------------------------------------
HTTP has no session, so an explicit transaction opened by one request stays open
across requests until some later request ends it — the state lives on the
database handle, not the connection. That is genuinely a footgun for a
multi-client server, and the reason it is acceptable here is the reason the
whole workspace design is acceptable: this API serves *one* explorer looking at
*its own* database file. The active transaction is reported on every GET so the
UI can show a persistent "transaction open" banner rather than letting one be
forgotten.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from engine.server import mappers
from engine.server.deps import DatabaseDep
from engine.server.routers.query import SESSION_PATTERN
from engine.server.schemas.transactions import (
    TransactionListResponse,
    TransactionResultResponse,
)
from engine.transaction.manager import DEFAULT_SESSION

router = APIRouter(prefix="/databases/{database_id}", tags=["transactions"])

#: Which console a request belongs to. See ``engine/server/routers/query.py``.
SessionParam = Annotated[str, Query(pattern=SESSION_PATTERN)]


@router.get(
    "/transactions",
    response_model=TransactionListResponse,
    summary="The open transaction, its undo log, and finished history",
)
def get_transactions(
    managed: DatabaseDep, session: SessionParam = DEFAULT_SESSION
) -> TransactionListResponse:
    with managed.use() as db, db.in_session(session):
        return mappers.transactions_to_api(db.transactions, session=session)


@router.post(
    "/transactions",
    response_model=TransactionResultResponse,
    summary="BEGIN",
)
def begin_transaction(
    managed: DatabaseDep, session: SessionParam = DEFAULT_SESSION
) -> TransactionResultResponse:
    with managed.use() as db, db.in_session(session):
        transaction = db.begin()
        model = mappers.transaction_to_api(transaction, with_records=True)
    return TransactionResultResponse(
        action="begin",
        transaction=model,
        message=f"transaction {model.transaction_id} open",
    )


@router.post(
    "/transactions/commit",
    response_model=TransactionResultResponse,
    summary="COMMIT: keep the work and release the undo log",
)
def commit_transaction(
    managed: DatabaseDep, session: SessionParam = DEFAULT_SESSION
) -> TransactionResultResponse:
    """Commit, unless a statement already failed — then this rolls back.

    PostgreSQL's behaviour, and the reason ``action`` reports the *outcome*
    rather than echoing the request: a caller that asked to commit and got a
    rollback needs to be told in the field it is going to switch on, not only in
    prose it might not render.
    """
    with managed.use() as db, db.in_session(session):
        transaction = db.commit()
        model = mappers.transaction_to_api(transaction)
    if model.state == "aborted":
        return TransactionResultResponse(
            action="rollback",
            transaction=model,
            message=(
                f"transaction {model.transaction_id} rolled back: a statement in "
                f"it failed ({model.pages_restored} page(s) restored)"
            ),
        )
    return TransactionResultResponse(
        action="commit",
        transaction=model,
        message=(
            f"transaction {model.transaction_id} committed "
            f"({model.statements} statement(s))"
        ),
    )


@router.post(
    "/transactions/rollback",
    response_model=TransactionResultResponse,
    summary="ROLLBACK: put every touched page back as it was",
)
def rollback_transaction(
    managed: DatabaseDep, session: SessionParam = DEFAULT_SESSION
) -> TransactionResultResponse:
    with managed.use() as db, db.in_session(session):
        transaction = db.rollback()
        model = mappers.transaction_to_api(transaction)
    return TransactionResultResponse(
        action="rollback",
        transaction=model,
        message=(
            f"transaction {model.transaction_id} rolled back "
            f"({model.pages_restored} page(s) restored)"
        ),
    )
