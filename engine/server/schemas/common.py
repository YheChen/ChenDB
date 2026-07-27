"""Shared API model pieces."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ApiError",
    "ApiModel",
    "HealthResponse",
    "PageInfo",
    "PaginationMeta",
    "RequestModel",
]


class ApiModel(BaseModel):
    """Base for responses. Permissive on input, explicit on output."""

    model_config = ConfigDict(frozen=True)


class RequestModel(BaseModel):
    """Base for request bodies.

    ``extra="forbid"`` turns a client typo into a 422 instead of a silently
    dropped field — worth it for a tool whose whole job is showing what really
    happened.
    """

    model_config = ConfigDict(extra="forbid")


class ApiError(ApiModel):
    """Uniform error envelope for every non-2xx response."""

    error: str = Field(description="Machine-readable error class, e.g. 'DatabaseNotFound'")
    message: str = Field(description="Human-readable explanation")
    detail: dict[str, object] | None = Field(
        default=None, description="Optional structured context"
    )


class HealthResponse(ApiModel):
    engine_version: str
    api_version: str
    milestone: int = Field(description="Highest completed milestone")
    workspace: str = Field(description="Workspace directory name, not a full path")
    open_databases: int
    features: dict[str, bool] = Field(
        description=(
            "Which capabilities exist in this build. The UI hides panels whose "
            "feature is false rather than showing controls that cannot work."
        )
    )


class PaginationMeta(ApiModel):
    """Offset pagination for row and page listings."""

    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    returned: int = Field(ge=0)
    total: int | None = Field(
        default=None,
        description="Total available, when counting it is cheap enough to do",
    )
    has_more: bool


class PageInfo(ApiModel):
    """Cursor pagination for the event stream."""

    after_seq: int | None
    returned: int
    next_cursor: int | None = Field(
        description="Pass as after_seq to continue; null when caught up"
    )
    has_more: bool
