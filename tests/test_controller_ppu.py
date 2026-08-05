from __future__ import annotations

import unittest
from unittest.mock import patch

from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.constants import Button
from nes_from_scratch.controller import Controller
from nes_from_scratch.ppu import PPU
from support import make_ines


class ControllerTests(unittest.TestCase):
    def test_serial_order_and_high_bits_after_eight_reads(self) -> None:
        controller = Controller()
        controller.set_buttons(int(Button.A | Button.START | Button.LEFT))
        controller.write_strobe(1)
        controller.write_strobe(0)
        bits = [controller.read() for _ in range(10)]
        self.assertEqual(bits[:8], [1, 0, 0, 1, 0, 0, 1, 0])
        self.assertEqual(bits[8:], [1, 1])

    def test_strobe_reports_live_a_button(self) -> None:
        controller = Controller()
        controller.write_strobe(1)
        controller.set_button(Button.A, True)
        self.assertEqual(controller.read(), 1)
        controller.set_button(Button.A, False)
        self.assertEqual(controller.read(), 0)


class PPUTests(unittest.TestCase):
    def test_world_row_cache_evicts_stale_lru_without_bulk_reset(self) -> None:
        ppu = PPU(
            Cartridge.from_bytes(make_ines(chr_banks=1)),
            fast_mode=True,
        )
        ppu.mask = 0x0A
        ppu._fast_world_cache_active = True

        def render_world_row(index: int) -> None:
            start_v = ((index & 7) << 12) | ((index >> 3) << 5)
            ppu._render_background_scanline_fast(start_v, 0, 256 * 3)

        with patch(
            "nes_from_scratch.ppu.FAST_BACKGROUND_WORLD_CACHE_LIMIT",
            4,
        ):
            for index in range(4):
                render_world_row(index)
            original_keys = tuple(ppu._fast_background_world_cache)
            render_world_row(0)
            render_world_row(4)

        self.assertEqual(len(ppu._fast_background_world_cache), 4)
        self.assertIn(original_keys[0], ppu._fast_background_world_cache)
        self.assertNotIn(original_keys[1], ppu._fast_background_world_cache)
        self.assertEqual(ppu.diagnostic_world_cache_evictions, 1)
        self.assertEqual(ppu.diagnostic_world_cache_misses, 5)

    def test_fast_sprite_zero_probe_matches_full_scanline_hit(self) -> None:
        cart = Cartridge.from_bytes(make_ines(chr_banks=1))
        ppu = PPU(cart, fast_mode=True)
        ppu.mask = 0x1E
        ppu.scanline = 1
        ppu.cycle = 0
        ppu.v = 0
        # Solid tile 1 supplies both the background at X=16 and sprite zero.
        cart.chr_memory[16] = 0xFF
        ppu.nametable_ram[2] = 1
        ppu.oam[0:4] = bytes((0, 1, 0, 16))

        probed = ppu._probe_fast_sprite_zero_hit_cycle()
        rendered = ppu._render_scanline_fast(1)

        self.assertEqual(probed, 17)
        self.assertEqual(rendered, probed)
        ppu.scanline = 20
        self.assertEqual(ppu._fast_sprite_zero_candidate_cycle(), 0)

    def test_sprite_palette_write_preserves_background_only_lines(self) -> None:
        def make_ppu() -> PPU:
            ppu = PPU(
                Cartridge.from_bytes(make_ines(chr_banks=0)),
                fast_mode=True,
            )
            ppu.mask = 0x1E
            ppu.palette_ram[0x00] = 0x0F
            ppu.palette_ram[0x01] = 0x30
            ppu.palette_ram[0x11] = 0x21
            ppu.cartridge.chr_memory[16:24] = bytes((0xFF,)) * 8
            ppu.nametable_ram[:] = bytes((1,)) * len(ppu.nametable_ram)
            ppu.oam[:] = bytes((0xFF, 0, 0, 0)) * 64
            ppu.oam[0:4] = bytes((0, 1, 0, 16))
            ppu.invalidate_fast_sprite_cache()
            return ppu

        optimized = make_ppu()
        reference = make_ppu()
        for ppu in (optimized, reference):
            ppu._render_scanline_fast(20)
            ppu._render_scanline_fast(1)

        replay_count = optimized.diagnostic_scanline_replays
        optimized.write_memory(0x3F11, 0x16)
        reference.write_memory(0x3F11, 0x16)
        reference.invalidate_fast_cache(
            preserve_mapper_viewports=True,
            replay_content_tracked=True,
        )

        optimized._render_scanline_fast(20)
        self.assertEqual(
            optimized.diagnostic_scanline_replays,
            replay_count + 1,
        )
        self.assertEqual(
            optimized.diagnostic_palette_background_preserves,
            1,
        )

        optimized._render_scanline_fast(1)
        reference._render_scanline_fast(20)
        reference._render_scanline_fast(1)
        self.assertEqual(optimized.framebuffer, reference.framebuffer)

        # A background palette write still invalidates every line that may
        # use that entry.
        replay_count = optimized.diagnostic_scanline_replays
        optimized.write_memory(0x3F01, 0x12)
        optimized._render_scanline_fast(20)
        self.assertEqual(
            optimized.diagnostic_scanline_replays,
            replay_count,
        )

    def ppu(self, vertical: bool = False) -> PPU:
        return PPU(Cartridge.from_bytes(make_ines(vertical=vertical, chr_banks=0)))

    def test_horizontal_and_vertical_nametable_wiring(self) -> None:
        horizontal = self.ppu(False)
        horizontal.write_memory(0x2000, 0x55)
        self.assertEqual(horizontal.read_memory(0x2400), 0x55)
        self.assertNotEqual(horizontal.read_memory(0x2800), 0x55)

        vertical = self.ppu(True)
        vertical.write_memory(0x2000, 0x66)
        self.assertEqual(vertical.read_memory(0x2800), 0x66)
        self.assertNotEqual(vertical.read_memory(0x2400), 0x66)

    def test_palette_universal_color_aliases(self) -> None:
        ppu = self.ppu()
        ppu.write_memory(0x3F00, 0x2A)
        self.assertEqual(ppu.read_memory(0x3F10), 0x2A)
        ppu.write_memory(0x3F14, 0x11)
        self.assertEqual(ppu.read_memory(0x3F04), 0x11)

    def test_ppudata_buffering_and_increment(self) -> None:
        ppu = self.ppu()
        ppu.write_memory(0x2000, 0x42)
        ppu.v = 0x2000
        self.assertEqual(ppu.cpu_read_register(0x2007), 0)
        self.assertEqual(ppu.v, 0x2001)
        ppu.v = 0x2000
        self.assertEqual(ppu.cpu_read_register(0x2007), 0x42)

    def test_status_read_clears_vblank_and_address_latch(self) -> None:
        ppu = self.ppu()
        ppu.status = 0x80
        ppu.write_toggle = True
        value = ppu.cpu_read_register(0x2002)
        self.assertTrue(value & 0x80)
        self.assertFalse(ppu.status & 0x80)
        self.assertFalse(ppu.write_toggle)

    def test_vblank_raises_nmi(self) -> None:
        ppu = self.ppu()
        ppu.cpu_write_register(0x2000, 0x80)
        for _ in range(100_000):
            ppu.step()
            if ppu.nmi_pending:
                break
        self.assertTrue(ppu.poll_nmi())
        self.assertTrue(ppu.status & 0x80)

    def test_status_read_suppresses_vblank_in_both_steppers(self) -> None:
        for fast_mode, race_cycle in ((False, 1), (True, 0)):
            with self.subTest(fast_mode=fast_mode):
                ppu = PPU(
                    Cartridge.from_bytes(make_ines(chr_banks=0)),
                    fast_mode=fast_mode,
                )
                ppu.ctrl = 0x80
                ppu.scanline = 241
                ppu.cycle = race_cycle
                self.assertEqual(ppu.cpu_read_register(0x2002) & 0x80, 0)
                if fast_mode:
                    ppu.step_fast(1)
                else:
                    ppu.step_accurate(1)
                self.assertFalse(ppu.status & 0x80)
                self.assertFalse(ppu.nmi_pending)

    def test_fast_forced_blank_keeps_vblank_edge_transitions(self) -> None:
        ppu = PPU(
            Cartridge.from_bytes(make_ines(chr_banks=0)),
            fast_mode=True,
        )
        ppu.ctrl = 0x80
        ppu.mask = 0
        ppu.scanline = 241
        ppu.cycle = 0

        ppu.step_fast(1)

        self.assertTrue(ppu.status & 0x80)
        self.assertTrue(ppu.nmi_line)
        self.assertTrue(ppu.nmi_pending)

        ppu.scanline = 261
        ppu.cycle = 0
        ppu.step_fast(1)

        self.assertFalse(ppu.status & 0x80)
        self.assertFalse(ppu.nmi_line)

    def test_sprite_overflow_cycle_models_secondary_oam_timing(self) -> None:
        ppu = self.ppu()
        ppu.oam[:] = bytes((0xF8,)) * 0x100
        for index in range(9):
            ppu.oam[index * 4] = 0

        self.assertEqual(ppu._sprite_overflow_cycle(0), 130)
        self.assertEqual(ppu._sprite_overflow_cycle(239), 0)

        for index in range(9):
            ppu.oam[index * 4] = 239
        self.assertEqual(ppu._sprite_overflow_cycle(239), 130)
        self.assertEqual(ppu._sprite_overflow_cycle(238), 0)

    def test_sprite_overflow_cycle_models_diagonal_oam_bug(self) -> None:
        ppu = self.ppu()
        ppu.oam[:] = bytes((0xF8,)) * 0x100
        for index in range(8):
            ppu.oam[index * 4] = 128
        # Sprite 9 is out of range. The evaluator then treats sprite 10's
        # tile byte as a Y coordinate on the following comparison.
        ppu.oam[9 * 4 + 1] = 128

        self.assertEqual(ppu._sprite_overflow_cycle(128), 132)

    def test_overflow_transition_is_dot_timed_in_both_steppers(self) -> None:
        for fast_mode in (False, True):
            with self.subTest(fast_mode=fast_mode):
                ppu = PPU(
                    Cartridge.from_bytes(make_ines(chr_banks=0)),
                    fast_mode=fast_mode,
                )
                ppu.oam[:] = bytes((0xF8,)) * 0x100
                for index in range(9):
                    ppu.oam[index * 4] = 0
                ppu.mask = 0x08
                ppu.scanline = 0
                ppu.cycle = 128
                if fast_mode:
                    ppu.step_fast(1)
                else:
                    # The dot path establishes its evaluation plan at dot 65.
                    ppu.cycle = 64
                    ppu.step_accurate(66)
                self.assertFalse(ppu.status & 0x20)

                if fast_mode:
                    ppu.step_fast(1)
                else:
                    ppu.step_accurate(1)
                self.assertTrue(ppu.status & 0x20)


if __name__ == "__main__":
    unittest.main()
