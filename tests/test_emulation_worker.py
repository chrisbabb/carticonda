from __future__ import annotations

import time
import unittest
from array import array

from nes_from_scratch.emulation_worker import (
    FRAME_BYTES,
    EmulationWorker,
    EmulationWorkerError,
)


class _Controller:
    def __init__(self) -> None:
        self.buttons = 0

    def set_buttons(self, buttons: int) -> None:
        self.buttons = buttons


class _Apu:
    def __init__(self) -> None:
        self.samples = array("h")

    def drain_samples(self) -> array:
        samples = self.samples
        self.samples = array("h")
        return samples


class _Ppu:
    def __init__(self) -> None:
        self.frame_number = 0
        self.framebuffer = bytearray(FRAME_BYTES)
        self.diagnostic_scanline_renders = 0


class _Console:
    def __init__(
        self,
        *,
        fail: bool = False,
        video_changes: bool = True,
    ) -> None:
        self.controller1 = _Controller()
        self.controller2 = _Controller()
        self.apu = _Apu()
        self.ppu = _Ppu()
        self.fail = fail
        self.video_changes = video_changes

    def run_frame(self, *, copy_frame: bool = True):
        if self.fail:
            raise ValueError("synthetic core failure")
        self.ppu.frame_number += 1
        marker = (
            self.controller1.buttons
            ^ (self.controller2.buttons << 1)
            ^ self.ppu.frame_number
        ) & 0xFF
        self.ppu.framebuffer[0] = marker
        if self.video_changes:
            self.ppu.diagnostic_scanline_renders += 1
        self.apu.samples.extend((marker, -marker))
        return (
            bytes(self.ppu.framebuffer)
            if copy_frame
            else self.ppu.framebuffer
        )


class EmulationWorkerTests(unittest.TestCase):
    def test_worker_transfers_input_pixels_audio_and_timing(self) -> None:
        console = _Console()
        worker = EmulationWorker(console, queue_depth=2)
        worker.set_inputs(0x12, 0x34)
        worker.start()
        try:
            packet = worker.get(timeout=1.0)
            self.assertIsNotNone(packet)
            assert packet is not None
            expected = (0x12 ^ (0x34 << 1) ^ 1) & 0xFF
            self.assertEqual(packet.pixels[0], expected)
            self.assertEqual(packet.samples.tolist(), [expected, -expected])
            self.assertEqual(packet.frame_number, 1)
            self.assertTrue(packet.video_changed)
            self.assertGreaterEqual(packet.emulation_seconds, 0.0)
            self.assertGreaterEqual(packet.emulation_cpu_seconds, 0.0)
            worker.release(packet)
        finally:
            worker.stop()

    def test_audio_only_frame_skips_framebuffer_transfer(self) -> None:
        console = _Console(video_changes=False)
        worker = EmulationWorker(console, queue_depth=1)
        worker.start()
        try:
            packet = worker.get(timeout=1.0)
            self.assertIsNotNone(packet)
            assert packet is not None
            self.assertFalse(packet.video_changed)
            self.assertEqual(packet.pixels[0], 0)
            self.assertNotEqual(console.ppu.framebuffer[0], 0)
            self.assertTrue(packet.samples)
            worker.release(packet)
        finally:
            worker.stop()

    def test_queue_bounds_how_far_the_core_can_run_ahead(self) -> None:
        console = _Console()
        worker = EmulationWorker(console, queue_depth=2)
        worker.start()
        try:
            deadline = time.monotonic() + 1.0
            while worker.queue_high_water < 2 and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(worker.queue_high_water, 2)
            # With exactly two pooled framebuffers, the core cannot begin a
            # third frame until the main thread returns one.
            time.sleep(0.01)
            self.assertEqual(console.ppu.frame_number, 2)
        finally:
            worker.stop()

    def test_framebuffer_is_reused_after_main_thread_release(self) -> None:
        worker = EmulationWorker(_Console(), queue_depth=1)
        worker.start()
        try:
            first = worker.get(timeout=1.0)
            self.assertIsNotNone(first)
            assert first is not None
            buffer_identity = id(first.pixels)
            worker.release(first)
            second = worker.get(timeout=1.0)
            self.assertIsNotNone(second)
            assert second is not None
            self.assertEqual(id(second.pixels), buffer_identity)
            worker.release(second)
        finally:
            worker.stop()

    def test_worker_failure_is_re_raised_on_the_main_thread(self) -> None:
        worker = EmulationWorker(_Console(fail=True))
        worker.start()
        deadline = time.monotonic() + 1.0
        while worker.running and time.monotonic() < deadline:
            time.sleep(0.001)
        with self.assertRaisesRegex(
            EmulationWorkerError,
            "synthetic core failure",
        ):
            worker.get()
        with self.assertRaises(EmulationWorkerError):
            worker.stop()


if __name__ == "__main__":
    unittest.main()
