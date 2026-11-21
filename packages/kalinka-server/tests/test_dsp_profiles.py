import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalinka_server.dsp_profiles import DspProfiles, RewImport, import_rew
from kalinka_server.renderer_proto import renderer_pb2 as pb

EXPORT = """Filter Settings file
Room EQ V5.31
Equaliser: Generic
Filter  1: ON  PK       Fc   53.25 Hz  Gain  -5.50 dB  Q  4.000
Filter  2: ON  LSQ      Fc   90.0 Hz   Gain   2.5 dB   Q  0.707
Filter  3: OFF None
Filter  4: OFF LP       Fc   250 Hz
Filter  5: ON  PK       Fc   100 Hz    Gain   0 dB     Q  1
"""


def test_publish_select_and_download(tmp_path):
    store = DspProfiles(tmp_path)
    app = FastAPI()
    app.include_router(store.router())
    client = TestClient(app)
    payload = {
        "name": "Listening room",
        "left": EXPORT,
        "right": EXPORT.replace("-5.50", "-2"),
    }
    response = client.post("/dsp/profiles/rew", json=payload)
    assert response.status_code == 200, response.text
    digest = response.json()["id"]
    assert client.post("/dsp/profiles/rew", json=payload).json()["id"] == digest
    content = client.get(f"/dsp/profiles/{digest}").content
    assert hashlib.sha256(content).hexdigest() == digest
    profile = pb.DspProfile.FromString(content)
    assert profile.version == 1 and not profile.shared
    assert [(b.frequency_hz, b.gain_db) for b in profile.left] == [
        (53.25, -5.5),
        (90, 2.5),
    ]
    assert profile.right[0].gain_db == -2
    snapshot = {
        "config_version": "1",
        "sections": [{"fields": [{"path": "dsp.profile", "options": []}]}],
    }
    DspProfiles(tmp_path).offer(snapshot)
    assert (
        snapshot["sections"][0]["fields"][0]["options"]
        == client.get("/dsp/profiles").json()
    )
    assert snapshot["config_version"] != "1"
    assert client.get("/dsp/profiles/bad").status_code == 404


@pytest.mark.parametrize(
    "text",
    [
        EXPORT + "Filter 6: ON LP Fc 150 Hz",
        EXPORT + "Filter ?: ON PK Fc 150 Hz Gain 3 dB Q 1",
        EXPORT.replace("Generic", "DSP1124P"),
        EXPORT.replace("4.000", "nan"),
        EXPORT.replace("-5.50", "-31"),
        EXPORT.replace("4.000", "-1"),
        EXPORT.replace("53.25", "0"),
        "Filter 1: OFF None",
        "Filter 1 ON PK Fc 100 Hz Gain 2 dB Q 1",
        "\n".join([EXPORT] * 11),
        "not a filter file",
    ],
)
def test_refuse_invalid_profile_as_a_whole(text):
    with pytest.raises(ValueError):
        import_rew(RewImport(name="Room", shared=text))


def test_generic_fixed_shelf_and_peaks():
    profile = pb.DspProfile.FromString(
        import_rew(
            RewImport(
                name="Room", shared=EXPORT + "Filter 6: ON HS Fc 8000Hz Gain +3.0dB"
            )
        )
    )
    assert [b.type for b in profile.shared] == [
        pb.DspProfile.Band.PEAK,
        pb.DspProfile.Band.LOW_SHELF,
        pb.DspProfile.Band.HIGH_SHELF,
    ]
    assert 0.66 < profile.shared[-1].q < 0.68


def test_modal_filter_uses_rews_exported_q():
    profile = pb.DspProfile.FromString(
        import_rew(
            RewImport(
                name="Modal",
                shared="Filter 2: ON Modal Fc 100 Hz Gain -3.0 dB Q 5.41 T60 target 100 ms",
            )
        )
    )
    assert profile.shared[0].type == pb.DspProfile.Band.PEAK
    assert profile.shared[0].q == 5.41
