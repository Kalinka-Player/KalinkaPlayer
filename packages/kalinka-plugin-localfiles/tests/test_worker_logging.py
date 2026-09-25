"""Worker logging must reject SMB payloads before QueueHandler formats them."""

import importlib
import logging
import queue

import pytest


@pytest.mark.parametrize(
    "worker_name", ["librarian", "embedder.embedder", "searcher.searcher"]
)
def test_workers_skip_packet_formatting_but_forward_warnings(
    worker_name, monkeypatch
):
    worker = importlib.import_module(f"kalinka_plugin_localfiles.{worker_name}")
    names = (
        "smbprotocol", "smbprotocol.open", "smbclient", "smbclient._io",
        "spnego", "spnego._ntlm", "kalinka_worker_logging_test",
    )
    loggers = [logging.getLogger(name) for name in names]
    previous_levels = [logger.level for logger in loggers]
    root = logging.getLogger()
    previous_root_level, previous_handlers = root.level, root.handlers[:]
    records = queue.Queue()

    class Packet:
        formatted = 0

        def __str__(self):
            self.formatted += 1
            return "expensive audio packet dump"

    packet = Packet()
    login = Packet()

    async def probe(*args):
        for name in ("smbprotocol.open", "smbclient._io"):
            logger = logging.getLogger(name)
            logger.debug(packet)
            logger.info(packet)
            logger.warning("share unavailable")
            logger.error("read failed")
        logging.getLogger("spnego._ntlm").debug(login)
        logging.getLogger("kalinka_worker_logging_test").debug("worker diagnostic")

    monkeypatch.setattr(worker, "async_main", probe)
    monkeypatch.setattr(worker, "set_proc_title", lambda name: None)
    try:
        # Reproduce a fresh spawned worker: no inherited logger overrides.
        for logger in loggers:
            logger.setLevel(logging.NOTSET)
        worker.main(None, records, None, None)
        assert packet.formatted == 0
        assert login.formatted == 0, "the share login exchange was formatted"
        forwarded = []
        while not records.empty():
            forwarded.append(records.get_nowait().getMessage())
        assert forwarded.count("share unavailable") == 2
        assert forwarded.count("read failed") == 2
        assert "worker diagnostic" in forwarded
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_root_level)
        for logger, level in zip(loggers, previous_levels):
            logger.setLevel(level)
