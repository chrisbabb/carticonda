from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nes_from_scratch.apu import (
    APU,
    DMC_PERIODS,
    FRAME_4_IRQ_START,
    PULSE_MIX_TABLE,
    TND_MIX_TABLE,
)
from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.console import Console
from nes_from_scratch.constants import CPU_HZ
from nes_from_scratch.state import StateError
from support import make_ines


class APUTests(unittest.TestCase):
    def test_local_quiet_sample_batch_matches_every_reference_cycle(
        self,
    ) -> None:
        """Locally held channel/filter state is sample- and state-exact."""
        optimized = APU(sample_rate=44_100)
        reference = APU(sample_rate=44_100)
        for apu in (optimized, reference):
            for address, value in (
                (0x4015, 0x0F),
                (0x4000, 0xDF), (0x4002, 0x21), (0x4003, 0x18),
                (0x4004, 0x9A), (0x4006, 0x0D), (0x4007, 0x20),
                (0x4008, 0xBF), (0x400A, 0x07), (0x400B, 0x18),
                (0x400C, 0x1C), (0x400E, 0x00), (0x400F, 0x18),
            ):
                apu.write_register(address, value)
            # Exercise the output shifter after its final memory byte has
            # arrived; no fetch is pending, but sample-buffer promotion,
            # silence, and DAC deltas still occur inside the quiet path.
            apu.dmc.bytes_remaining = 0
            apu.dmc.sample_buffer = 0xA5
            apu.dmc.shift_register = 0x53
            apu.dmc.bits_remaining = 3
            apu.dmc.timer_counter = 5
            apu.dmc.silence = False
            apu.dmc.output_level = 64

        for count in (1, 39, 40, 41, 127, 911, 2048):
            optimized._step_quiet_samples(count)
            for _ in range(count):
                reference.step()
            self.assertEqual(optimized.get_state(), reference.get_state())

    def test_muted_channel_timers_collapse_to_the_exact_reference_state(
        self,
    ) -> None:
        """Inaudible timer runs are advanced once without changing samples."""
        optimized = APU(sample_rate=44_100)
        optimized.sample_phase = 12_345
        optimized.pulse1.enabled = True
        optimized.pulse1.length = 10
        optimized.pulse1.timer_period = 31
        optimized.pulse1.timer_counter = 7
        optimized.pulse1.sequence = 3
        optimized.pulse1.envelope.constant = True
        optimized.pulse1.envelope.period = 0
        optimized.pulse2.timer_period = 17
        optimized.pulse2.timer_counter = 2
        optimized.pulse2.sequence = 6
        optimized.triangle.timer_period = 9
        optimized.triangle.timer_counter = 4
        optimized.triangle.sequence = 11
        optimized.noise.enabled = True
        optimized.noise.length = 12
        optimized.noise.mode = True
        optimized.noise.period_index = 0
        optimized.noise.timer_counter = 3
        optimized.noise.shift_register = 0x5231
        optimized.noise.envelope.constant = True
        optimized.noise.envelope.period = 0
        optimized.dmc.silence = True
        optimized.dmc.sample_buffer = None
        optimized.dmc.timer_counter = 19
        optimized.dmc.bits_remaining = 3
        optimized.dmc.shift_register = 0xA5

        reference = APU(sample_rate=44_100)
        reference.set_state(optimized.get_state())
        optimized._step_quiet_samples(6_000)
        for _ in range(6_000):
            reference.step()

        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_nonlinear_mixer_tables_match_the_hardware_equations(self) -> None:
        for pulse in range(31):
            expected = (
                0.0
                if pulse == 0
                else 95.88 / (8128.0 / pulse + 100.0)
            )
            self.assertEqual(PULSE_MIX_TABLE[pulse], expected)
        for triangle in range(16):
            for noise in range(16):
                for dmc in range(128):
                    level = (
                        triangle / 8227.0
                        + noise / 12241.0
                        + dmc / 22638.0
                    )
                    expected = (
                        0.0
                        if level == 0
                        else 159.79 / (1.0 / level + 100.0)
                    )
                    index = (triangle << 11) | (noise << 7) | dmc
                    self.assertEqual(TND_MIX_TABLE[index], expected)

    def test_output_filter_softens_abrupt_dac_edges_and_removes_dc(self) -> None:
        apu = APU(sample_rate=44_100)
        apu._mix()
        apu.dmc.output_level = 127

        filtered_step = abs(apu._mix())
        unfiltered_step = int(TND_MIX_TABLE[127] * 28_000)

        self.assertLess(filtered_step, unfiltered_step * 0.75)
        self.assertGreater(filtered_step, 0)
        for _ in range(44_100):
            settled = abs(apu._mix())
        self.assertLess(settled, 1)

    def test_older_state_seeds_the_expanded_output_filter_safely(self) -> None:
        previous = APU(sample_rate=44_100)
        previous.dmc.output_level = 96
        previous._mix()
        state = previous.get_state()
        for key in (
            "hp440_previous_input",
            "hp440_previous_output",
            "lp_previous_input",
            "lp_previous_output",
        ):
            state.pop(key)

        restored = APU(sample_rate=44_100)
        restored.set_state(state)

        self.assertLess(abs(restored._mix()), 32_768)

    def test_channel_enable_length_status_and_disable(self) -> None:
        apu = APU(sample_rate=8_000)
        apu.write_register(0x4015, 0x01)
        apu.write_register(0x4000, 0x9F)
        apu.write_register(0x4002, 0x40)
        apu.write_register(0x4003, 0x08)
        self.assertTrue(apu.read_status() & 1)
        for _ in range(500):
            apu.step()
        self.assertGreater(len(apu.samples), 0)
        apu.write_register(0x4015, 0)
        self.assertFalse(apu.read_status() & 1)

    def test_dmc_fetch_requests_four_cpu_stall_cycles(self) -> None:
        apu = APU()
        apu.dmc_reader = lambda address: address & 0xFF
        apu.write_register(0x4012, 0)
        apu.write_register(0x4013, 0)
        apu.write_register(0x4015, 0x10)
        apu.step()
        self.assertEqual(apu.cpu_stall_cycles, 4)
        self.assertEqual(apu.dmc.sample_buffer, 0)

    def test_dmc_fetch_on_cpu_write_requests_three_stall_cycles(self) -> None:
        apu = APU()
        apu.dmc_reader = lambda address: address & 0xFF
        apu.write_register(0x4012, 0)
        apu.write_register(0x4013, 0)
        apu.write_register(0x4015, 0x10)
        apu.cpu_write_cycle = 1

        apu.step()

        self.assertEqual(apu.cpu_stall_cycles, 3)
        self.assertEqual(apu.dmc.last_fetch_stall, 3)

    def test_dmc_rate_table_values_are_complete_cpu_periods(self) -> None:
        self.assertEqual(DMC_PERIODS[0], 428)
        self.assertEqual(DMC_PERIODS[13], 84)
        self.assertEqual(DMC_PERIODS[15], 54)
        apu = APU(sample_rate=0)
        dmc = apu.dmc
        dmc.silence = False
        dmc.shift_register = 1

        for _ in range(DMC_PERIODS[0] - 1):
            apu.step()
        self.assertEqual(dmc.output_level, 0)
        apu.step()
        self.assertEqual(dmc.output_level, 2)

    def test_batched_clocks_match_the_single_cycle_reference(self) -> None:
        batched = APU(sample_rate=44_100)
        batched.write_register(0x4015, 0x0F)
        batched.write_register(0x4000, 0x9A)
        batched.write_register(0x4002, 0x47)
        batched.write_register(0x4003, 0x28)
        batched.write_register(0x4008, 0x8F)
        batched.write_register(0x400A, 0x21)
        batched.write_register(0x400B, 0x18)
        batched.write_register(0x400C, 0x15)
        batched.write_register(0x400E, 0x83)
        batched.write_register(0x400F, 0x20)
        reference = APU()
        reference.set_state(batched.get_state())

        batched.step_many(3_000)
        for _ in range(3_000):
            reference.step()
        self.assertEqual(batched.get_state(), reference.get_state())

    def test_batched_looping_dmc_matches_each_reference_cycle(self) -> None:
        batched = APU(sample_rate=44_100)
        batched.dmc_reader = lambda address: (address >> 3) & 0xFF
        batched.write_register(0x4010, 0x4F)
        batched.write_register(0x4012, 0x20)
        batched.write_register(0x4013, 0x00)
        batched.write_register(0x4015, 0x10)
        reference = APU()
        reference.set_state(batched.get_state())
        reference.dmc_reader = batched.dmc_reader

        for count in (1, 2, 7, 31, 113, 257, 997, 2_500):
            batched.step_many(count)
            for _ in range(count):
                reference.step()
            self.assertEqual(batched.get_state(), reference.get_state())

    def test_large_batch_splits_exactly_at_frame_sequencer_edges(self) -> None:
        batched = APU(sample_rate=44_100)
        batched.write_register(0x4015, 0x0F)
        batched.write_register(0x4000, 0x9A)
        batched.write_register(0x4002, 0x47)
        batched.write_register(0x4003, 0x28)
        batched.write_register(0x4008, 0x8F)
        batched.write_register(0x400A, 0x21)
        batched.write_register(0x400B, 0x18)
        reference = APU()
        reference.set_state(batched.get_state())

        batched.step_many(40_000)
        for _ in range(40_000):
            reference.step()
        self.assertEqual(batched.get_state(), reference.get_state())

    def test_batched_software_dac_events_match_literal_writes(self) -> None:
        optimized = APU(sample_rate=44_100)
        optimized.write_register(0x4015, 0x0F)
        optimized.write_register(0x4000, 0x9A)
        optimized.write_register(0x4002, 0x47)
        optimized.write_register(0x4003, 0x28)
        optimized.write_register(0x4008, 0x8F)
        optimized.write_register(0x400A, 0x21)
        optimized.write_register(0x400B, 0x18)
        optimized.write_register(0x400C, 0x15)
        optimized.write_register(0x400E, 0x83)
        optimized.write_register(0x400F, 0x20)

        initial_cycles = 5
        events = ((17, 12), (43, 96), (79, 31), (111, 127))
        # Put a host-sample boundary on the first DAC-write cycle. The
        # optimized path must mix the old DAC level before applying the write,
        # exactly like step_many(...); write_register($4011, value).
        first_event_cycle = initial_cycles + events[0][0]
        optimized.sample_phase = (
            optimized.sample_rate * -first_event_cycle
        ) % CPU_HZ

        reference = APU(sample_rate=44_100)
        reference.set_state(optimized.get_state())
        optimized_advance = optimized.step_many_with_dac_events(
            initial_cycles,
            events,
        )

        reference.step_many(initial_cycles)
        previous_offset = 0
        for offset, value in events:
            reference.step_many(offset - previous_offset)
            reference.write_register(0x4011, value)
            previous_offset = offset

        self.assertEqual(
            optimized_advance,
            initial_cycles + events[-1][0],
        )
        self.assertEqual(optimized.get_state(), reference.get_state())

        zero_cycle = APU(sample_rate=0)
        self.assertEqual(
            zero_cycle.step_many_with_dac_events(0, ((0, 91),)),
            0,
        )
        self.assertEqual(zero_cycle.dmc.output_level, 91)


