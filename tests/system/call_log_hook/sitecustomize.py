"""Times the library-database calls of the server under test.

Python imports a module of this name at startup when its directory is on
PYTHONPATH, so this reaches the server and every worker process it starts.
It does nothing unless KALINKA_CALL_LOG names a file. With it set, each call
a worker makes through ``LibraryDb._open`` that changed rows, failed, or
took half a second is appended there as one tab-separated line: start
(epoch seconds), duration, process name, the method, rows changed, and the
error it ended with. See ``call_log.py`` for the reader.
"""

import importlib.abc
import importlib.machinery
import os
import sys
import time

_LOG = os.environ.get("KALINKA_CALL_LOG")
_SLOW_S = 0.5
_TARGET = "kalinka_plugin_localfiles.db_connection"
_PLUMBING = ("db_connection.py", "contextlib.py", "worker_utils.py")


def _process_name() -> str:
    try:
        with open("/proc/self/comm") as comm:
            return comm.read().strip()
    except OSError:
        return str(os.getpid())


def _calling_method() -> str:
    frame = sys._getframe(2)
    while frame is not None:
        code = frame.f_code
        if not code.co_filename.endswith(_PLUMBING):
            return f"{os.path.basename(code.co_filename)}:{code.co_qualname}"
        frame = frame.f_back
    return "?"


def _time_calls(module) -> None:
    from contextlib import asynccontextmanager

    calling = module._calling

    @asynccontextmanager
    async def timed(conn):
        method = _calling_method()
        started, clock = time.time(), time.monotonic()
        changes_before = conn.total_changes
        error = ""
        try:
            async with calling(conn) as call:
                yield call
        except BaseException as e:
            error = f"{type(e).__name__}: {e}"[:160].replace("\t", " ").replace("\n", " ")
            raise
        finally:
            seconds = time.monotonic() - clock
            changed = conn.total_changes - changes_before
            if changed or error or seconds >= _SLOW_S:
                with open(_LOG, "a") as log:
                    log.write(
                        f"{started:.3f}\t{seconds:.3f}\t{_process_name()}\t{method}"
                        f"\t{changed}\t{error}\n"
                    )

    module._calling = timed


class _Loader(importlib.abc.Loader):
    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        _time_calls(module)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is not None and spec.loader is not None:
            spec.loader = _Loader(spec.loader)
        return spec


if _LOG:
    sys.meta_path.insert(0, _Finder())
