"""Rebuild the Kalinka and FFmpeg measurements for the DSD-to-PCM article."""

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import shlex
import statistics
import subprocess
import sys
import tarfile
from pathlib import Path

import matplotlib
import numpy as np
import scipy
from scipy.io import wavfile

HERE = Path(__file__).resolve().parent
RELEASE = "a043989071d8bbe391ba9ae3372dbedf91a8a705"
TIMING = "57f1ae92ce45c0e5da90ca5a5ece9b26ae1b4447"
RATE = 176400
PRESETS = ("30k", "50k", "wide")
FIXTURE_SHA = "e21d0b464515f7a1a3b73ad3051900896e88a783d079cdc4c83e6d43cff1a5f0"
SONG_SHA = "b28e53003e8d8aaad8eb7ddb027a9aa051c963840a076b8330094e7e9a7881bb"
SARACON_SHA = "940dbd1fa692664f0d42231202bea2b04271d16afc9372a1f8f7a5545ed7b008"
FFMPEG_FILTER = "aresample=176400:resampler=swr:osf=dbl:dither_method=none"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_hash(path, expected):
    actual = sha256(path)
    if actual != expected:
        raise ValueError(f"{path}: expected {expected}, got {actual}")


def release_sources(repo, commit, build):
    prefix = "packages/kalinka-renderer"
    archive = subprocess.check_output(["git", "-C", repo, "archive", commit, prefix])
    destination = build / commit[:10]
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    return destination / prefix


def ffmpeg(source, target, *, resample, music=False):
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", source]
    if resample:
        cmd.extend(["-af", FFMPEG_FILTER])
    if music:
        cmd.extend(["-t", "53", "-f", "f64le"])
    subprocess.run([*cmd, "-c:a", "pcm_f64le", target], check=True)


def compile_tool(cxx, flags, sources, libs, target):
    subprocess.run(
        [
            *cxx,
            *flags,
            *sources,
            *shlex.split(os.getenv("LDFLAGS", "")),
            *libs,
            "-o",
            target,
        ],
        check=True,
    )


def build_release(build, release, cxx, flags):
    native, tests = release / "src/native_player", release / "tests"
    flags = [*flags, "-I", native, "-I", tests, "-I", tests / "dsp"]
    objects = []
    for name in (
        "dsd/DsdToPcm",
        "dsd/Stage1",
        "dsd/ByteHistory",
        "dsd/DsdKernels",
        "fir/HalfBandDecimator",
        "fir/SymmetricFir",
        "fir/FirKernels",
    ):
        obj = build / (Path(name).stem + ".o")
        subprocess.run(
            [*cxx, *flags, "-c", native / "dsp" / (name + ".cpp"), "-o", obj],
            check=True,
        )
        objects.append(obj)
    compile_tool(
        cxx, flags, [HERE / "render.cpp", *objects], ["-lhwy"], build / "render"
    )
    harness = [
        tests / "dsp" / (n + ".cpp") for n in ("Signals", "SigmaDelta", "Analysis")
    ]
    compile_tool(
        cxx,
        flags,
        [HERE / "release_probe.cpp", *objects, *harness],
        ["-lhwy", "-lgtest", "-pthread"],
        build / "release-probe",
    )
    return flags, harness


