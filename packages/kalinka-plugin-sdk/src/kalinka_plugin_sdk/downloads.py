"""Fetching a file a plugin needs, such as a model or an index.

A connection that stalls without a reset, as on a Wi-Fi drop or behind a
captive portal, would otherwise hold the thread fetching it for ever, and a
body without end would fill the disk.
"""

from __future__ import annotations

import logging
import os
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_CHUNK = 1 << 20


def download_file(
    url: str,
    dest: str,
    *,
    timeout_s: float = 30.0,
    max_bytes: Optional[int] = None,
) -> bool:
    """SDK 3.24: fetch ``url`` to ``dest``; True once it is there whole.

    Blocking, so call it from an executor. The body goes to ``dest + ".part"``
    and replaces ``dest`` only once complete, so ``dest`` never holds a
    truncated file. Gives up, returning False with the reason logged and no
    ``.part`` left, when the address does not answer or answers with an
    error, when the network sends nothing for ``timeout_s``, when the body
    ends short of its Content-Length, or when it is longer than
    ``max_bytes``. ``timeout_s`` bounds each wait, not the whole download.
    """
    tmp = dest + ".part"
    shown = _without_secrets(url)
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        logger.info("downloading %s", shown)
        limit = float("inf") if max_bytes is None else max_bytes
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            announced = response.headers.get("Content-Length")
            expected = None if announced is None else int(announced)
            if expected is not None and expected > limit:
                raise ValueError(f"{expected} bytes announced, over {max_bytes}")
            received = 0
            with open(tmp, "wb") as out:
                while chunk := response.read(_CHUNK):
                    received += len(chunk)
                    if received > limit:
                        raise ValueError(f"the body runs over {max_bytes} bytes")
                    out.write(chunk)
            if expected is not None and received < expected:
                raise ValueError(f"the body ended at {received} of {expected} bytes")
        os.replace(tmp, dest)
        return True
    except Exception as e:
        logger.warning("download failed (%s): %s", shown, e)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        return False


def _without_secrets(url: str) -> str:
    """The address as a log may show it: a key rides in its query or before
    its host."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
