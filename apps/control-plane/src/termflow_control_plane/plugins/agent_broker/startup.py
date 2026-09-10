"""Explicit recovery fence separating process health from Agent readiness."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)
Hook = Callable[[], Awaitable[object]]


class AgentStartupState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class AgentStartupResult:
    state: AgentStartupState
    reason_code: str | None = None
    critical_failures: tuple[str, ...] = ()


class AgentStartupCoordinator:
    def __init__(
        self,
        fencing: Hook,
        controller: Hook | None = None,
        topology: Hook | None = None,
        backend: Hook | None = None,
        watches: Hook | None = None,
        dispatcher: Hook | None = None,
        cleanup: Hook | None = None,
    ) -> None:
        self._fencing = fencing
        self._hooks = (controller, topology, watches, backend, dispatcher)
        self._cleanup = cleanup
        self._recovered = False
        self._lock = asyncio.Lock()
        self.result = AgentStartupResult(AgentStartupState.STARTING)

    async def recover(self) -> AgentStartupResult:
        async with self._lock:
            if self.result.state == AgentStartupState.READY:
                return self.result
            try:
                report = await self._fencing()
                failures = getattr(report, "critical_failures", ())
                if failures:
                    raise RuntimeError("critical recovery failed")
                self._recovered = True
                self.result = AgentStartupResult(AgentStartupState.STARTING)
            except Exception:
                logger.exception("Agent recovery fence failed")
                self._recovered = False
                self.result = AgentStartupResult(
                    AgentStartupState.DEGRADED, "recovery_failed", ("fencing",)
                )
            if self._cleanup is not None:
                try:
                    await self._cleanup()
                except Exception:
                    logger.exception("Agent cleanup retry failed")
            return self.result

    async def activate(self) -> AgentStartupResult:
        async with self._lock:
            if not self._recovered or self.result.state == AgentStartupState.READY:
                return self.result
            try:
                for hook in self._hooks:
                    if hook is not None:
                        await hook()
                self.result = AgentStartupResult(AgentStartupState.READY)
            except Exception:
                logger.exception("Agent activation failed")
                self._recovered = False
                self.result = AgentStartupResult(
                    AgentStartupState.DEGRADED, "recovery_failed", ("activation",)
                )
            return self.result
