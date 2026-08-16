"""Core capability-discovery endpoint for the Agent Broker plugin.

C hides Agent UI/streams based on this response rather than relying on Agent
API 404 responses, so a disabled Agent Broker remains discoverable (plan M1:
"do not make feature disablement depend only on Agent API 404 responses").

The endpoint is intentionally unauthenticated and mounted unconditionally: it
reveals only a non-sensitive deployment feature flag and must stay reachable
while the plugin itself is disabled.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from termflow_control_plane.api.dependencies import get_settings
from termflow_control_plane.config import Settings

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class AgentCapabilitiesResponse(BaseModel):
    """Agent Broker capability-discovery payload consumed by C."""

    model_config = ConfigDict(extra="forbid")

    agent_broker_enabled: bool
    #: Delegated Write Grants are design-only in 0.2.0; always False so C can
    #: display the disabled capability (spec §8).
    delegated_write_grants_enabled: bool = False


@router.get("/capabilities", response_model=AgentCapabilitiesResponse)
async def get_agent_capabilities(
    settings: Annotated[Settings, Depends(get_settings)],
) -> AgentCapabilitiesResponse:
    return AgentCapabilitiesResponse(
        agent_broker_enabled=settings.agent_broker_enabled,
        delegated_write_grants_enabled=settings.agent_delegated_write_grants_enabled,
    )
