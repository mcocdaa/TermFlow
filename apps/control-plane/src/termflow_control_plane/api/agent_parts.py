"""Read-only OpenCode session parts for conversation display parity.

The Agent display wants what OpenCode shows: reasoning, structured tool
input/output, step markers.  B's canonical stream deliberately projects a
narrow contract, so this endpoint reads the runtime session through the same
deployment credentials and returns a bounded, display-only view.  Nothing is
persisted; approvals stay on the existing B-side flow.
"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from termflow_control_plane.api.dependencies import (
    get_repositories,
    get_settings,
    require_admin,
)
from termflow_control_plane.config import Settings
from termflow_control_plane.errors import TermFlowError
from termflow_control_plane.persistence.repositories import RepositoryBundle

router = APIRouter(
    prefix="/api/v1/agent/conversations",
    tags=["agent"],
    dependencies=[Depends(require_admin)],
)

_MAX_TEXT = 8192
_MAX_INPUT = 4096
_MAX_ERROR = 2048


def _bounded(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return None
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断）"


@router.get("/{conversation_id}/parts")
async def get_agent_conversation_parts(
    conversation_id: UUID,
    request: Request,
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Return bounded OpenCode session parts for the conversation (display only)."""
    del request
    if await repositories.agent_conversations.get_by_id(conversation_id) is None:
        raise TermFlowError(
            "conversation_not_found", 404, "The Agent Conversation does not exist."
        )
    ref = await repositories.agent_backend_conversations.get_by_conversation(conversation_id)
    if ref is None:
        return JSONResponse({"parts": []})
    password = settings.agent_opencode_password
    base = str(settings.agent_opencode_base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{base}/session/{ref.provider_ref}/message",
                params={"directory": settings.agent_opencode_directory, "limit": 50},
                auth=(
                    settings.agent_opencode_username,
                    password.get_secret_value() if password is not None else None,
                ),
            )
    except httpx.HTTPError:
        return JSONResponse({"parts": []})
    if response.status_code != 200:
        return JSONResponse({"parts": []})
    raw = response.json()
    if not isinstance(raw, list):
        return JSONResponse({"parts": []})

    parts: list[dict[str, object]] = []
    for message in raw[-30:]:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        role = info.get("role") if isinstance(info, dict) else None
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind in ("text", "reasoning"):
                parts.append(
                    {"type": kind, "id": part.get("id"), "role": role, "text": _bounded(part.get("text"), _MAX_TEXT)}
                )
            elif kind == "tool":
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                parts.append(
                    {
                        "type": "tool",
                        "id": part.get("id"),
                        "tool": part.get("tool"),
                        "status": state.get("status"),
                        "input": _bounded(state.get("input"), _MAX_INPUT),
                        "output": _bounded(state.get("output"), _MAX_TEXT),
                        "error": _bounded(state.get("error"), _MAX_ERROR),
                    }
                )
            elif kind in ("step-start", "step-finish"):
                parts.append(
                    {
                        "type": kind,
                        "id": part.get("id"),
                        "reason": part.get("reason"),
                        "cost": part.get("cost"),
                        "tokens": part.get("tokens"),
                    }
                )
    return JSONResponse({"parts": parts})
