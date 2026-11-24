"""Import REW Generic EQ text on the Core and serve immutable renderer profiles."""

import asyncio
import hashlib
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .fir_profiles import MAX_PROFILE_BYTES, read_profile
from .renderer_proto import renderer_pb2 as pb

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_FILTER = re.compile(r"Filter\s+\d+\s*:\s*(ON|OFF)\s+(.+)", re.I)
_BAND = re.compile(
    rf"(PK|Modal|LS|HS|LSQ|HSQ|LP|HP|LPQ|HPQ|LP1|HP1)\s+Fc\s*({_NUMBER})\s*Hz"
    rf"(?:\s+Gain\s*({_NUMBER})\s*dB)?(?:\s+Q\s*({_NUMBER}))?",
    re.I,
)
_TYPES = {
    "PK": pb.DspProfile.Band.PEAK,
    "MODAL": pb.DspProfile.Band.PEAK,
    "LS": pb.DspProfile.Band.LOW_SHELF,
    "HS": pb.DspProfile.Band.HIGH_SHELF,
    "LP": pb.DspProfile.Band.LOW_PASS,
    "HP": pb.DspProfile.Band.HIGH_PASS,
    "LP1": pb.DspProfile.Band.FIRST_ORDER_LOW_PASS,
    "HP1": pb.DspProfile.Band.FIRST_ORDER_HIGH_PASS,
}
_FIRST_ORDER = ("LP1", "HP1")
_PASS = ("LP", "HP", "LPQ", "HPQ") + _FIRST_ORDER
_PASS_TYPES = frozenset(_TYPES[kind.rstrip("Q")] for kind in _PASS)
_FIXED_Q = ("LS", "HS", "LP", "HP")
_BUTTERWORTH_Q = math.sqrt(0.5)


class RewImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    shared: str | None = Field(None, max_length=65536)
    left: str | None = Field(None, max_length=65536)
    right: str | None = Field(None, max_length=65536)


def _bands(text: str, keep_unity: bool = False):
    bands = []
    rows = 0
    for line_no, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if (
            line.lower().startswith(("equaliser:", "equalizer:"))
            and line.split(":", 1)[1].strip().lower() != "generic"
        ):
            raise ValueError(
                "Export filter settings as text with REW's Generic equaliser"
            )
        if not re.match(r"Filter\b", line, re.I) or line.lower().startswith(
            "filter settings"
        ):
            continue
        rows += 1
        row = _FILTER.fullmatch(line)
        if row and (row[1].upper() == "OFF" or row[2].upper() == "NONE"):
            continue
        spec = row[2] if row else ""
        if spec.upper().startswith("MODAL "):
            spec = re.sub(rf"\s+T60\s+target\s+{_NUMBER}\s*ms$", "", spec, flags=re.I)
        band = _BAND.fullmatch(spec)
        if not band:
            raise ValueError(f"Unsupported REW filter on line {line_no}")
        kind, frequency, gain, quality = band.groups()
        kind, frequency = kind.upper(), float(frequency)
        passing = kind in _PASS
        if passing != (gain is None):
            raise ValueError(f"Unsupported REW filter on line {line_no}")
        gain = 0.0 if passing else float(gain)
        if not 0 < frequency <= 384000 or not -30 <= gain <= 30:
            raise ValueError(f"Frequency or gain out of range on line {line_no}")
        if kind in _FIRST_ORDER:
            if quality is not None:
                raise ValueError(f"A first-order filter takes no Q on line {line_no}")
            q = 0.0
        elif kind in _FIXED_Q:
            if quality is not None:
                raise ValueError(f"Use {kind}Q for an adjustable Q on line {line_no}")
            if passing:
                q = _BUTTERWORTH_Q
            else:
                a = 10 ** (gain / 40)
                q = 1 / math.sqrt((a + 1 / a) * (1 / 0.9 - 1) + 2)
        elif quality is None or not 0.01 <= (q := float(quality)) <= 1000:
            raise ValueError(f"Invalid or missing Q on line {line_no}")
        if passing or gain or keep_unity:
            bands.append(
                pb.DspProfile.Band(
                    type=_TYPES[kind.rstrip("Q")],
                    frequency_hz=frequency,
                    gain_db=gain,
                    q=q,
                )
            )
        if len(bands) > 20:
            raise ValueError("At most 20 active bands per channel")
    if not rows:
        raise ValueError("Expected numbered REW filter rows")
    return bands


def import_rew(request: RewImport, *, keep_unity: bool = False) -> bytes:
    name = request.name.strip()
    if not name or not name.isprintable() or len(name.encode()) > 120:
        raise ValueError("Use a printable profile name of at most 120 UTF-8 bytes")
    profile = pb.DspProfile(version=1, name=name)
    if request.shared is not None and request.left is None and request.right is None:
        profile.shared.extend(_bands(request.shared, keep_unity))
    elif (
        request.shared is None
        and request.left is not None
        and request.right is not None
    ):
        profile.left.extend(_bands(request.left, keep_unity))
        profile.right.extend(_bands(request.right, keep_unity))
    else:
        raise ValueError("Supply one shared export or both left and right exports")
    if not any(
        b.gain_db or b.type in _PASS_TYPES
        for bands in (profile.shared, profile.left, profile.right)
        for b in bands
    ):
        raise ValueError("The profile has no active non-unity bands")
    return profile.SerializeToString(deterministic=True)


