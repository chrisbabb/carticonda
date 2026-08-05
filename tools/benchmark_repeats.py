#!/usr/bin/env python3
"""Run isolated repeated benchmark suites with median/p95/variance output."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModeSample:
    mode: str
    frames: int
    seconds: float
    fps: float
    realtime_factor: float


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _run_benchmark_once(repo_root: Path, python_exe: str, args: argparse.Namespace) -> dict[str, ModeSample]:
    command = [
        python_exe,
        "tools/benchmark.py",
        "--json",
        "--frames",
        str(args.frames),
        "--control-frames",
        str(args.control_frames),
        "--accurate-frames",
        str(args.accurate_frames),
        "--synchronized-frames",
        str(args.synchronized_frames),
        "--dmc-frames",
        str(args.dmc_frames),
        "--gameplay-frames",
        str(args.gameplay_frames),
        "--mmc2-frames",
        str(args.mmc2_frames),
        "--mmc3-frames",
        str(args.mmc3_frames),
        "--rewind-frames",
        str(args.rewind_frames),
        "--mmc2-blank-frames",
        str(args.mmc2_blank_frames),
        "--mmc3-irq-frames",
        str(args.mmc3_irq_frames),
        "--mmc3-churn-frames",
        str(args.mmc3_churn_frames),
        "--mmc3-sprite-frames",
        str(args.mmc3_sprite_frames),
        "--mmc3-slot-frames",
        str(args.mmc3_slot_frames),
        "--mmc3-field-frames",
        str(args.mmc3_field_frames),
        "--mmc3-prg-churn-frames",
        str(args.mmc3_prg_churn_frames),
    ]
    completed = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": "src"},
    )
    rows = json.loads(completed.stdout)
    return {
        row["mode"]: ModeSample(
            mode=row["mode"],
            frames=int(row["frames"]),
            seconds=float(row["seconds"]),
            fps=float(row["fps"]),
            realtime_factor=float(row["realtime_factor"]),
        )
        for row in rows
    }


def _print_table(aggregate: dict[str, dict[str, float]]) -> None:
    headers = (
        "mode",
        "median_fps",
        "p95_frame_ms",
        "cv_pct",
        "runs",
    )
    print(
        f"{headers[0]:24} {headers[1]:>11} {headers[2]:>13} {headers[3]:>8} {headers[4]:>4}"
    )
    print("-" * 70)
    for mode in sorted(aggregate):
        row = aggregate[mode]
        print(
            f"{mode:24} {row['median_fps']:11.2f} "
            f"{row['p95_frame_ms']:13.3f} {row['cv_pct']:8.2f} "
            f"{int(row['runs']):4d}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--cooldown-ms", type=int, default=500)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--control-frames", type=int, default=40)
    parser.add_argument("--accurate-frames", type=int, default=4)
    parser.add_argument("--synchronized-frames", type=int, default=40)
    parser.add_argument("--dmc-frames", type=int, default=30)
    parser.add_argument("--gameplay-frames", type=int, default=120)
    parser.add_argument("--mmc2-frames", type=int, default=120)
    parser.add_argument("--mmc3-frames", type=int, default=120)
    parser.add_argument("--rewind-frames", type=int, default=120)
    parser.add_argument("--mmc2-blank-frames", type=int, default=0)
    parser.add_argument("--mmc3-irq-frames", type=int, default=0)
    parser.add_argument("--mmc3-churn-frames", type=int, default=0)
    parser.add_argument("--mmc3-sprite-frames", type=int, default=0)
    parser.add_argument("--mmc3-slot-frames", type=int, default=0)
    parser.add_argument("--mmc3-field-frames", type=int, default=0)
    parser.add_argument("--mmc3-prg-churn-frames", type=int, default=0)
    args = parser.parse_args()

    if args.repeats < 1:
        raise SystemExit("--repeats must be >= 1")
    if args.warmup_runs < 0:
        raise SystemExit("--warmup-runs must be >= 0")

    repo_root = Path(__file__).resolve().parents[1]

    for _ in range(args.warmup_runs):
        _run_benchmark_once(repo_root, args.python, args)
        if args.cooldown_ms > 0:
            time.sleep(args.cooldown_ms / 1000.0)

    runs: list[dict[str, ModeSample]] = []
    for run_index in range(args.repeats):
        runs.append(_run_benchmark_once(repo_root, args.python, args))
        if args.cooldown_ms > 0 and run_index + 1 < args.repeats:
            time.sleep(args.cooldown_ms / 1000.0)

    mode_names = sorted(runs[0].keys())
    aggregate: dict[str, dict[str, float]] = {}
    for mode in mode_names:
        fps_values = [run[mode].fps for run in runs]
        frame_ms_values = [
            (run[mode].seconds / run[mode].frames) * 1000.0 for run in runs
        ]
        mean_fps = statistics.fmean(fps_values)
        stdev_fps = statistics.pstdev(fps_values) if len(fps_values) > 1 else 0.0
        cv_pct = (stdev_fps / mean_fps * 100.0) if mean_fps > 0 else 0.0
        aggregate[mode] = {
            "runs": float(len(fps_values)),
            "median_fps": statistics.median(fps_values),
            "p95_frame_ms": _percentile(frame_ms_values, 0.95),
            "mean_fps": mean_fps,
            "min_fps": min(fps_values),
            "max_fps": max(fps_values),
            "cv_pct": cv_pct,
        }

    _print_table(aggregate)

    if args.output_json is not None:
        payload = {
            "repeats": args.repeats,
            "warmup_runs": args.warmup_runs,
            "cooldown_ms": args.cooldown_ms,
            "python": args.python,
            "modes": aggregate,
        }
        args.output_json.write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
        print(f"wrote summary: {args.output_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
