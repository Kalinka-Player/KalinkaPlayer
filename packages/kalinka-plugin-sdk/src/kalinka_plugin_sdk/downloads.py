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
                    raise ValueError(
                        f"the body ended at {received} of {expected} bytes"
                    )
                # A power cut after the rename must not leave a short dest.
                out.flush()
                os.fsync(out.fileno())
        os.replace(tmp, dest)
        return True
    except Exception as e:
        logger.warning("download failed (%s): %s", shown, _scrubbed(str(e), url))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _without_secrets(url: str) -> str:
    """The address as a log may show it: a key rides in its query or before
    its host."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparsable address>"
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def _scrubbed(text: str, url: str) -> str:
    """``text`` with the address's secrets taken out, as an error quoting the
    address, raw or as its repr, would carry them. Split by hand: urlsplit
    drops the control characters such an error quotes."""
    authority = url.partition("://")[2].partition("/")[0]
    secrets = [url.partition("?")[2], url.partition("#")[2]]
    secrets.append(authority.rpartition("@")[0])
    for secret in filter(None, secrets):
        for form in (secret, repr(secret)[1:-1]):
            text = text.replace(form, "<secret>")
    return text
