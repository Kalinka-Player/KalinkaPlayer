"""Build and time the Kalinka, FFmpeg and MPD conversion paths on one Linux CPU."""

import argparse
import csv
import io
import json
import os
import platform
import shlex
import statistics
import subprocess
import tarfile
from pathlib import Path

import numpy as np
from measure import measure
from reproduce import FIXTURE_SHA, RELEASE, check_hash, release_sources
from scipy.io import wavfile

HERE = Path(__file__).resolve().parent
MPD = "72077d76f11ffba21f6705fe912f47b0b5dc1d6e"
PATHS = ("kalinka-30k", "kalinka-wide", "ffmpeg", "mpd")


def build(output, repo, mpd_repo):
    work = output / "build"
    work.mkdir(exist_ok=True)
    if not mpd_repo:
        mpd_repo = work / "MPD"
        if not mpd_repo.exists():
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    "v0.24.4",
                    "https://github.com/MusicPlayerDaemon/MPD.git",
                    mpd_repo,
                ],
                check=True,
            )
    # Archive both revisions so local modifications cannot alter either decoder.
    mpd = work / "mpd-source"
    mpd.mkdir(exist_ok=True)
    with tarfile.open(
        fileobj=io.BytesIO(
            subprocess.check_output(["git", "-C", mpd_repo, "archive", MPD, "src"])
        )
    ) as archive:
        archive.extractall(mpd, filter="data")
    native = release_sources(repo, RELEASE, work) / "src/native_player"
    dsp = native / "dsp"
    kalinka_src = [
        dsp / (n + ".cpp")
        for n in (
            "dsd/DsdToPcm",
            "dsd/ByteHistory",
            "dsd/Stage1",
            "dsd/DsdKernels",
            "fir/HalfBandDecimator",
            "fir/SymmetricFir",
            "fir/FirKernels",
        )
    ]
    libraries = ["libhwy", "libavcodec", "libavutil", "libswresample", "samplerate"]

    def pkg(option):
        return shlex.split(
            subprocess.check_output(["pkg-config", option, *libraries], text=True)
        )

    binary = work / "decoder-cpu"
    subprocess.run(
        [
            *shlex.split(os.getenv("CXX", "g++")),
            "-O3",
            "-DNDEBUG",
            "-std=c++23",
            "-I",
            native,
            "-I",
            mpd / "src",
            *pkg("--cflags"),
            HERE / "decoder_cpu.cpp",
            *kalinka_src,
            mpd / "src/pcm/Dsd2Pcm.cxx",
            mpd / "src/util/BitReverse.cxx",
            *pkg("--libs"),
            "-o",
            binary,
        ],
        env={**os.environ, "TMPDIR": str(work)},
        check=True,
    )
    return binary


def analyse(output):
    with (output / "cpu-runs.csv").open() as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 48:
        raise ValueError("expected 12 rounds of four conversion paths")
    results = []
    for path in PATHS:
        selected = [row for row in rows if row["converter"] == path]
        if {int(row["round"]) for row in selected} != set(range(1, 13)):
            raise ValueError(f"incomplete rounds for {path}")
        values = [float(row["percent_one_core"]) for row in selected]
        for row, value in zip(selected, values):
            frames = int(row["frames"])
            if (
                frames != 4194304
                or abs(value - 100 * float(row["cpu_seconds"]) * 176400 / frames) > 1e-7
            ):
                raise ValueError("unexpected frame count or CPU calculation")
        stereo = np.fromfile(output / (path + ".f64"), dtype="<f8").reshape(-1, 2)
        if not np.array_equal(stereo[:, 0], stereo[:, 1]):
            raise ValueError(f"{path}: the two copies of the input channel differ")
        wav = output / (path + ".wav")
        wavfile.write(wav, 176400, stereo[:, 0])
        cutoff, level, thdn = measure(wav)
        results.append(
            [
                path,
                statistics.median(values),
                min(values),
                max(values),
                cutoff,
                level,
                thdn,
            ]
        )
    with (output / "comparison.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "converter",
                "median_percent_one_core",
                "min_percent",
                "max_percent",
                "minus3db_hz",
                "tone_dbfs",
                "thdn_db",
            ]
        )
        writer.writerows(results)
    print((output / "comparison.csv").read_text(), end="")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument(
        "--repo",
        type=Path,
        default=HERE,
        help="KalinkaPlayer checkout containing the release revision",
    )
    cli.add_argument(
        "--mpd-repo",
        type=Path,
        help="Existing MPD clone containing v0.24.4; otherwise fetched from upstream",
    )
    cli.add_argument(
        "--cpu",
        type=int,
        default=2,
        help="Logical core to pin the measurement to (default 2)",
    )
    cli.add_argument(
        "--analyse-only",
        action="store_true",
        help="Use previously captured cpu-runs.csv and the four stereo .f64 renders",
    )
    args = cli.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.analyse_only:
        fixture = HERE / "dsd64.dsf"
        check_hash(fixture, FIXTURE_SHA)
        repo = subprocess.check_output(
            ["git", "-C", args.repo, "rev-parse", "--show-toplevel"], text=True
        ).strip()
        binary = build(output, repo, args.mpd_repo)
        call = ["taskset", "-c", str(args.cpu), binary]

        def reading(path):
            p = Path(path)
            return p.read_text().strip().strip("\x00") if p.exists() else None

        def condition():
            base = f"/sys/devices/system/cpu/cpu{args.cpu}/cpufreq"
            return {
                "temp_millicelsius": reading("/sys/class/thermal/thermal_zone0/temp"),
                "governor": reading(base + "/scaling_governor"),
                "max_khz": reading(base + "/scaling_max_freq"),
            }

        env = {
            "cpu": args.cpu,
            "model": reading("/proc/device-tree/model"),
            "kernel": platform.release(),
            "release": RELEASE,
            "mpd": MPD,
            "dsf_sha256": FIXTURE_SHA,
            "before": condition(),
        }
        with (output / "cpu-runs.csv").open("wb") as result, fixture.open("rb") as src:
            status = subprocess.run(
                [*call, "time"],
                stdin=src,
                stdout=result,
                stderr=subprocess.PIPE,
                check=True,
            )
        env.update(after=condition(), libraries=status.stderr.decode().strip())
        (output / "environment.json").write_text(json.dumps(env, indent=2) + "\n")
        for path in PATHS:
            with (
                fixture.open("rb") as src,
                (output / (path + ".f64")).open("wb") as result,
            ):
                subprocess.run(
                    [*call, "render", path], stdin=src, stdout=result, check=True
                )
    analyse(output)


if __name__ == "__main__":
    main()
