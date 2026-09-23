"""The AcoustID key rides in the request body, never in the URL.

A request's URL turns up in its exceptions — logged at WARNING whenever the
service is unreachable — and in urllib3's debug log, so a key in the query
string is a key in the journal.
"""

import pytest
import requests

from kalinka_plugin_localfiles.config_model import LocalFilesConfig
from kalinka_plugin_localfiles.enricher import acoustid_plugin
from kalinka_plugin_localfiles.enricher.acoustid_plugin import AcoustIdPlugin
from kalinka_plugin_localfiles.enricher.enricher_plugin import (
    TransientEnrichmentError,
)

KEY = "acoustid-key-do-not-log"


class _Response:
    status_code = 200

    def json(self):
        return {"status": "ok", "results": []}


def _plugin() -> AcoustIdPlugin:
    cfg = LocalFilesConfig(db_path=":memory:")
    cfg.enricher.plugins.acoustid.api_key = KEY
    return AcoustIdPlugin(cfg, db_manager=None)


def _no_get(*_args, **_kwargs):
    raise AssertionError("a GET would put the key in the URL")


def test_the_key_is_sent_in_the_body(monkeypatch):
    sent = {}

    def post(url, data=None, **kwargs):
        sent.update(url=url, data=data, kwargs=kwargs)
        return _Response()

    monkeypatch.setattr(acoustid_plugin.requests, "post", post)
    monkeypatch.setattr(acoustid_plugin.requests, "get", _no_get)

    _plugin()._lookup_fingerprint("AQAA", 200)

    assert KEY not in sent["url"]
    assert "params" not in sent["kwargs"]
    assert sent["data"]["client"] == KEY


def test_an_unreachable_service_is_reported_without_the_key(monkeypatch):
    def post(url, **_kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(acoustid_plugin.requests, "post", post)
    monkeypatch.setattr(acoustid_plugin.requests, "get", _no_get)

    with pytest.raises(TransientEnrichmentError) as raised:
        _plugin()._lookup_fingerprint("AQAA", 200)

    assert KEY not in str(raised.value)
