"""Build and time the Kalinka, FFmpeg, MPD and Squeezelite conversion paths on one Linux CPU."""

import argparse
import csv
import io
import json
import math
import os
import platform
import shlex
import statistics
import subprocess
import tarfile
from pathlib import Path

import numpy as np
from figures import pi_response
from measure import measure
from reproduce import FIXTURE_SHA, RELEASE, check_hash, release_sources
from scipy.io import wavfile

HERE = Path(__file__).resolve().parent
MPD = "72077d76f11ffba21f6705fe912f47b0b5dc1d6e"
SQUEEZELITE = "262994a989dc29ce3be4390c57c6a43373dfdca2"
PATHS = ("kalinka-30k", "kalinka-wide", "ffmpeg", "mpd", "squeezelite")


def upstream(work, checkout, project, revision, directory):
    if checkout is None:
        checkout = work / project.split("/")[-1]
        if not (checkout / ".git").is_dir():
            subprocess.run(["git", "init", "-q", checkout], check=True)
        exists = subprocess.run(
            ["git", "-C", checkout, "cat-file", "-e", revision + "^{commit}"],
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if exists.returncode:
            subprocess.run(
                [
                    "git",
                    "-C",
                    checkout,
                    "fetch",
                    "--depth=1",
                    f"https://github.com/{project}.git",
                    revision,
                ],
                check=True,
            )
    tree = work / revision[:10]
    snapshot = subprocess.check_output(
        ["git", "-C", checkout, "archive", revision, directory]
    )
    with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
        archive.extractall(tree, filter="data")
    return tree / directory


def build(output, repo, mpd_repo, squeezelite_repo):
    work = output / "build"
    work.mkdir(exist_ok=True)
    mpd = upstream(work, mpd_repo, "MusicPlayerDaemon/MPD", MPD, "src")
    squeeze = upstream(
        work, squeezelite_repo, "ralph-irving/squeezelite", SQUEEZELITE, "dsd2pcm"
    )
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
    libraries = [
        "libhwy",
        "libavcodec",
        "libavutil",
        "libswresample",
        "samplerate",
        "soxr",
    ]

    def pkg(option):
        return shlex.split(
            subprocess.check_output(["pkg-config", option, *libraries], text=True)
        )

    build_env = {**os.environ, "TMPDIR": str(work)}
    dsd2pcm = work / "dsd2pcm.o"
    subprocess.run(
        [
            *shlex.split(os.getenv("CC", "cc")),
            "-O3",
            "-DNDEBUG",
            "-std=c11",
            "-c",
            squeeze / "dsd2pcm.c",
            "-o",
            dsd2pcm,
        ],
        env=build_env,
        check=True,
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
            mpd,
            "-I",
            squeeze,
            *pkg("--cflags"),
            HERE / "decoder_cpu.cpp",
            *kalinka_src,
            mpd / "pcm/Dsd2Pcm.cxx",
            mpd / "util/BitReverse.cxx",
            dsd2pcm,
            *pkg("--libs"),
            "-o",
            binary,
        ],
        env=build_env,
        check=True,
    )
    return binary


def read_timings(csv_path):
    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    if len(rows) != 50:
        raise ValueError("expected 10 rounds of five conversion paths")
    timings = {}
    for path in PATHS:
        selected = [row for row in rows if row["converter"] == path]
        if {int(row["round"]) for row in selected} != set(range(1, 11)):
            raise ValueError(f"incomplete rounds for {path}")
        values = [float(row["percent_one_core"]) for row in selected]
        for row, value in zip(selected, values):
            frames = int(row["frames"])
            cpu = float(row["cpu_seconds"])
            wall = float(row["wall_seconds"])
            if (
                abs(frames - 4194304) > (2304 if path == "squeezelite" else 0)
                or not all(math.isfinite(x) and x > 0 for x in (cpu, wall, value))
                or not math.isclose(
                    value, 100 * cpu * 176400 / frames, rel_tol=0, abs_tol=1e-7
                )
            ):
                raise ValueError(f"{path} round {row['round']}: invalid CPU timing")
        timings[path] = values
    return timings


def analyse(output):
    timings = read_timings(output / "cpu-runs.csv")
    # At the native rate, compare every first-channel float before packing.
    native = np.fromfile(output / "native.f32", dtype="<f4").reshape(-1, 3)
    bits = int.from_bytes((HERE / "dsd64.dsf").read_bytes()[64:72], "little")
    if len(native) != bits // 8 or not np.isfinite(native).all():
        raise ValueError("incomplete native-rate capture")
    with (output / "native-comparison.csv").open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "reference",
                "converter",
                "rate_hz",
                "frames",
                "different_float32_words",
                "max_abs_sample_difference",
            ]
        )
        for i, other in enumerate(("ffmpeg", "mpd"), 1):
            error = native[:, i].astype(np.float64) - native[:, 0]
            different = np.count_nonzero(
                native[:, i].view("<u4") != native[:, 0].view("<u4")
            )
            writer.writerow(
                [
                    "squeezelite",
                    other,
                    352800,
                    len(native),
                    different,
                    np.max(abs(error)),
                ]
            )
    results = []
    for path, values in timings.items():
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
        "--squeezelite-repo",
        type=Path,
        help="Existing Squeezelite clone containing the pinned 2.0.0-1517 revision",
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
        help="Use previously captured cpu-runs.csv and the native.f32 and five stereo .f64 captures",
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
        binary = build(output, repo, args.mpd_repo, args.squeezelite_repo)
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
            "squeezelite": SQUEEZELITE,
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
        with fixture.open("rb") as src, (output / "native.f32").open("wb") as result:
            subprocess.run([*call, "native"], stdin=src, stdout=result, check=True)
        for path in PATHS:
            with (
                fixture.open("rb") as src,
                (output / (path + ".f64")).open("wb") as result,
            ):
                subprocess.run(
                    [*call, "render", path], stdin=src, stdout=result, check=True
                )
    analyse(output)
    pi_response(output)


if __name__ == "__main__":
    main()
