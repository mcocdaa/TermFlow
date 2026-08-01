"""Confirmed, fail-fast routing for one plain-text Pane input."""

from __future__ import annotations

import asyncio
from typing import Protocol
from uuid import UUID, uuid4

from termflow_protocol import (
    CommandResultPayload,
    MessageType,
    PaneInputPayload,
    TermRenamePayload,
    TermRenameResultPayload,
    WireMessage,
)

from termflow_control_plane.config import Settings
from termflow_control_plane.connections.registry import (
    ConnectionBackpressure,
    InstanceOffline,
    LiveConnection,
    LiveInstanceRegistry,
)
from termflow_control_plane.errors import TermFlowError


class AuditWriter(Protocol):
    async def record(
        self,
        operation: str,
        instance_id: UUID | None,
        pane_id: str | None,
        input_bytes: int | None,
        result: str,
        error_code: str | None,
    ) -> object: ...


_BRIDGE_ERROR_STATUS = {
    "pane_not_found": 404,
    "invalid_input": 422,
    "payload_too_large": 413,
    "backpressure": 429,
}


class CommandRouter:
    def __init__(
        self,
        *,
        registry: LiveInstanceRegistry,
        audit: AuditWriter,
        settings: Settings,
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._timeout = settings.command_timeout_seconds

    async def _record(
        self,
        instance_id: UUID,
        pane_id: str,
        input_bytes: int,
        result: str,
        error_code: str | None,
    ) -> None:
        await self._audit.record(
            operation="pane.input",
            instance_id=instance_id,
            pane_id=pane_id,
            input_bytes=input_bytes,
            result=result,
            error_code=error_code,
        )

    async def send_input(
        self,
        instance_id: UUID,
        pane_id: str,
        text: str,
        submit: bool,
        idempotency_key: UUID,
    ) -> CommandResultPayload:
        input_bytes = len(text.encode("utf-8"))
        try:
            connection = await self._registry.get(instance_id)
        except InstanceOffline as exc:
            await self._record(instance_id, pane_id, input_bytes, "rejected", "instance_offline")
            raise TermFlowError(
                "instance_offline",
                409,
                "The Instance is not connected.",
            ) from exc

        if connection.topology is None:
            try:
                async with asyncio.timeout(self._timeout):
                    await connection.topology_ready.wait()
            except TimeoutError:
                pass
        if connection.topology is None:
            await self._record(
                instance_id,
                pane_id,
                input_bytes,
                "rejected",
                "topology_unavailable",
            )
            raise TermFlowError(
                "topology_unavailable",
                409,
                "The Instance has not reported its topology.",
            )
        if not connection.topology.contains_pane(pane_id):
            await self._record(instance_id, pane_id, input_bytes, "rejected", "pane_not_found")
            raise TermFlowError("pane_not_found", 404, "The Pane does not exist.")

        command_id = uuid4()
        payload = PaneInputPayload(
            command_id=command_id,
            idempotency_key=idempotency_key,
            pane_id=pane_id,
            text=text,
            submit=submit,
        )
        message = WireMessage(
            type=MessageType.PANE_INPUT,
            instance_id=instance_id,
            payload=payload.model_dump(mode="json"),
        )
        future: asyncio.Future[CommandResultPayload] = (
            asyncio.get_running_loop().create_future()
        )
        connection.pending[command_id] = future
        try:
            try:
                await self._registry.enqueue(instance_id, message)
            except ConnectionBackpressure as exc:
                await self._record(instance_id, pane_id, input_bytes, "rejected", "backpressure")
                raise TermFlowError(
                    "backpressure",
                    429,
                    "The Instance command queue is full.",
                ) from exc
            except InstanceOffline as exc:
                await self._record(
                    instance_id,
                    pane_id,
                    input_bytes,
                    "rejected",
                    "instance_offline",
                )
                raise TermFlowError(
                    "instance_offline",
                    409,
                    "The Instance is not connected.",
                ) from exc

            try:
                async with asyncio.timeout(self._timeout):
                    result = await future
            except TimeoutError as exc:
                await self._record(instance_id, pane_id, input_bytes, "unknown", "command_timeout")
                raise TermFlowError(
                    "command_timeout",
                    504,
                    "The Bridge did not confirm the command in time.",
                ) from exc
            except InstanceOffline as exc:
                await self._record(instance_id, pane_id, input_bytes, "unknown", "outcome_unknown")
                raise TermFlowError(
                    "outcome_unknown",
                    409,
                    "The connection was lost before command confirmation.",
                ) from exc

            if not result.ok:
                error_code = result.error_code or "connection_lost"
                await self._record(instance_id, pane_id, input_bytes, "failed", error_code)
                raise TermFlowError(
                    error_code,
                    _BRIDGE_ERROR_STATUS.get(error_code, 409),
                    "The Bridge rejected the command.",
                )
            await self._record(instance_id, pane_id, input_bytes, "ok", None)
            return result
        finally:
            connection.pending.pop(command_id, None)

    @staticmethod
    def resolve_result(
        connection: LiveConnection,
        result: CommandResultPayload,
    ) -> bool:
        future = connection.pending.pop(result.command_id, None)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True

    async def rename_term(self, instance_id: UUID, name: str) -> TermRenameResultPayload:
        try:
            connection = await self._registry.get(instance_id)
        except InstanceOffline as exc:
            await self._audit.record(
                "term.rename",
                instance_id,
                None,
                None,
                "rejected",
                "instance_offline",
            )
            raise TermFlowError(
                "instance_offline",
                409,
                "The Instance is not connected.",
            ) from exc

        command_id = uuid4()
        payload = TermRenamePayload(command_id=command_id, name=name)
        message = WireMessage(
            type=MessageType.TERM_RENAME,
            instance_id=instance_id,
            payload=payload.model_dump(mode="json"),
        )
        future: asyncio.Future[TermRenameResultPayload] = (
            asyncio.get_running_loop().create_future()
        )
        connection.pending_renames[command_id] = future
        try:
            try:
                await self._registry.enqueue(instance_id, message)
            except ConnectionBackpressure as exc:
                await self._audit.record(
                    "term.rename", instance_id, None, None, "rejected", "backpressure"
                )
                raise TermFlowError(
                    "backpressure",
                    429,
                    "The Instance command queue is full.",
                ) from exc
            try:
                async with asyncio.timeout(self._timeout):
                    result = await future
            except TimeoutError as exc:
                await self._audit.record(
                    "term.rename", instance_id, None, None, "unknown", "command_timeout"
                )
                raise TermFlowError(
                    "command_timeout",
                    504,
                    "The Bridge did not confirm the rename.",
                ) from exc
            except InstanceOffline as exc:
                await self._audit.record(
                    "term.rename", instance_id, None, None, "unknown", "outcome_unknown"
                )
                raise TermFlowError(
                    "outcome_unknown",
                    409,
                    "The connection was lost before rename confirmation.",
                ) from exc
            if not result.ok:
                error_code = result.error_code or "rename_failed"
                await self._audit.record(
                    "term.rename", instance_id, None, None, "failed", error_code
                )
                raise TermFlowError(error_code, 409, "The Bridge rejected the rename.")
            await self._audit.record("term.rename", instance_id, None, None, "ok", None)
            return result
        finally:
            connection.pending_renames.pop(command_id, None)

    @staticmethod
    def resolve_rename(
        connection: LiveConnection,
        result: TermRenameResultPayload,
    ) -> bool:
        future = connection.pending_renames.pop(result.command_id, None)
        if future is None or future.done():
            return False
        future.set_result(result)
        return True
