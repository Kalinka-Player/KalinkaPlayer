"""The production app, built by ``create_app``, with only the outside world
faked: installed plugins, mDNS and the release check."""

import asyncio
from contextlib import asynccontextmanager

import httpx

from kalinka_server import server, update_check
from kalinka_server.player_setup import modules


class _NoServiceDiscovery:
    def __init__(self, *_args, **_kwargs):
        pass

    async def register_service(self):
        pass

    async def unregister_service(self):
        pass


async def _no_release_check(*_args, **_kwargs):
    await asyncio.Event().wait()


def isolate_app(monkeypatch, tmp_path) -> None:
    """Point the server's files at ``tmp_path`` and keep it off the network."""
    monkeypatch.setenv("KALINKA_PREFIX", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # create_app rebinds these process globals; restore them afterwards.
    for name, value in list(vars(modules).items()):
        monkeypatch.setattr(modules, name, value)
    monkeypatch.setattr(server, "_browse_registry", server._browse_registry)
    monkeypatch.setattr(modules, "_scan_entry_points", lambda: iter(()))
    monkeypatch.setattr(server, "ServiceDiscovery", _NoServiceDiscovery)
    monkeypatch.setattr(update_check.checker, "run", _no_release_check)


@asynccontextmanager
async def running(app):
    # The lifespan swallows exceptions raised into it, so a failed assertion
    # must not be thrown through it.
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield client
    finally:
        await lifespan.__aexit__(None, None, None)
