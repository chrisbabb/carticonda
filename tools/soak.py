#!/usr/bin/env python3
"""Exercise Cartaconda's real threaded frame/audio reservoir at NTSC speed."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.console import Console
from nes_from_scratch.constants import DEFAULT_SAMPLE_RATE, NTSC_FRAME_RATE
from nes_from_scratch.emulation_worker import EmulationWorker
from nes_from_scratch.rewind import RewindBuffer

if __package__:
    from .benchmark import (
        build_gameplay_stress_rom,
        build_mmc2_gameplay_stress_rom,
        build_mmc3_gameplay_stress_rom,
        build_mmc3_cross_slot_scroll_rom,
        build_mmc3_irq_stress_rom,
        configure_mmc3_field_tail,
        configure_mmc2_gameplay_stress,
        update_mmc3_field_tail,
    )
else:
    from benchmark import (
        build_gameplay_stress_rom,
        build_mmc2_gameplay_stress_rom,
        build_mmc3_gameplay_stress_rom,
        build_mmc3_cross_slot_scroll_rom,
        build_mmc3_irq_stress_rom,
        configure_mmc3_field_tail,
        configure_mmc2_gameplay_stress,
        update_mmc3_field_tail,
    )


@dataclass(frozen=True, slots=True)
class SoakResult:
    workload: str
    frames: int
    wall_seconds: float
    emulation_average_ms: float
    emulation_p95_ms: float
    emulation_p99_ms: float
    emulation_max_ms: float
    emulation_cpu_average_ms: float
    emulation_cpu_p99_ms: float
    emulation_cpu_max_ms: float
    slowest_frame_number: int
    frame_budget_ms: float
    budget_misses: int
    queue_depth: int
    queue_high_water: int
    queue_starvations: int
    audio_underruns: int
    audio_min_reservoir_samples: int
    rewind_captures: int
    rewind_deferrals: int
    rewind_compression_deferrals: int
    rewind_memory_bytes: int
    rewind_failure: str | None
    clean_shutdown: bool

    @property
    def passed(self) -> bool:
        return (
            self.queue_starvations == 0
            and self.audio_underruns == 0
            and self.rewind_failure is None
            and self.clean_shutdown
        )


def _percentile_seconds(samples: list[float], quantile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(
        len(ordered) - 1,
        max(0, int((len(ordered) - 1) * quantile)),
    )
    return ordered[index]


def _make_console(workload: str) -> Console:
    if workload == "nrom":
        rom = build_gameplay_stress_rom()
    elif workload == "mmc2":
        rom = build_mmc2_gameplay_stress_rom()
    elif workload == "mmc3":
        rom = build_mmc3_gameplay_stress_rom()
    elif workload == "mmc3-irq":
        rom = build_mmc3_irq_stress_rom()
    elif workload == "mmc3-slot":
        rom = build_mmc3_cross_slot_scroll_rom()
    elif workload == "mmc3-field":
        rom = build_mmc3_cross_slot_scroll_rom()
    else:
        raise ValueError(f"unknown soak workload: {workload}")
    console = Console(
        Cartridge.from_bytes(rom, title=f"soak-{workload}"),
        sample_rate=DEFAULT_SAMPLE_RATE,
        fast_ppu=True,
    )
    console.ppu.mask = 0x18
    if workload in {"mmc2", "mmc3", "mmc3-slot"}:
        configure_mmc2_gameplay_stress(console)
    elif workload == "mmc3-field":
        configure_mmc3_field_tail(console)
        original_run_frame = console.run_frame
        field_frame = 0

        def run_field_frame(*, copy_frame: bool = True):
            nonlocal field_frame
            update_mmc3_field_tail(console, field_frame)
            field_frame += 1
            return original_run_frame(copy_frame=copy_frame)

        console.run_frame = run_field_frame
    return console


def run_soak(
    workload: str,
    *,
    frames: int,
    queue_depth: int,
    rewind_enabled: bool,
    switch_interval: float,
) -> SoakResult:
    console = _make_console(workload)
    rewind = RewindBuffer() if rewind_enabled else None
    if rewind is not None:
        rewind.capture(console, force=True)
    worker = EmulationWorker(
        console,
        queue_depth=queue_depth,
        rewind_buffer=rewind,
    )

    frame_period = 1.0 / NTSC_FRAME_RATE
    frame_budget_ms = frame_period * 1000.0
    emulation_times: list[float] = []
    emulation_cpu_times: list[float] = []
    slowest_frame_number = 0
    slowest_frame_seconds = 0.0
    starvations = 0
    audio_underruns = 0
    reservoir = 0
    minimum_reservoir = 1 << 30
    audio_started = False
    # Match the frontend's two 2,048-sample startup chunks.
    prebuffer_samples = 4096
    consumption_phase = 0.0
    clean_shutdown = False
    host_frame = bytearray(len(console.ppu.framebuffer))
    old_switch_interval = sys.getswitchinterval()
    gc_was_enabled = gc.isenabled()

    try:
        sys.setswitchinterval(switch_interval)
        if gc_was_enabled:
            gc.disable()
        worker.start()

        # Begin the measured segment with the same full two-frame variance
        # reservoir used after the frontend's cold-start grace period.
        warm_deadline = time.monotonic() + 5.0
        while (
            worker.ready_frames < queue_depth
            and time.monotonic() < warm_deadline
        ):
            worker.raise_if_failed()
            time.sleep(0.001)
        if worker.ready_frames < queue_depth:
            raise RuntimeError("emulation queue did not warm before the soak")

        started = time.monotonic()
        deadline = started + frame_period
        for _ in range(frames):
            packet = worker.get()
            # The real frontend accepts the next queued frame at the beginning
            # of the presentation interval, but will wait until the measured
            # scale lead point if a producer tail emptied the queue.
            draw_deadline = deadline - 0.003
            while packet is None and time.monotonic() < draw_deadline:
                packet = worker.get(
                    timeout=min(
                        0.002,
                        max(0.0, draw_deadline - time.monotonic()),
                    )
                )
            if packet is None:
                starvations += 1
            else:
                emulation_times.append(packet.emulation_seconds)
                emulation_cpu_times.append(packet.emulation_cpu_seconds)
                if packet.emulation_seconds > slowest_frame_seconds:
                    slowest_frame_seconds = packet.emulation_seconds
                    slowest_frame_number = packet.frame_number
                host_frame[:] = packet.pixels
                reservoir += len(packet.samples)
                worker.release(packet)

            # Match the frontend's two-millisecond audio-service wakeups and
            # bounded sub-millisecond deadline tail. These wakeups are what
            # make the interpreter switch interval relevant to the worker.
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if remaining > 0.00035:
                    time.sleep(min(0.002, remaining - 0.00035))
                else:
                    # Mirror the frontend's micro-sleep tail to avoid a full
                    # busy-spin while preserving sub-millisecond pacing.
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        if remaining > 0.00008:
                            time.sleep(0.00008)
                    break

            if not audio_started and reservoir >= prebuffer_samples:
                audio_started = True
            if audio_started:
                consumption_phase += DEFAULT_SAMPLE_RATE / NTSC_FRAME_RATE
                consume = int(consumption_phase)
                consumption_phase -= consume
                if reservoir < consume:
                    audio_underruns += 1
                    audio_started = False
                    reservoir = 0
                    consumption_phase = 0.0
                else:
                    reservoir -= consume
                    minimum_reservoir = min(minimum_reservoir, reservoir)
            deadline += frame_period
        wall_seconds = time.monotonic() - started
    finally:
        try:
            worker.stop()
            clean_shutdown = True
        finally:
            sys.setswitchinterval(old_switch_interval)
            if gc_was_enabled:
                gc.enable()

    p95 = _percentile_seconds(emulation_times, 0.95)
    p99 = _percentile_seconds(emulation_times, 0.99)
    cpu_p99 = _percentile_seconds(emulation_cpu_times, 0.99)
    average = (
        sum(emulation_times) / len(emulation_times)
        if emulation_times
        else 0.0
    )
    return SoakResult(
        workload=workload,
        frames=frames,
        wall_seconds=wall_seconds,
        emulation_average_ms=average * 1000.0,
        emulation_p95_ms=p95 * 1000.0,
        emulation_p99_ms=p99 * 1000.0,
        emulation_max_ms=(
            max(emulation_times, default=0.0) * 1000.0
        ),
        emulation_cpu_average_ms=(
            (
                sum(emulation_cpu_times) / len(emulation_cpu_times)
                if emulation_cpu_times
                else 0.0
            )
            * 1000.0
        ),
        emulation_cpu_p99_ms=cpu_p99 * 1000.0,
        emulation_cpu_max_ms=(
            max(emulation_cpu_times, default=0.0) * 1000.0
        ),
        slowest_frame_number=slowest_frame_number,
        frame_budget_ms=frame_budget_ms,
        budget_misses=sum(
            sample > frame_period for sample in emulation_times
        ),
        queue_depth=queue_depth,
        queue_high_water=worker.queue_high_water,
        queue_starvations=starvations,
        audio_underruns=audio_underruns,
        audio_min_reservoir_samples=(
            0 if minimum_reservoir == 1 << 30 else minimum_reservoir
        ),
        rewind_captures=0 if rewind is None else rewind.captures,
        rewind_deferrals=worker.rewind_deferrals,
        rewind_compression_deferrals=(
            worker.rewind_compression_deferrals
        ),
        rewind_memory_bytes=0 if rewind is None else rewind.memory_bytes,
        rewind_failure=(
            None
            if worker.rewind_failure is None
            else str(worker.rewind_failure)
        ),
        clean_shutdown=clean_shutdown,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the bounded worker, exact-NTSC consumer, virtual PCM "
            "reservoir, and rewind capture path for release validation."
        )
    )
    parser.add_argument(
        "--workload",
        choices=(
            "nrom", "mmc2", "mmc3", "mmc3-irq", "mmc3-slot", "mmc3-field",
            "both", "all",
        ),
        default="both",
    )
    parser.add_argument("--frames", type=int, default=900)
    parser.add_argument("--queue-depth", type=int, default=2)
    parser.add_argument(
        "--switch-ms",
        type=float,
        default=5.0,
        help="temporary Python worker thread-switch interval",
    )
    parser.add_argument(
        "--no-rewind",
        action="store_true",
        help="disable the 2.0 rewind capture path",
    )
    parser.add_argument("--json", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return a failure status after any starvation or underrun",
    )
    args = parser.parse_args()
    if args.frames <= 0:
        parser.error("--frames must be positive")
    if args.queue_depth <= 0:
        parser.error("--queue-depth must be positive")
    if args.switch_ms <= 0:
        parser.error("--switch-ms must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.workload == "both":
        workloads = ("nrom", "mmc2")
    elif args.workload == "all":
        workloads = (
            "nrom", "mmc2", "mmc3", "mmc3-irq", "mmc3-slot", "mmc3-field",
        )
    else:
        workloads = (args.workload,)
    results = [
        run_soak(
            workload,
            frames=args.frames,
            queue_depth=args.queue_depth,
            rewind_enabled=not args.no_rewind,
            switch_interval=args.switch_ms / 1000.0,
        )
        for workload in workloads
    ]
    payload = [
        {**asdict(result), "passed": result.passed}
        for result in results
    ]
    rendered = json.dumps(payload, indent=2)
    print(rendered)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(rendered + "\n", encoding="utf-8")
    if args.strict and not all(result.passed for result in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
