"""Bounded compressed rewind history for deterministic console state."""

from __future__ import annotations

import math
import pickle
import threading
import zlib
from collections import deque
from dataclasses import dataclass

from .constants import NTSC_FRAME_RATE
DEFAULT_REWIND_SECONDS = 30.0
DEFAULT_CAPTURE_INTERVAL = 6
REWIND_COMPRESSION_LEVEL = 1


@dataclass(frozen=True, slots=True)
class RewindSnapshot:
    frame_number: int
    compressed_state: bytes


@dataclass(frozen=True, slots=True)
class PreparedRewindSnapshot:
    """Immutable owner-thread state awaiting background compression."""

    frame_number: int
    encoded_state: bytes


@dataclass(frozen=True, slots=True)
class RewindResult:
    from_frame: int
    to_frame: int
    snapshots: int

    @property
    def frames(self) -> int:
        return max(0, self.from_frame - self.to_frame)

    @property
    def seconds(self) -> float:
        return self.frames / NTSC_FRAME_RATE


class RewindBuffer:
    """Keep a fixed-duration history without retaining mutable core objects.

    Captures are made by the emulation owner thread after a complete frame.
    Restore is performed only while that worker is stopped. Every entry uses
    the same ROM-identified primitive state tree as an ordinary save state.
    The ring is never accepted from disk or another process, so Python's fast
    binary protocol is safe here and avoids the Base85/JSON cost required for
    portable, untrusted save-state files.
    """

    def __init__(
        self,
        *,
        seconds: float = DEFAULT_REWIND_SECONDS,
        capture_interval_frames: int = DEFAULT_CAPTURE_INTERVAL,
        frame_rate: float = NTSC_FRAME_RATE,
    ) -> None:
        if seconds <= 0:
            raise ValueError("rewind duration must be positive")
        if capture_interval_frames <= 0:
            raise ValueError("capture interval must be positive")
        if frame_rate <= 0:
            raise ValueError("frame rate must be positive")
        self.seconds = float(seconds)
        self.capture_interval_frames = int(capture_interval_frames)
        self.frame_rate = float(frame_rate)
        self.capacity = max(
            2,
            math.ceil(
                self.seconds
                * self.frame_rate
                / self.capture_interval_frames
            )
            + 1,
        )
        self._snapshots: deque[RewindSnapshot] = deque(maxlen=self.capacity)
        self._snapshot_lock = threading.Lock()
        self._frames_since_capture = 0
        self._last_prepared_frame: int | None = None
        self.captures = 0
        self.restores = 0

    def __len__(self) -> int:
        with self._snapshot_lock:
            return len(self._snapshots)

    @property
    def memory_bytes(self) -> int:
        with self._snapshot_lock:
            return sum(
                len(item.compressed_state) for item in self._snapshots
            )

    @property
    def available_seconds(self) -> float:
        with self._snapshot_lock:
            if len(self._snapshots) < 2:
                return 0.0
            return max(
                0.0,
                (
                    self._snapshots[-1].frame_number
                    - self._snapshots[0].frame_number
                )
                / self.frame_rate,
            )

    def clear(self) -> None:
        with self._snapshot_lock:
            self._snapshots.clear()
        self._frames_since_capture = 0
        self._last_prepared_frame = None

    def prepare_capture(
        self,
        console,
        *,
        force: bool = False,
    ) -> PreparedRewindSnapshot | None:
        """Freeze primitive state on the owner thread without compressing it."""
        if not force:
            self._frames_since_capture += 1
            if self._frames_since_capture < self.capture_interval_frames:
                return None
        self._frames_since_capture = 0
        frame_number = int(console.ppu.frame_number)
        with self._snapshot_lock:
            committed_frame = (
                self._snapshots[-1].frame_number
                if self._snapshots
                else None
            )
        if (
            committed_frame == frame_number
            or self._last_prepared_frame == frame_number
        ):
            return None
        # get_state() and pickle must stay on the console owner thread. The
        # resulting byte string is immutable and can safely cross to the
        # compressor without exposing mutable CPU/PPU/APU objects.
        encoded = pickle.dumps(
            console.get_state(include_framebuffer=False),
            protocol=5,
        )
        self._last_prepared_frame = frame_number
        return PreparedRewindSnapshot(frame_number, encoded)

    def commit_prepared(self, prepared: PreparedRewindSnapshot) -> None:
        """Compress and publish one immutable prepared snapshot."""
        encoded = zlib.compress(
            prepared.encoded_state,
            level=REWIND_COMPRESSION_LEVEL,
        )
        with self._snapshot_lock:
            self._snapshots.append(
                RewindSnapshot(prepared.frame_number, encoded)
            )
            self.captures += 1

    def capture(self, console, *, force: bool = False) -> bool:
        prepared = self.prepare_capture(console, force=force)
        if prepared is None:
            return False
        self.commit_prepared(prepared)
        return True

    def restore_steps(self, console, steps: int = 1) -> RewindResult | None:
        requested = max(1, int(steps))
        from_frame = int(console.ppu.frame_number)
        current_frame = from_frame
        selected: RewindSnapshot | None = None
        consumed = 0

        # A forced snapshot can describe the state currently on screen. It is
        # a useful anchor but not a rewind destination.
        with self._snapshot_lock:
            while (
                self._snapshots
                and self._snapshots[-1].frame_number >= current_frame
            ):
                self._snapshots.pop()

            while consumed < requested and self._snapshots:
                selected = self._snapshots.pop()
                current_frame = selected.frame_number
                consumed += 1

        if selected is None:
            return None
        console.set_state(
            pickle.loads(zlib.decompress(selected.compressed_state))
        )
        # Host audio queued after the restored frame must never leak onto the
        # new timeline.
        console.apu.drain_samples()
        self._frames_since_capture = 0
        self._last_prepared_frame = int(console.ppu.frame_number)
        self.restores += 1
        return RewindResult(
            from_frame=from_frame,
            to_frame=int(console.ppu.frame_number),
            snapshots=consumed,
        )

    def restore_seconds(
        self,
        console,
        seconds: float = 1.0,
    ) -> RewindResult | None:
        steps = max(
            1,
            math.ceil(
                max(0.0, float(seconds))
                * self.frame_rate
                / self.capture_interval_frames
            ),
        )
        return self.restore_steps(console, steps)
