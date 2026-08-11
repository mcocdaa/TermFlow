"""Concrete feature context handed to B feature plugins.

This module carries the assembly-time container shape only.  Ports are
constructor-injected and intentionally unvalidated so the same context type
serves fakes, tests, and the eventual composition root without importing
any real service wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from termflow_control_plane.plugins.protocol import (
    AgentRuntimeSupervisor,
    AuthPort,
    LifecyclePort,
    TerminalCommandPort,
    TerminalObservationPort,
    TermPort,
    UnitOfWorkFactory,
)


@dataclass(frozen=True, slots=True)
class FeatureContext:
    """Immutable dependency surface for a B feature plugin.

    Mirrors ``BFeatureContext`` with concrete storage so registries and
    composition roots can construct and hand out a single object.
    """

    auth: AuthPort
    terms: TermPort
    observation: TerminalObservationPort
    commands: TerminalCommandPort
    persistence: UnitOfWorkFactory
    lifecycle: LifecyclePort
    runtime: AgentRuntimeSupervisor


def build_feature_context(
    *,
    auth: AuthPort,
    terms: TermPort,
    observation: TerminalObservationPort,
    commands: TerminalCommandPort,
    persistence: UnitOfWorkFactory,
    lifecycle: LifecyclePort,
    runtime: AgentRuntimeSupervisor,
) -> FeatureContext:
    """Assemble a :class:`FeatureContext` from injected ports."""
    return FeatureContext(
        auth=auth,
        terms=terms,
        observation=observation,
        commands=commands,
        persistence=persistence,
        lifecycle=lifecycle,
        runtime=runtime,
    )
