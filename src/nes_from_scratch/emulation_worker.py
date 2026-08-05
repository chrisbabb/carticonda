"""Bounded pure-Python emulation pipeline.

The worker owns the mutable NES core while it is running.  Pygame and every
SDL object remain on the main thread; only plain Python buffers cross this
boundary.  A small ready queue lets inexpensive NES frames absorb the cost of
occasional expensive frames without changing the emulated clock rate.
"""

from __future__ import annotations

import queue
import threading
import time
from array import array
from dataclasses import dataclass

from .constants import PPU_HEIGHT, PPU_WIDTH
from .rewind import PreparedRewindSnapshot


FRAME_BYTES = PPU_WIDTH * PPU_HEIGHT * 3
DEFAULT_QUEUE_DEPTH = 2
# Rewind snapshots are convenience work and must not consume the first sliver
# of headroom after a slow frame. Require a short run with the ready queue full
# before compression resumes; a new producer tail resets the streak. Stable
# scenes still capture at the configured six-frame cadence, while a marginal
# machine dedicates its recovered lead to video and PCM first.
REWIND_HEADROOM_FRAMES = 12
REWIND_COMPRESSION_QUEUE_DEPTH = 2


@dataclass(slots=True)
class EmulatedFrame:
    """One completed NES frame and the PCM generated alongside it."""

    pixels: bytearray
    samples: array
    emulation_seconds: float
    emulation_cpu_seconds: float
    frame_number: int
    video_changed: bool = True


class EmulationWorkerError(RuntimeError):
    """Raised on the main thread when the emulation worker failed."""


