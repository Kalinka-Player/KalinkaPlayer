"""An in-memory share standing in for ``smbclient``."""

import io
import stat
from types import SimpleNamespace


class RawFile(io.RawIOBase):
    """A file on the share, counting the reads made of it and the bytes they
    fetched."""

    def __init__(self, data):
        super().__init__()
        self._data = data
        self._offset = 0
        self.reads = 0
        self.fetched = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def readinto(self, buffer):
        chunk = self._data[self._offset:self._offset + len(buffer)]
        buffer[:len(chunk)] = chunk
        self._offset += len(chunk)
        self.reads += 1
        self.fetched += len(chunk)
        return len(chunk)

    def seek(self, offset, whence=io.SEEK_SET):
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._offset,
                io.SEEK_END: len(self._data)}[whence]
        self._offset = base + offset
        return self._offset

    def tell(self):
        return self._offset


class FakeSmbClient:
    """An in-memory share, recording what it was asked and with what."""

    def __init__(self, listings=None, files=None, stats=None, failure=None):
        self.listings = listings or {}
        self.files = files or {}
        self.stats = stats or {}
        self.failure = failure
        self.calls = []
        self.logons = []
        self.opened = []
        self.session = SimpleNamespace(tree_connect_table={})

    def register_session(self, server, **kwargs):
        """What the real client pools a connection and a session under. It is
        also where a logon fails, so a configured failure surfaces here."""
        self.logons.append((server, kwargs))
        if self.failure is not None:
            raise self.failure
        return self.session

    def _record(self, unc, kwargs):
        self.calls.append((unc, kwargs))
        if self.failure is not None:
            raise self.failure

    def scandir(self, unc, **kwargs):
        self._record(unc, kwargs)
        if unc not in self.listings:
            raise OSError(f"no such directory: {unc}")
        return iter(self.listings[unc])

    def stat(self, unc, **kwargs):
        self._record(unc, kwargs)
        if unc in self.stats:
            return self.stats[unc]
        if unc in self.listings:
            return stat_result(is_dir=True)
        if unc in self.files:
            return stat_result(size=len(self.files[unc]))
        raise OSError(f"no such path: {unc}")

    def open_file(self, unc, mode="rb", buffering=-1, share_access=None,
                  **kwargs):
        """Buffered as the real client buffers: a ``BufferedReader`` of the
        size asked for, one SMB2 payload by default, or the raw file for
        0."""
        self._record(
            unc,
            dict(
                kwargs,
                mode=mode,
                buffering=buffering,
                share_access=share_access,
            ),
        )
        if unc not in self.files:
            raise OSError(f"no such file: {unc}")
        raw = RawFile(self.files[unc])
        self.opened.append(raw)
        if buffering == 0:
            return raw
        return io.BufferedReader(
            raw, buffer_size=64 * 1024 if buffering == -1 else buffering
        )


def stat_result(is_dir=False, size=0, mtime_ns=1_700_000_000_000_000_000,
                dev=305419896, ino=42):
    return SimpleNamespace(
        st_mode=stat.S_IFDIR if is_dir else stat.S_IFREG,
        st_size=size,
        st_mtime_ns=mtime_ns,
        st_dev=dev,
        st_ino=ino,
    )
