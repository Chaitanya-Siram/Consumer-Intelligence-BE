"""Custom exception handlers for FastAPI request validation errors.

FastAPI's default response for Pydantic validation failures is a verbose list
of {type, loc, msg, input, url} entries. This module flattens that into a
single human-readable string under the standard `detail` key, e.g.

    {"detail": "workflow.branches[1].assembly.branding.client_name: Input should be a valid string"}
"""
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from configs import logger


_LOC_PREFIXES_TO_STRIP = {"body", "query", "path", "header", "cookie"}


def _format_loc(loc: tuple) -> str:
    """Return just the last named field from the loc tuple, e.g.
    ('body', 'workflow', 'branches', 1, 'assembly', 'branding', 'client_name')
    → 'client_name'. List indices are skipped so we don't return '1'."""
    for segment in reversed(loc):
        if isinstance(segment, str) and segment not in _LOC_PREFIXES_TO_STRIP:
            return segment
    return "(root)"


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    messages: list[str] = []
    for err in exc.errors():
        field = _format_loc(err.get("loc", ()))
        msg = err.get("msg", "Invalid value")
        messages.append(f"{field}: {msg}")
    detail = "; ".join(messages) if messages else "Validation failed"
    logger.warning(f"Validation failed for {request.method} {request.url.path}: {detail}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": detail},
    )
