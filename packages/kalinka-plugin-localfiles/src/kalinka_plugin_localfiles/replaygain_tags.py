"""Read track and album ReplayGain from ID3, Vorbis and MP4 tag mappings."""

import math


def read_replaygain(tags: dict) -> dict[str, float]:
    values = {key.lower().removeprefix("txxx:"): value for key, value in tags.items()}
    result = {}
    for scope in ("track", "album"):
        for kind in ("gain", "peak"):
            raw = values.get(f"replaygain_{scope}_{kind}")
            if isinstance(raw, (list, tuple)):
                raw = raw[0] if raw else None
            try:
                value = float(str(raw).strip().lower().removesuffix("db").strip())
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and (kind != "peak" or value > 0):
                field = (
                    f"replaygain_{kind}"
                    if scope == "track"
                    else f"replaygain_album_{kind}"
                )
                result[field] = value
    return result