def time_converter(repo, runs, build, output, cxx, flags):
    release = release_sources(repo, TIMING, build)
    native, bench = release / "src/native_player", release / "bench"
    units = [
        native / (n + ".cpp")
        for n in ("DsdToPcm", "DsdKernels", "FirFilters", "FirKernels")
    ]
    binary = build / "historical-timing"
    compile_tool(
        cxx,
        [*flags, "-O3", "-DNDEBUG", "-I", native, "-I", bench],
        [HERE / "timing.cpp", bench / "DsdToPcm.cpp", *units],
        ["-lhwy"],
        binary,
    )
    rows = {}
    for i in range(runs):
        log = subprocess.check_output([binary], text=True)
        (output / f"timing-run-{i + 1}.md").write_text(log)
        for line in log.splitlines():
            if not line.startswith("| DSD"):
                continue
            conversion, preset, _, _, pairs, _ = map(
                str.strip, line.strip("|").split("|")
            )
            percent = float(re.search(r"\(([\d.]+) %\)", pairs)[1])
            rows.setdefault((conversion, preset), []).append(percent)
    with (output / "timing.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["conversion", "filter", "median_channel_pair_percent_of_one_core", "runs"]
        )
        for key, values in rows.items():
            writer.writerow([*key, statistics.median(values), len(values)])


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument(
        "--repo",
        type=Path,
        default=HERE,
        help="Any directory inside a full KalinkaPlayer Git checkout",
    )
    cli.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Directory for builds, renders, CSVs and plots",
    )
    cli.add_argument(
        "--music-dsf", type=Path, help="BitPerfect's original Looking For A Home DSF"
    )
    cli.add_argument(
        "--saracon-aiff", type=Path, help="Optional archived Saracon render of that DSF"
    )
    cli.add_argument(
        "--timing-runs",
        type=int,
        default=0,
        help="Also run the historical stereo CPU benchmark N times",
    )
    cli.add_argument(
        "--regenerate-fixture",
        action="store_true",
        help="Write this CPU's DSD generator output for inspection",
    )
    args = cli.parse_args()
    if args.timing_runs < 0 or (args.saracon_aiff and not args.music_dsf):
        cli.error(
            "Use a nonnegative timing count and supply --music-dsf for a Saracon comparison."
        )
    output = args.output.resolve()
    build = output / "build"
    build.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(build)
    os.environ["MPLCONFIGDIR"] = str(build / "matplotlib")
    cxx = shlex.split(os.getenv("CXX", "g++"))
    flags = ["-std=c++23", "-O2", *shlex.split(os.getenv("CPPFLAGS", ""))]
    fixture = HERE / "dsd64.dsf"
    check_hash(fixture, FIXTURE_SHA)
    if args.music_dsf:
        check_hash(args.music_dsf, SONG_SHA)
    if args.saracon_aiff:
        check_hash(args.saracon_aiff, SARACON_SHA)
    repo = subprocess.check_output(
        ["git", "-C", args.repo, "rev-parse", "--show-toplevel"], text=True
    ).strip()
    release = release_sources(repo, RELEASE, build)
    release_flags, harness = build_release(build, release, cxx, flags)
    if args.regenerate_fixture:
        compile_tool(
            cxx,
            release_flags,
            [HERE / "make_fixture.cpp", *harness[:2]],
            [],
            build / "make-fixture",
        )
        subprocess.run(
            [build / "make-fixture", output / "locally-generated.dsf"], check=True
        )
    subprocess.run([build / "release-probe"], cwd=output, check=True)
    wavs = []
    for preset in PRESETS:
        raw = build / (preset + ".f64")
        subprocess.run([build / "render", fixture, preset, raw], check=True)
        wav = output / (preset + ".wav")
        wavfile.write(wav, RATE, np.fromfile(raw, dtype="<f8"))
        wavs.append(wav)
    wav = output / "ffmpeg.wav"
    ffmpeg(fixture, wav, resample=True)
    with (output / "fixture-results.csv").open("w") as f:
        subprocess.run(
            [sys.executable, HERE / "measure.py", *wavs, wav], stdout=f, check=True
        )
    plot_args = []
    if args.music_dsf:
        kal, other = build / "music-kal.f64", build / "music-ffmpeg.f64"
        subprocess.run([build / "render", args.music_dsf, "30k", kal], check=True)
        ffmpeg(args.music_dsf, other, resample=True, music=True)
        recordings = [kal]
        if args.saracon_aiff:
            historical = build / "music-saracon.f64"
            ffmpeg(args.saracon_aiff, historical, resample=False, music=True)
            recordings.append(historical)
        with (output / "music-alignment.txt").open("w") as f:
            subprocess.run(
                [sys.executable, HERE / "music_measure.py", *recordings, other],
                cwd=output,
                stdout=f,
                check=True,
            )
        plot_args.append("--music")
    subprocess.run(
        [sys.executable, HERE / "figures.py", output, *plot_args], check=True
    )
    if args.timing_runs:
        time_converter(repo, args.timing_runs, build, output, cxx, flags)
    sources = [fixture, *filter(None, (args.music_dsf, args.saracon_aiff))]
    info = subprocess.check_output(
        ["lscpu", "--json"], text=True, env={**os.environ, "LC_ALL": "C"}
    )
    cpu = {
        item["field"].rstrip(":"): item["data"] for item in json.loads(info)["lscpu"]
    }
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    manifest = {
        "renderer_commit": RELEASE,
        "timing_commit": TIMING if args.timing_runs else None,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "compiler": subprocess.check_output(
            [*cxx, "--version"], text=True
        ).splitlines()[0],
        "compiler_flags": flags,
        "linker_flags": os.getenv("LDFLAGS", ""),
        "cpu_model": cpu.get("Model name", platform.machine()),
        "governor": governor.read_text().strip() if governor.exists() else None,
        "matplotlib": matplotlib.__version__,
        "ffmpeg_version": subprocess.check_output(["ffmpeg", "-version"], text=True),
        "ffmpeg_filter": FFMPEG_FILTER,
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "input_sha256": {p.name: sha256(p) for p in sources},
        "tone_output_sha256": {p.name: sha256(p) for p in [*wavs, wav]},
    }
    (output / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print((output / "fixture-results.csv").read_text(), end="")
    print(f"Results and source versions: {output}")


if __name__ == "__main__":
    main()
