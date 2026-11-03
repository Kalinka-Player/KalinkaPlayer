"""Routes optional plugin receiver controllers; no protocol policy lives here."""

import asyncio
import logging

from fastapi import FastAPI, HTTPException

from kalinka_plugin_sdk.events import ReceiversChangedEvent
from kalinka_plugin_sdk.receiver_control import (
    ReceiverCommand,
    ReceiverDescriptor,
    ReceiverError,
    ReceiverSnapshot,
    ReceiversSnapshot,
)

logger = logging.getLogger(__name__)
CALL_TIMEOUT = 15


class ReceiverRegistry:
    def __init__(self, modules, dispatch):
        self.modules, self.dispatch = modules, dispatch
        self._task = None
        self._dirty = False
        self._closed = False
        self._last = None

    def entries(self):
        return {
            name: p
            for name, p in self.modules.prepared_input_modules.items()
            if isinstance(getattr(p.plugin_class, "RECEIVER", None), ReceiverDescriptor)
        }

    def _entry(self, name):
        entry = self.entries().get(name)
        if entry is None:
            raise HTTPException(
                404,
                detail={
                    "code": "receiver_not_found",
                    "message": "Receiver is not installed",
                },
            )
        return entry

    @staticmethod
    def _controller(entry):
        return (
            entry.plugin_instance.get_receiver_control()
            if entry.plugin_instance
            else None
        )

    async def snapshot(self, name):
        entry = self._entry(name)
        descriptor = entry.plugin_class.RECEIVER
        try:
            controller = self._controller(entry)
            if controller is None:
                raise ReceiverError(
                    "backend_unavailable",
                    entry.error_message or "Receiver could not initialize",
                )
            snapshot = await asyncio.wait_for(controller.snapshot(), 2)
            snapshot = ReceiverSnapshot.model_validate(snapshot)
        except Exception as error:
            snapshot = ReceiverSnapshot(
                enabled=entry.plugin_context.config.enabled,
                state="error",
                message=str(error) or "Receiver did not answer",
            )
        return snapshot.model_copy(
            update={
                "receiver_id": name,
                "title": descriptor.title,
                "kind": descriptor.kind,
                "instructions": descriptor.instructions,
                "enabled": entry.plugin_context.config.enabled,
                "actions": [
                    a for a in snapshot.actions if a.command in descriptor.commands
                ],
            }
        )

    async def snapshot_all(self):
        return ReceiversSnapshot(
            receivers=list(
                await asyncio.gather(*(self.snapshot(name) for name in self.entries()))
            )
        )

    def changed(self, name=None):
        if self._closed or (name is not None and name not in self.entries()):
            return
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._publish())

    async def _publish(self):
        try:
            while self._dirty:
                self._dirty = False
                snapshot = await self.snapshot_all()
                if snapshot != self._last:
                    self._last = snapshot
                    self.dispatch(ReceiversChangedEvent(receivers=snapshot.receivers))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Could not publish receiver state")

    async def close(self):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def execute(self, name, command):
        entry = self._entry(name)
        try:
            if command.command not in entry.plugin_class.RECEIVER.commands:
                raise ReceiverError(
                    "unsupported_operation",
                    "This receiver does not support that action",
                )
            controller = self._controller(entry)
            if controller is None:
                raise ReceiverError(
                    "backend_unavailable", "Receiver could not initialize"
                )
            await asyncio.wait_for(controller.execute(command), CALL_TIMEOUT)
            return await self.snapshot(name)
        except ReceiverError as error:
            status = {
                "backend_unavailable": 503,
                "backend_timeout": 504,
                "unsupported_operation": 400,
            }.get(error.code, 409)
            raise HTTPException(
                status, detail={"code": error.code, "message": str(error)}
            ) from error
        except TimeoutError as error:
            raise HTTPException(
                504,
                detail={
                    "code": "backend_timeout",
                    "message": "Receiver did not finish; refresh its state before trying again",
                },
            ) from error
        except Exception as error:
            logger.exception("Receiver %s command failed", name)
            raise HTTPException(
                503,
                detail={
                    "code": "backend_unavailable",
                    "message": str(error) or "Receiver failed",
                },
            ) from error
        finally:
            self.changed(name)


def register_receiver_routes(app: FastAPI, registry: ReceiverRegistry):
    @app.get("/receivers", response_model=ReceiversSnapshot)
    async def receivers():
        return await registry.snapshot_all()

    @app.get("/receivers/{receiver_id}", response_model=ReceiverSnapshot)
    async def receiver(receiver_id: str):
        return await registry.snapshot(receiver_id)

    @app.post("/receivers/{receiver_id}/commands", response_model=ReceiverSnapshot)
    async def command(receiver_id: str, command: ReceiverCommand):
        return await registry.execute(receiver_id, command)
