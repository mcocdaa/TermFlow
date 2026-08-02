"""Stable application errors that never echo sensitive payloads."""

from dataclasses import dataclass


@dataclass(slots=True)
class TermFlowError(Exception):
    code: str
    status_code: int
    message: str
    retry_after: int | None = None