class DspProfiles:
    def __init__(self, directory: Path):
        self._db = directory / "dsp-profiles.db"
        self._edit_locks = {}

    def _connect(self):
        db = sqlite3.connect(self._db)
        db.execute(
            "CREATE TABLE IF NOT EXISTS profile (id TEXT PRIMARY KEY, name TEXT, data BLOB)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS working_eq "
            "(renderer TEXT, id TEXT, data BLOB, PRIMARY KEY(renderer, id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS fir_profile "
            "(id TEXT PRIMARY KEY, name TEXT, rates TEXT, data BLOB)"
        )
        return db

    def publish(self, request: RewImport) -> dict:
        data = import_rew(request)
        digest = hashlib.sha256(data).hexdigest()
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO profile VALUES (?, ?, ?)",
                (digest, request.name.strip(), data),
            )
        return {"id": digest, "name": request.name.strip()}

    def _store_edit(self, renderer_id, current, digest, data):
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            # Keep the audible curve available even if the new write is refused.
            db.execute(
                "DELETE FROM working_eq WHERE renderer=? AND id!=?",
                (renderer_id, current),
            )
            db.execute(
                "INSERT OR IGNORE INTO working_eq VALUES (?, ?, ?)",
                (renderer_id, digest, data),
            )

    async def edit(self, renderer_id, request, configs):
        data = import_rew(request, keep_unity=True)
        digest = hashlib.sha256(data).hexdigest()
        async with self._edit_locks.setdefault(renderer_id, asyncio.Lock()):
            snapshot = await configs.get(renderer_id)
            field = next(
                (
                    field
                    for section in snapshot["sections"]
                    for field in section["fields"]
                    if field["path"] == "dsp.profile"
                ),
                None,
            )
            if field is None or field["read_only"]:
                raise ValueError("This renderer does not support editing EQ")
            await asyncio.to_thread(
                self._store_edit, renderer_id, field["value"], digest, data
            )
            return await configs.update(renderer_id, {"dsp.profile": digest})

    def publish_fir(self, payload):
        name, rates, data = read_profile(payload)
        digest = hashlib.sha256(data).hexdigest()
        self._db.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO fir_profile VALUES (?,?,?,?)",
                (digest, name, rates, data),
            )
        return {"id": digest, "name": name, "rates": rates}

    def options(self, fir=False):
        if not self._db.exists():
            return []
        with closing(self._connect()) as db:
            if fir:
                return [
                    {"value": digest, "label": name, "description": rates}
                    for digest, name, rates in db.execute(
                        "SELECT id, name, rates FROM fir_profile ORDER BY name, id"
                    )
                ]
            return [
                {"value": digest, "label": name, "description": ""}
                for digest, name in db.execute(
                    "SELECT id, name FROM profile ORDER BY name, id"
                )
            ]

    def offer(self, snapshot: dict):
        for section in snapshot["sections"]:
            for field in section["fields"]:
                fir = field["path"] == "output.rew_fir_profile" and field.get(
                    "supports_fir_import"
                )
                if fir or field["path"] == "dsp.profile":
                    known = {o["value"] for o in field["options"]}
                    extra = [
                        o for o in self.options(fir=fir) if o["value"] not in known
                    ]
                    field["options"].extend(extra)
                    if extra:
                        ids = "".join(o["value"] for o in extra)
                        snapshot["config_version"] += (
                            "." + hashlib.sha256(ids.encode()).hexdigest()[:12]
                        )

    def router(self):
        router = APIRouter(prefix="/dsp/profiles")

        @router.post("/rew")
        def publish(request: RewImport):
            try:
                return self.publish(request)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        @router.post("/fir")
        async def publish_fir(request: Request):
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > MAX_PROFILE_BYTES:
                    raise HTTPException(413, "A filter profile must fit in 32 MiB")
                data.extend(chunk)
            try:
                return await asyncio.to_thread(self.publish_fir, data)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        @router.get("")
        def profiles():
            return self.options()

        @router.get("/{digest}")
        def download(digest: str):
            if not self._db.exists():
                raise HTTPException(404, "Unknown DSP profile")
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT data FROM profile WHERE id=? UNION ALL "
                    "SELECT data FROM working_eq WHERE id=? UNION ALL "
                    "SELECT data FROM fir_profile WHERE id=? LIMIT 1",
                    (digest, digest, digest),
                ).fetchone()
            if row is None:
                raise HTTPException(404, "Unknown DSP profile")
            return Response(row[0], media_type="application/x-protobuf")

        return router
