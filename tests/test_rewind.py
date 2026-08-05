from __future__ import annotations

import time
import threading
import unittest

from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.console import Console
from nes_from_scratch.emulation_worker import EmulationWorker
from nes_from_scratch.rewind import RewindBuffer
from nes_from_scratch.state import StateError, dumps
from support import make_ines


class RewindTests(unittest.TestCase):
    @staticmethod
    def console(opcode: int = 0x4C) -> Console:
        program = (
            bytes((0x4C, 0x00, 0x80))
            if opcode == 0x4C
            else bytes((opcode, 0x4C, 0x01, 0x80))
        )
        return Console(
            Cartridge.from_bytes(make_ines(program=program)),
            sample_rate=0,
            fast_ppu=True,
        )

    def test_bounded_history_restores_an_earlier_complete_frame(self) -> None:
        console = self.console()
        rewind = RewindBuffer(
            seconds=0.05,
            capture_interval_frames=1,
        )
        console.bus.ram[0] = 0
        rewind.capture(console, force=True)
        values_by_frame = {int(console.ppu.frame_number): 0}

        for value in range(1, 8):
            console.run_frame(copy_frame=False)
            console.bus.ram[0] = value
            values_by_frame[int(console.ppu.frame_number)] = value
            rewind.capture(console)

        self.assertLessEqual(len(rewind), rewind.capacity)
        current_frame = int(console.ppu.frame_number)
        result = rewind.restore_steps(console, 2)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.from_frame, current_frame)
        self.assertLess(result.to_frame, result.from_frame)
        self.assertEqual(
            console.bus.ram[0],
            values_by_frame[result.to_frame],
        )
        self.assertEqual(len(console.apu.samples), 0)

    def test_capture_interval_and_clear(self) -> None:
        console = self.console()
        rewind = RewindBuffer(
            seconds=2,
            capture_interval_frames=3,
        )
        rewind.capture(console, force=True)
        for _ in range(2):
            console.run_frame(copy_frame=False)
            self.assertFalse(rewind.capture(console))
        console.run_frame(copy_frame=False)
        self.assertTrue(rewind.capture(console))
        self.assertGreater(rewind.memory_bytes, 0)
        rewind.clear()
        self.assertEqual(len(rewind), 0)
        self.assertEqual(rewind.memory_bytes, 0)

    def test_rom_identity_is_enforced_on_restore(self) -> None:
        first = self.console(0xEA)
        second = self.console(0x78)
        rewind = RewindBuffer(capture_interval_frames=1)
        rewind.capture(first, force=True)
        first.run_frame(copy_frame=False)
        rewind.capture(first)
        second.run_frame(copy_frame=False)
        with self.assertRaises(StateError):
            rewind.restore_steps(second)

    def test_worker_captures_without_crossing_the_sdl_boundary(self) -> None:
        commit_threads: list[str] = []

        class RecordingRewindBuffer(RewindBuffer):
            def commit_prepared(self, prepared) -> None:
                commit_threads.append(threading.current_thread().name)
                super().commit_prepared(prepared)

        console = self.console()
        rewind = RecordingRewindBuffer(
            seconds=1,
            capture_interval_frames=1,
        )
        rewind.capture(console, force=True)
        commit_threads.clear()
        worker = EmulationWorker(
            console,
            queue_depth=2,
            rewind_buffer=rewind,
        )
        worker.start()
        deadline = time.monotonic() + 2
        packet = None
        while packet is None and time.monotonic() < deadline:
            packet = worker.get(timeout=0.05)
        self.assertIsNotNone(packet)
        if packet is not None:
            worker.release(packet)
        while len(rewind) < 2 and time.monotonic() < deadline:
            # Rewind is intentionally admitted only after the producer has
            # rebuilt and held its complete variance reservoir. Pace this
            # synthetic consumer accordingly instead of draining both slots
            # as fast as Python can poll them.
            while (
                worker.ready_frames < worker.queue_depth
                and time.monotonic() < deadline
            ):
                time.sleep(0.001)
            packet = worker.get(timeout=0.05)
            if packet is not None:
                worker.release(packet)
        worker.stop()
        self.assertGreaterEqual(len(rewind), 2)
        self.assertIsNone(worker.rewind_failure)
        self.assertIn("CartacondaRewindCompressor", commit_threads)

    def test_state_compression_level_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            dumps({}, compression_level=10)


if __name__ == "__main__":
    unittest.main()
