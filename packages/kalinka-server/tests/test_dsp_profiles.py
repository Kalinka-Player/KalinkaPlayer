import hashlib
import io
import struct
import wave

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kalinka_server.dsp_profiles import DspProfiles, RewImport, import_rew
from kalinka_server.fir_profiles import impulse_info
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
        EXPORT + "Filter 6: ON AP Fc 150 Hz Q 0.7",
        EXPORT + "Filter 6: ON NO Fc 150 Hz",
        EXPORT + "Filter 6: ON LP1 Fc 150 Hz Q 0.7",
        EXPORT + "Filter 6: ON HP1 Fc 150 Hz Gain 3 dB",
        EXPORT + "Filter 6: ON LP Fc 150 Hz Q 2",
        EXPORT + "Filter 6: ON HPQ Fc 150 Hz",
        EXPORT + "Filter 6: ON HPQ Fc 150 Hz Q 0",
        EXPORT + "Filter 6: ON HP Fc 150 Hz Gain 3 dB",
        EXPORT + "Filter 6: ON PK Fc 150 Hz Q 2",
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


@pytest.mark.asyncio
async def test_current_edits_survive_reload_and_stay_out_of_saved_presets(tmp_path):
    from unittest.mock import AsyncMock

    store = DspProfiles(tmp_path)
    saved = store.publish(RewImport(name="Original", shared=EXPORT))["id"]
    configs = AsyncMock()
    app = FastAPI()
    app.include_router(store.router())
    client = TestClient(app)

    async def edit(current, frequency, renderer="room", accepted=True):
        configs.get.return_value = {
            "sections": [
                {
                    "fields": [
                        {"path": "dsp.profile", "read_only": False, "value": current}
                    ]
                }
            ]
        }
        configs.update.return_value = {
            "outcomes": [{"path": "dsp.profile", "applied": accepted}]
        }
        result = await store.edit(
            renderer,
            RewImport(
                name="Current EQ", shared=EXPORT.replace("53.25", str(frequency))
            ),
            configs,
        )
        assert result["outcomes"][0]["applied"] == accepted
        output, changes = configs.update.await_args.args
        assert output == renderer
        assert set(changes) == {"dsp.profile"}
        return changes["dsp.profile"]

    first = await edit(saved, 55)
    second = await edit(first, 60, accepted=False)
    other = await edit(saved, 80, renderer="kitchen")
    third = await edit(first, 75)
    for digest in (first, third, other, saved):
        assert client.get(f"/dsp/profiles/{digest}").status_code == 200
    assert client.get(f"/dsp/profiles/{second}").status_code == 404
    assert [p["value"] for p in DspProfiles(tmp_path).options()] == [saved]
    active = pb.DspProfile.FromString(client.get(f"/dsp/profiles/{third}").content)
    assert [b.frequency_hz for b in active.shared] == [75, 90, 100]


def test_fir_import_groups_rates_and_rejects_invalid_variants(tmp_path):
    store = DspProfiles(tmp_path)
    app = FastAPI()
    app.include_router(store.router())
    client = TestClient(app)

    def impulse(rate):
        file = io.BytesIO()
        with wave.open(file, "wb") as wav:
            wav.setparams((1, 2, rate, 0, "NONE", ""))
            wav.writeframes(b"\xff\x7f")
        return file.getvalue()

    lo, hi = impulse(44100), impulse(192000)

    def upload(*wavs):
        data = pb.FirProfile(
            name="Listening room", impulse_wav=wavs
        ).SerializeToString()
        return client.post("/dsp/profiles/fir", content=data)

    result = upload(hi, lo)
    assert result.status_code == 200, result.text
    digest = result.json()["id"]
    assert upload(lo, hi).json()["id"] == digest
    data = client.get(f"/dsp/profiles/{digest}").content
    assert hashlib.sha256(data).hexdigest() == digest
    assert pb.FirProfile.FromString(data).impulse_wav == [lo, hi]
    assert store.options(fir=True) == [
        {"value": digest, "label": "Listening room", "description": "44.1, 192 kHz"}
    ]
    for files in [(hi, hi), (hi[:-1],), (b"abc",), ()]:
        assert upload(*files).status_code == 422
    assert store.options() == []


@pytest.mark.parametrize("extensible", [False, True])
def test_float_impulse_accepts_real_coefficients_and_refuses_nonfinite(extensible):
    def wav(value):
        fmt = struct.pack(
            "<HHIIHH", 0xFFFE if extensible else 3, 2, 192000, 192000 * 8, 8, 32
        )
        if extensible:
            fmt += struct.pack("<HHI", 22, 32, 3) + bytes.fromhex(
                "0300000000001000800000aa00389b71"
            )
        body = (
            b"WAVEfmt "
            + struct.pack("<I", len(fmt))
            + fmt
            + b"data"
            + struct.pack("<Iff", 8, 1.25, value)
        )
        return b"RIFF" + struct.pack("<I", len(body)) + body

    assert impulse_info(wav(-0.5)) == (192000, 2, 1)
    with pytest.raises(ValueError, match="non-finite"):
        impulse_info(wav(float("nan")))


def test_pass_filters_are_butterworth_unless_the_row_gives_a_q():
    profile = pb.DspProfile.FromString(
        import_rew(
            RewImport(
                name="Subsonic",
                shared="""Filter  1: ON  HP       Fc     30 Hz
Filter  2: ON  LPQ      Fc   10000 Hz  Q  0.400
Filter  3: ON  HPQ      Fc    20.0 Hz  Q  0.500
Filter  4: ON  LP       Fc    8000 Hz
""",
            )
        )
    )
    assert [(b.type, b.frequency_hz, b.gain_db) for b in profile.shared] == [
        (pb.DspProfile.Band.HIGH_PASS, 30, 0),
        (pb.DspProfile.Band.LOW_PASS, 10000, 0),
        (pb.DspProfile.Band.HIGH_PASS, 20, 0),
        (pb.DspProfile.Band.LOW_PASS, 8000, 0),
    ]
    assert [b.q for b in profile.shared] == pytest.approx(
        [0.5**0.5, 0.4, 0.5, 0.5**0.5], abs=1e-15
    )


def test_first_order_passes_carry_a_frequency_and_nothing_else():
    profile = pb.DspProfile.FromString(
        import_rew(
            RewImport(
                name="Third order",
                shared="""Filter  1: ON  HP1      Fc     80 Hz
Filter  2: ON  HPQ      Fc     80 Hz  Q  1.000
Filter  3: ON  LP1      Fc  12000 Hz
""",
            )
        )
    )
    assert [(b.type, b.frequency_hz, b.gain_db, b.q) for b in profile.shared] == [
        (pb.DspProfile.Band.FIRST_ORDER_HIGH_PASS, 80, 0, 0),
        (pb.DspProfile.Band.HIGH_PASS, 80, 0, 1),
        (pb.DspProfile.Band.FIRST_ORDER_LOW_PASS, 12000, 0, 0),
    ]