class EmulationWorker:
    """Run one console into a bounded, allocation-free framebuffer queue."""

    def __init__(
        self,
        console,
        *,
        queue_depth: int = DEFAULT_QUEUE_DEPTH,
        rewind_buffer=None,
        measure_timing: bool = False,
    ) -> None:
        if queue_depth < 1:
            raise ValueError("queue_depth must be positive")
        self.console = console
        self.queue_depth = int(queue_depth)
        self.rewind_buffer = rewind_buffer
        self.rewind_failure: Exception | None = None
        self.rewind_deferrals = 0
        self._rewind_headroom_frames = 0
        self._rewind_compression_queue: queue.Queue[
            PreparedRewindSnapshot | None
        ] = queue.Queue(REWIND_COMPRESSION_QUEUE_DEPTH)
        self._rewind_compression_thread: threading.Thread | None = None
        self.rewind_compression_deferrals = 0
        self._ready: queue.Queue[EmulatedFrame] = queue.Queue(self.queue_depth)
        self._free: queue.Queue[bytearray] = queue.Queue(self.queue_depth)
        for _ in range(self.queue_depth):
            self._free.put_nowait(bytearray(FRAME_BYTES))
        self._stop = threading.Event()
        self._measure_timing = bool(measure_timing)
        self._inputs = (0, 0)
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None
        self.produced_frames = 0
        self.queue_high_water = 0

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def ready_frames(self) -> int:
        return self._ready.qsize()

    def set_inputs(self, player1: int, player2: int) -> None:
        # Tuple assignment is atomic under the GIL, so readers never observe a
        # half-written pair and the hot frame path avoids an extra lock hop.
        self._inputs = (int(player1) & 0xFF, int(player2) & 0xFF)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._failure = None
        if self.rewind_buffer is not None:
            self._rewind_compression_queue = queue.Queue(
                REWIND_COMPRESSION_QUEUE_DEPTH
            )
            self._rewind_compression_thread = threading.Thread(
                target=self._compress_rewind,
                name="CartacondaRewindCompressor",
                daemon=True,
            )
            self._rewind_compression_thread.start()
        self._thread = threading.Thread(
            target=self._run,
            name="CartacondaEmulation",
            daemon=True,
        )
        self._thread.start()

    def get(self, timeout: float = 0.0) -> EmulatedFrame | None:
        self.raise_if_failed()
        try:
            if timeout > 0:
                frame = self._ready.get(timeout=timeout)
            else:
                frame = self._ready.get_nowait()
        except queue.Empty:
            self.raise_if_failed()
            return None
        self.raise_if_failed()
        return frame

    def release(self, frame: EmulatedFrame) -> None:
        """Return a consumed framebuffer to the fixed-size worker pool."""
        try:
            self._free.put_nowait(frame.pixels)
        except queue.Full:
            # stop() may already have reclaimed the ready queue.  A duplicate
            # release must not turn orderly shutdown into a secondary error.
            pass

    def raise_if_failed(self) -> None:
        failure = self._failure
        if failure is not None:
            raise EmulationWorkerError(
                f"emulation worker failed: {failure}"
            ) from failure

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise EmulationWorkerError(
                    "emulation worker did not stop within the safety timeout"
                )
        self._thread = None
        compression_thread = self._rewind_compression_thread
        if compression_thread is not None:
            # The emulation owner is stopped, so no more prepared states can
            # enter the FIFO. A sentinel behind existing work drains every
            # accepted snapshot before pause/rewind code inspects the ring.
            self._rewind_compression_queue.put(None)
            compression_thread.join(timeout)
            if compression_thread.is_alive():
                raise EmulationWorkerError(
                    "rewind compressor did not stop within the safety timeout"
                )
        self._rewind_compression_thread = None
        while True:
            try:
                frame = self._ready.get_nowait()
            except queue.Empty:
                break
            self.release(frame)
        self.raise_if_failed()

    def _compress_rewind(self) -> None:
        while True:
            prepared = self._rewind_compression_queue.get()
            if prepared is None:
                return
            rewind = self.rewind_buffer
            if rewind is None:
                continue
            try:
                rewind.commit_prepared(prepared)
            except Exception as exc:
                # Rewind remains optional. Disable future capture while the
                # video/audio producer continues uninterrupted.
                self.rewind_failure = exc
                self.rewind_buffer = None

    def _get_free_buffer(self) -> bytearray | None:
        while not self._stop.is_set():
            try:
                return self._free.get(timeout=0.02)
            except queue.Empty:
                continue
        return None

    def _publish(self, frame: EmulatedFrame) -> bool:
        while not self._stop.is_set():
            try:
                self._ready.put(frame, timeout=0.02)
                ready = self._ready.qsize()
                if ready > self.queue_high_water:
                    self.queue_high_water = ready
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        frame_buffer: bytearray | None = None
        console = self.console
        ppu = console.ppu
        controller1 = console.controller1
        controller2 = console.controller2
        run_frame = console.run_frame
        drain_samples = console.apu.drain_samples
        perf_counter = time.perf_counter
        thread_time = time.thread_time
        measure_timing = self._measure_timing
        try:
            while not self._stop.is_set():
                frame_buffer = self._get_free_buffer()
                if frame_buffer is None:
                    break
                player1, player2 = self._inputs
                controller1.set_buttons(player1)
                controller2.set_buttons(player2)

                fast_mode = bool(getattr(ppu, "fast_mode", True))
                scanline_renders_before = (
                    int(getattr(ppu, "diagnostic_scanline_renders", 0))
                    if fast_mode
                    else 0
                )
                if measure_timing:
                    started = perf_counter()
                    cpu_started = thread_time()
                live_frame = run_frame(copy_frame=False)
                if measure_timing:
                    elapsed = perf_counter() - started
                    cpu_elapsed = thread_time() - cpu_started
                else:
                    elapsed = 0.0
                    cpu_elapsed = 0.0
                video_changed = (
                    int(
                        getattr(
                            ppu,
                            "diagnostic_scanline_renders",
                            scanline_renders_before,
                        )
                    )
                    != scanline_renders_before
                    if fast_mode
                    else True
                )
                if video_changed:
                    frame_buffer[:] = live_frame
                samples = drain_samples()
                packet = EmulatedFrame(
                    pixels=frame_buffer,
                    samples=samples,
                    emulation_seconds=elapsed,
                    emulation_cpu_seconds=cpu_elapsed,
                    frame_number=int(ppu.frame_number),
                    video_changed=video_changed,
                )
                if not self._publish(packet):
                    self._free.put_nowait(frame_buffer)
                    frame_buffer = None
                    break
                frame_buffer = None
                self.produced_frames += 1
                # Publish completed pixels and PCM before optional snapshot
                # compression. The console is still owned exclusively by this
                # worker and does not begin the next frame until capture ends,
                # while the host can immediately consume the frame that was
                # just completed instead of seeing a false queue starvation.
                rewind = self.rewind_buffer
                if rewind is not None:
                    if not self._ready.full():
                        # Rewind is convenience, not part of emulation timing.
                        # When the variance reservoir is depleted, produce the
                        # next frame first and postpone the snapshot. This
                        # keeps a temporarily expensive scene transition from
                        # compounding into video starvation and PCM underrun.
                        self._rewind_headroom_frames = 0
                        self.rewind_deferrals += 1
                    elif self._rewind_headroom_frames < REWIND_HEADROOM_FRAMES:
                        self._rewind_headroom_frames += 1
                        self.rewind_deferrals += 1
                    elif self._rewind_compression_queue.full():
                        self.rewind_compression_deferrals += 1
                        self.rewind_deferrals += 1
                    else:
                        try:
                            prepared = rewind.prepare_capture(self.console)
                            if prepared is not None:
                                self._rewind_compression_queue.put_nowait(
                                    prepared
                                )
                        except Exception as exc:
                            # A compression/allocation failure must not take
                            # down the core, audio stream, or SDL owner thread.
                            self.rewind_failure = exc
                            self.rewind_buffer = None
        except BaseException as exc:
            self._failure = exc
            self._stop.set()
            if frame_buffer is not None:
                try:
                    self._free.put_nowait(frame_buffer)
                except queue.Full:
                    pass
