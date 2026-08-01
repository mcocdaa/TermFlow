"""Dashboard read model grouped by Computer and Term."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from termflow_protocol import (
    ComputerSummary,
    DashboardMetrics,
    DashboardResponse,
    TermSummary,
    TopologySnapshot,
)

from termflow_control_plane.api.dependencies import (
    get_registry,
    get_repositories,
    require_admin,
)
from termflow_control_plane.connections.registry import LiveConnection, LiveInstanceRegistry
from termflow_control_plane.persistence.models import Installation, Instance
from termflow_control_plane.persistence.repositories import RepositoryBundle

router = APIRouter(prefix="/api/v1", tags=["dashboard"])


def _topology_counts(topology: TopologySnapshot | None) -> tuple[int, int, int, str | None]:
    if topology is None:
        return 0, 0, 0, None
    panes = [pane for window in topology.windows for pane in window.panes]
    active = [pane for pane in panes if pane.active and not pane.dead]
    current_command = active[0].current_command if active else None
    return len(topology.windows), len(panes), len(active), current_command


def term_summary(instance: Instance, connection: LiveConnection | None) -> TermSummary:
    topology = connection.topology if connection is not None else None
    window_count, pane_count, active_count, current_command = _topology_counts(topology)
    return TermSummary(
        instance_id=instance.id,
        name=instance.name,
        online=connection is not None,
        window_count=window_count,
        pane_count=pane_count,
        active_pane_count=active_count,
        current_command=current_command,
        last_seen_at=instance.last_seen_at,
    )


async def computer_summaries(
    repositories: RepositoryBundle,
    registry: LiveInstanceRegistry,
) -> list[ComputerSummary]:
    installations = await repositories.installations.list_all()
    instances = await repositories.instances.list_all()
    instances_by_installation: dict[UUID, list[Instance]] = {}
    for instance in instances:
        instances_by_installation.setdefault(instance.installation_id, []).append(instance)
    summaries: list[ComputerSummary] = []
    for installation in installations:
        terms = [
            term_summary(instance, await registry.maybe_get(instance.id))
            for instance in instances_by_installation.get(installation.id, [])
        ]
        summaries.append(_computer_summary(installation, terms))
    return summaries


def _computer_summary(
    installation: Installation,
    terms: list[TermSummary],
) -> ComputerSummary:
    return ComputerSummary(
        installation_id=installation.id,
        hostname=installation.hostname,
        display_name=installation.display_name or installation.hostname or "Computer",
        platform=installation.platform,
        client_version=installation.client_version,
        registered_at=installation.created_at,
        last_seen_at=installation.last_seen_at,
        online=any(term.online for term in terms),
        terms=terms,
    )


@router.get(
    "/dashboard",
    response_model=DashboardResponse,
    dependencies=[Depends(require_admin)],
)
async def get_dashboard(
    repositories: Annotated[RepositoryBundle, Depends(get_repositories)],
    registry: Annotated[LiveInstanceRegistry, Depends(get_registry)],
) -> DashboardResponse:
    computers = await computer_summaries(repositories, registry)
    terms = [term for computer in computers for term in computer.terms]
    interactions = await repositories.audit.count_since(datetime.now(UTC) - timedelta(hours=24))
    return DashboardResponse(
        metrics=DashboardMetrics(
            online_terms=sum(term.online for term in terms),
            total_terms=len(terms),
            active_panes=sum(
                term.active_pane_count for term in terms if term.online
            ),
            interactions_24h=interactions,
            computers=len(computers),
        ),
        computers=computers,
    )