class BusAndStateTests(unittest.TestCase):
    def console(self, marker: int = 0xEA) -> Console:
        rom = make_ines(program=bytes((marker, 0x4C, 0x00, 0x80)))
        return Console(Cartridge.from_bytes(rom))

    @staticmethod
    def arm_immediate_dmc_fetch(console: Console) -> None:
        dmc = console.apu.dmc
        dmc.current_address = 0xC000
        dmc.bytes_remaining = 1
        dmc.sample_buffer = None

    def test_dmc_dma_replays_one_controller_clock(self) -> None:
        console = self.console()
        console.controller1.set_buttons(0)
        console.controller1.write_strobe(1)
        console.controller1.write_strobe(0)
        self.arm_immediate_dmc_fetch(console)

        console._begin_instruction()
        console.bus.cpu_access_cycle = 2
        console.bus.read(0x4016)

        # The held read pulse clocks the serial controller once, followed by
        # the CPU's eventual read.  Starting from $00, two shifts produce $C0.
        self.assertEqual(console.controller1.shift, 0xC0)
        self.assertEqual(console.apu.dmc.fetch_count, 1)

    def test_dmc_dma_replays_ppudata_on_each_held_read_cycle(self) -> None:
        for fetch_cycle, expected_increment in ((8, 3), (9, 4)):
            with self.subTest(fetch_cycle=fetch_cycle):
                console = self.console()
                while console.bus.cpu_cycles < fetch_cycle - 1:
                    console._clock_cpu_cycle()
                console.ppu.ctrl = 0
                console.ppu.v = 0
                self.arm_immediate_dmc_fetch(console)

                console._begin_instruction()
                console.bus.cpu_access_cycle = 2
                console.bus.read(0x2007)

                # Even/get requests hold PPUDATA for halt+dummy; odd/put
                # requests add an alignment read before the original access.
                self.assertEqual(console.apu.dmc.last_fetch_cycle, fetch_cycle)
                self.assertEqual(console.ppu.v, expected_increment)

    def test_oam_dma_copies_page_and_stalls_cpu(self) -> None:
        console = self.console()
        for index in range(256):
            console.bus.write(0x0200 + index, index)
        console.bus.write(0x4014, 0x02)
        self.assertEqual(console.ppu.oam, bytes(range(256)))
        self.assertEqual(console.bus.dma_stall_cycles, 512)

    def test_unmapped_cartridge_expansion_reads_cpu_open_bus(self) -> None:
        console = self.console()
        console.bus.open_bus = 0xA5

        self.assertEqual(console.bus.read(0x4020), 0xA5)
        self.assertEqual(console.bus.read(0x5FFF), 0xA5)

    def test_mirrored_ram_dma_preserves_oam_wrap_and_final_open_bus(self) -> None:
        console = self.console()
        page = bytes((index * 17 + 3) & 0xFF for index in range(256))
        console.bus.ram[0x200:0x300] = page
        console.ppu.oam_addr = 0xF0
        console.ppu._fast_scanline_cache[(0,)] = (b"", 0)
        console.bus.write(0x4014, 0x0A)

        split = 0x10
        self.assertEqual(console.ppu.oam[0xF0:], page[:split])
        self.assertEqual(console.ppu.oam[:0xF0], page[split:])
        self.assertEqual(console.ppu.oam_addr, 0xF0)
        self.assertEqual(console.bus.open_bus, page[-1])
        self.assertFalse(console.ppu._fast_scanline_cache)

    def test_batched_oam_stall_matches_literal_cpu_cycle_clocks(self) -> None:
        optimized = self.console()
        reference = self.console()
        for console in (optimized, reference):
            console.ppu.fast_mode = True
            console.bus.ram[0x200:0x300] = bytes(range(256))
            console.bus.write(0x4014, 0x02)

        optimized_cycles = optimized._drain_stalls()

        if reference.bus.dma_parity_pending:
            reference.bus.dma_stall_cycles = (
                512 + (reference.bus.cpu_cycles & 1)
            )
            reference.bus.dma_parity_pending = False
        reference_cycles = 0
        while (
            reference.bus.dma_stall_cycles > 0
            or reference.apu.cpu_stall_cycles > 0
        ):
            if reference.bus.dma_stall_cycles > 0:
                reference.bus.dma_stall_cycles -= 1
            else:
                reference.apu.cpu_stall_cycles -= 1
            reference._clock_cpu_cycle()
            reference_cycles += 1

        self.assertEqual(optimized_cycles, reference_cycles)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_dmc_dma_overlaps_oam_dma_instead_of_serializing(self) -> None:
        console = self.console()
        console.ppu.fast_mode = True
        console.apu.write_register(0x4010, 0x4F)
        console.apu.write_register(0x4012, 0x00)
        console.apu.write_register(0x4013, 0x00)
        console.apu.write_register(0x4015, 0x10)
        console.bus.write(0x4014, 0x02)
        nominal = 512 + (console.bus.cpu_cycles & 1)

        cycles = console._drain_stalls()

        self.assertEqual(cycles, nominal + 2)
        self.assertEqual(console.apu.cpu_stall_cycles, 0)

    def test_irq_asserted_in_poll_cycle_waits_for_next_poll(self) -> None:
        console = self.console()
        console.cpu._irq_inhibit = False
        console.apu.frame_irq_pending = True
        console.apu.frame_irq_assert_cycle = 100

        console._sample_irq(100)
        self.assertFalse(console.cpu._irq_sampled)
        console._sample_irq(101)
        self.assertTrue(console.cpu._irq_sampled)

    def test_post_oam_resumed_read_sees_same_cycle_irq(self) -> None:
        console = self.console()
        console.cpu._irq_inhibit = False
        console.apu.frame_irq_pending = True
        console.apu.frame_irq_assert_cycle = 100
        console._dma_irq_equality_until = 103

        console._sample_irq(100)

        self.assertTrue(console.cpu._irq_sampled)

    def test_cpu_batch_clocks_apu_before_final_irq_poll(self) -> None:
        console = self.console()
        console.cpu._irq_inhibit = False
        console.apu.frame_irq_inhibit = False
        console.apu.frame_cycle = FRAME_4_IRQ_START - 1
        console.apu.cycle = console.bus.cpu_cycles
        console.cpu.last_opcode = 0xEA
        console.cpu._last_batch_instruction_cycles = 3
        console._pending_cpu_cycles = 0
        console._device_batch_limit = 3

        console._commit_cpu_batch(3)

        self.assertTrue(console.apu.frame_irq_pending)
        self.assertTrue(console.cpu._irq_sampled)

    def test_full_state_round_trip(self) -> None:
        console = self.console()
        console.run_cpu_cycles(100)
        console.bus.ram[0x12] = 0xA7
        expected = console.get_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slot.fnes"
            console.save_state(path)
            console.bus.ram[0x12] = 0
            console.cpu.a = 0xFF
            console.load_state(path)
        self.assertEqual(console.bus.ram[0x12], 0xA7)
        self.assertEqual(console.get_state(), expected)

    def test_state_rejects_a_different_rom(self) -> None:
        first = self.console(0xEA)
        second = self.console(0x78)
        state = first.get_state()
        with self.assertRaises(StateError):
            second.set_state(state)

    def test_fast_scanline_cache_separates_background_and_sprites(self) -> None:
        console = Console(
            Cartridge.from_bytes(
                make_ines(program=bytes((0x4C, 0x00, 0x80)))
            ),
            fast_ppu=True,
        )
        console.ppu.mask = 0x18
        console.ppu._render_scanline_fast(0)
        # Viewport/composed rows are admitted only after an unchanged frame
        # signature demonstrates that retaining them can produce a hit.
        console.ppu._render_scanline_fast(0)
        self.assertTrue(console.ppu._fast_background_cache)
        self.assertTrue(console.ppu._fast_scanline_cache)

        background_entries = len(console.ppu._fast_background_cache)
        console.ppu.oam_addr = 0
        console.ppu.cpu_write_register(0x2004, 1)
        self.assertEqual(
            len(console.ppu._fast_background_cache),
            background_entries,
        )
        self.assertFalse(console.ppu._fast_scanline_cache)

        console.ppu.write_memory(0x2000, 1)
        self.assertFalse(console.ppu._fast_background_cache)

    def test_unchanged_scanline_replays_retained_framebuffer_safely(
        self,
    ) -> None:
        console = Console(
            Cartridge.from_bytes(
                make_ines(program=bytes((0x4C, 0x00, 0x80)))
            ),
            fast_ppu=True,
        )
        ppu = console.ppu
        ppu.mask = 0x18

        first_hit = ppu._render_scanline_fast(12)
        first_pixels = bytes(
            ppu.framebuffer[12 * 256 * 3:13 * 256 * 3]
        )
        second_hit = ppu._render_scanline_fast(12)

        self.assertEqual(second_hit, first_hit)
        self.assertEqual(
            bytes(ppu.framebuffer[12 * 256 * 3:13 * 256 * 3]),
            first_pixels,
        )
        self.assertEqual(ppu.diagnostic_scanline_replays, 1)

        # A write to an unrelated tile row leaves this retained viewport
        # valid instead of forcing all 240 scanlines to be recomposed.
        ppu.write_memory(0x2000 + 5 * 32, 1)
        ppu._render_scanline_fast(12)
        self.assertEqual(ppu.diagnostic_scanline_replays, 2)

        # A change to the row actually fetched by the viewport still composes.
        ppu.write_memory(0x2000, 1)
        ppu._render_scanline_fast(12)
        self.assertEqual(ppu.diagnostic_scanline_replays, 2)

    def test_row_granular_replay_matches_full_frame_invalidation(self) -> None:
        """Retaining unrelated rows produces the same pixels and machine state."""
        rom = bytearray(
            make_ines(program=bytes((0x4C, 0x00, 0x80)))
        )
        chr_start = 16 + 0x4000
        rom[chr_start + 16:chr_start + 24] = b"\xFF" * 8
        optimized = Console(
            Cartridge.from_bytes(bytes(rom), title="row-replay"),
            sample_rate=0,
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(bytes(rom), title="full-invalidation"),
            sample_rate=0,
            fast_ppu=True,
        )
        for console in (optimized, reference):
            console.ppu.mask = 0x0A
            console.ppu.write_memory(0x3F01, 0x30)
            console.run_frame(copy_frame=False)
            console.run_frame(copy_frame=False)

        address = 0x2000 + 5 * 32 + 7
        optimized.ppu.write_memory(address, 1)
        reference.ppu.write_memory(address, 1)
        reference.ppu.invalidate_fast_cache()
        optimized_replays = optimized.ppu.diagnostic_scanline_replays
        reference_replays = reference.ppu.diagnostic_scanline_replays

        optimized_frame = optimized.run_frame()
        reference_frame = reference.run_frame()

        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())
        self.assertGreater(
            optimized.ppu.diagnostic_scanline_replays - optimized_replays,
            reference.ppu.diagnostic_scanline_replays - reference_replays,
        )

    def test_scrolling_frames_do_not_retain_dead_viewport_rows(self) -> None:
        console = Console(
            Cartridge.from_bytes(
                make_ines(program=bytes((0x4C, 0x00, 0x80)))
            ),
            fast_ppu=True,
        )
        console.ppu.mask = 0x18
        for fine_x in range(8):
            console.ppu.fine_x = fine_x
            console.ppu._render_scanline_fast(0)
        self.assertFalse(console.ppu._fast_background_cache)
        self.assertFalse(console.ppu._fast_scanline_cache)
        self.assertTrue(console.ppu._fast_background_world_cache)


if __name__ == "__main__":
    unittest.main()
