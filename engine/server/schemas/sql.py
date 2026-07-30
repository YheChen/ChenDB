"""SQL parse API models.

The AST crosses the wire as a **flattened node list**, not a nested tree:

    { "nodes": [
        { "node_id": 0, "node_type": "SelectStatement", "children": [1, 5, 6], ... },
        { "node_id": 1, "node_type": "SelectItem",      "children": [2], ... }
      ],
      "root_ids": [0] }

Two reasons. A recursive Pydantic model produces a self-referential OpenAPI
schema, which most TypeScript generators handle badly. And the frontend needs
random access by ``node_id`` anyway (clicking a token has to find the smallest
node containing it) which a flat map gives for free.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from engine.server.schemas.common import ApiModel, RequestModel

__all__ = [
    "AstNodeModel",
    "AstTreeModel",
    "ParseRequest",
    "ParseResponse",
    "SqlErrorModel",
    "StatementModel",
    "TokenModel",
]


class TokenModel(ApiModel):
    """One token, with the source range it covers."""

    index: int
    type: str = Field(description="Token category: keyword, identifier, int_literal, ...")
    lexeme: str = Field(description="Exact source text, before unescaping")
    start: int = Field(description="Character offset of the first character")
    end: int = Field(description="Character offset one past the last")
    line: int
    column: int
    keyword: str | None = Field(default=None, description="Set when type is 'keyword'")
    value: Any = Field(default=None, description="Decoded value, for literals")


class AstNodeModel(ApiModel):
    """One AST node in the flattened tree."""

    node_id: int
    node_type: str = Field(description="Class name, e.g. 'BinaryOp'")
    start: int
    end: int
    line: int
    column: int
    text: str = Field(description="The source fragment this node was parsed from")
    children: list[int] = Field(description="node_ids of direct children, in order")
    attributes: dict[str, Any] = Field(
        description="Scalar fields: operator, name, value, data_type, ..."
    )
    label: str = Field(
        description="Short display label: the operator, name or value if it has one"
    )


class StatementModel(ApiModel):
    """One parsed statement."""

    root_id: int
    kind: str = Field(description="Statement node type, e.g. 'SelectStatement'")
    start: int
    end: int
    text: str


class AstTreeModel(ApiModel):
    nodes: list[AstNodeModel]
    root_ids: list[int] = Field(description="One per statement, in source order")


class SqlErrorModel(ApiModel):
    """A lex or parse failure, positioned for an editor marker."""

    kind: str = Field(description="LexError, ParseError or UnsupportedSqlError")
    message: str
    start: int
    end: int
    line: int
    column: int
    expected: list[str] = Field(
        default_factory=list, description="What the parser would have accepted"
    )
    found: str = Field(default="", description="What it saw instead")


class ParseRequest(RequestModel):
    sql: str = Field(
        max_length=100_000,
        description="One or more statements, separated by semicolons",
    )


class ParseResponse(ApiModel):
    """Tokens, AST and error together, all three are partial-result friendly.

    ``tokens`` can be non-empty while ``statements`` is empty and ``error`` is
    set: that is a half-typed query, the normal state of one being written.
    """

    sql: str
    ok: bool
    tokens: list[TokenModel]
    ast: AstTreeModel
    statements: list[StatementModel]
    error: SqlErrorModel | None = None
    lexed_ok: bool = Field(
        description="False when tokenizing failed, so `tokens` is truncated"
    )
    token_count: int
    node_count: int
    duration_ns: int
