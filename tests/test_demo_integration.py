from __future__ import annotations

import unittest
from unittest.mock import patch

from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.console import Console
from nes_from_scratch.constants import Button
from nes_from_scratch.cpu import (
    OPCODES,
    _BATCH_MEMORY_MISC,
    _BATCH_MISC,
    _BATCH_OPCODE_KINDS,
)
from nes_from_scratch.state import dumps, loads
from support import make_ines
from tools.make_demo_rom import build_rom
from tools.benchmark import (
    build_gameplay_stress_rom,
    build_mmc2_gameplay_stress_rom,
    build_mmc2_forced_blank_stream_rom,
    build_mmc3_gameplay_stress_rom,
    build_mmc3_cross_slot_scroll_rom,
    build_mmc3_irq_stress_rom,
    build_mmc3_prg_bank_churn_rom,
    build_mmc3_register_churn_rom,
    configure_mmc2_gameplay_stress,
)


class DemoIntegrationTests(unittest.TestCase):
    def test_extended_cpu_batches_match_literal_instruction_state(self) -> None:
        """Every newly inlined official opcode remains state-identical."""
        for opcode, kind in enumerate(_BATCH_OPCODE_KINDS):
            if kind not in (_BATCH_MEMORY_MISC, _BATCH_MISC):
                continue
            instruction = OPCODES[opcode]
            program = bytearray((0xEA,) * 0x4000)
            program[0] = opcode
            if instruction.mode.name in {
                "ZERO_PAGE", "ZERO_PAGE_X", "ZERO_PAGE_Y",
                "INDEXED_INDIRECT", "INDIRECT_INDEXED",
            }:
                program[1] = 0x20
            elif instruction.mode.name in {
                "ABSOLUTE", "ABSOLUTE_X", "ABSOLUTE_Y",
            }:
                address = 0x8100 if opcode == 0x20 else 0x0600
                program[1:3] = bytes((address & 0xFF, address >> 8))
            for vector_offset in (0x3FFA, 0x3FFC, 0x3FFE):
                program[vector_offset:vector_offset + 2] = b"\x00\x80"
            rom = make_ines(program=bytes(program))
            optimized = Console(
                Cartridge.from_bytes(rom, title=f"batch-{opcode:02x}"),
                sample_rate=0,
                fast_ppu=True,
            )
            reference = Console(
                Cartridge.from_bytes(rom, title=f"literal-{opcode:02x}"),
                sample_rate=0,
                fast_ppu=True,
            )
            for console in (optimized, reference):
                cpu = console.cpu
                cpu.a = 0x5A
                cpu.x = 0x10
                cpu.y = 0x20
                cpu.p = 0x65
                cpu.sp = 0xFB if opcode == 0x60 else 0xFD
                cpu.pc = 0x8000
                cpu.total_cycles = 41
                cpu._irq_inhibit = True
                ram = console.bus.ram
                for index in range(len(ram)):
                    ram[index] = (index * 37 + 11) & 0xFF
                if instruction.mode.name == "INDEXED_INDIRECT":
                    ram[0x30] = 0x00
                    ram[0x31] = 0x06
                elif instruction.mode.name == "INDIRECT_INDEXED":
                    ram[0x20] = 0xE0
                    ram[0x21] = 0x05
                if opcode == 0x60:
                    ram[0x01FC] = 0x34
                    ram[0x01FD] = 0x81

            with self.subTest(opcode=f"{opcode:02X}"):
                optimized_cycles = optimized.cpu.batch_safe_instructions(1)
                reference_cycles = reference.cpu.step()
                self.assertEqual(optimized_cycles, reference_cycles)
                self.assertEqual(
                    optimized.cpu.get_state(),
                    reference.cpu.get_state(),
                )
                self.assertEqual(optimized.bus.ram, reference.bus.ram)
                self.assertEqual(
                    optimized.bus.open_bus,
                    reference.bus.open_bus,
                )

    def test_extended_cpu_batches_match_crossed_and_banked_reads(self) -> None:
        """Indexed page crossings and MMC3 PRG reads retain literal timing."""
        cases = (
            (0xBD, 0x01F0, 0x20, 0x00),  # LDA $01F0,X -> CPU RAM
            (0xB9, 0x01F0, 0x00, 0x20),  # LDA $01F0,Y -> CPU RAM
            (0x9D, 0x01F0, 0x20, 0x00),  # STA $01F0,X -> CPU RAM
            (0xBD, 0x80F0, 0x20, 0x00),  # LDA $80F0,X -> NROM
        )
        for opcode, base, register_x, register_y in cases:
            program = bytearray((0xEA,) * 0x4000)
            program[0:3] = bytes((opcode, base & 0xFF, base >> 8))
            program[0x0110] = 0xA7
            rom = make_ines(program=bytes(program))
            optimized = Console(Cartridge.from_bytes(rom), sample_rate=0)
            reference = Console(Cartridge.from_bytes(rom), sample_rate=0)
            def initialize(console: Console) -> None:
                console.cpu.pc = 0x8000
                console.cpu.a = 0x5A
                console.cpu.x = register_x
                console.cpu.y = register_y
                console.cpu.p = 0x65
                console.cpu._irq_inhibit = True
                console.cpu.total_cycles = 0
                console.bus.open_bus = 0
                console.bus.ram[0x0210] = 0xA7

            for _ in range(8):
                initialize(optimized)
                self.assertGreater(
                    optimized.cpu.batch_safe_instructions(1),
                    0,
                )
            initialize(optimized)
            initialize(reference)

            with self.subTest(opcode=f"{opcode:02X}", base=f"{base:04X}"):
                self.assertGreater(
                    optimized.cpu.translated_block_compilations,
                    0,
                )
                optimized_cycles = optimized.cpu.batch_safe_instructions(1)
                reference_cycles = reference.cpu.step()
                self.assertEqual(optimized_cycles, reference_cycles)
                self.assertEqual(optimized.cpu.get_state(), reference.cpu.get_state())
                self.assertEqual(optimized.bus.ram, reference.bus.ram)
                self.assertEqual(optimized.bus.open_bus, reference.bus.open_bus)

        # MMC3's active PRG map is segmented into four immutable 8 KiB views.
        # Exercise the mapped-bank lookup rather than only NROM's flat mask.
        program = bytearray((0xEA,) * 0x8000)
        program[0:3] = b"\xBD\xF0\xA0"  # LDA $A0F0,X -> $A110
        program[0x0110] = 0x6D
        rom = make_ines(mapper=4, prg_banks=2, program=bytes(program))
        optimized = Console(Cartridge.from_bytes(rom), sample_rate=0)
        reference = Console(Cartridge.from_bytes(rom), sample_rate=0)
        def initialize_mmc3(console: Console) -> None:
            console.cpu.pc = 0x8000
            console.cpu.x = 0x20
            console.cpu.p = 0x65
            console.cpu._irq_inhibit = True
            console.cpu.total_cycles = 0
            console.bus.open_bus = 0
        optimized.cpu._refresh_segmented_fast_tables()
        for _ in range(8):
            initialize_mmc3(optimized)
            self.assertGreater(
                optimized.cpu.batch_safe_instructions(1),
                0,
            )
        initialize_mmc3(optimized)
        initialize_mmc3(reference)

        optimized_cycles = optimized.cpu.batch_safe_instructions(1)
        reference_cycles = reference.cpu.step()
        self.assertGreater(optimized.cpu.translated_block_compilations, 0)
        self.assertEqual(optimized_cycles, reference_cycles)
        self.assertEqual(optimized.cpu.get_state(), reference.cpu.get_state())
        self.assertEqual(optimized.bus.open_bus, reference.bus.open_bus)

    def test_hot_translated_blocks_match_every_supported_official_opcode(
        self,
    ) -> None:
        """Forced hot-block execution remains identical to one literal step."""
        unsupported_names = {"BRK", "CLI", "PLP", "RTI", "SEI"}
        unsupported_modes = {"INDIRECT"}
        for opcode, instruction in enumerate(OPCODES):
            if (
                instruction.unofficial
                or instruction.name in unsupported_names
                or instruction.mode.name in unsupported_modes
            ):
                continue

            program = bytearray((0xEA,) * 0x4000)
            length = (
                1
                if instruction.mode.name in {"IMPLIED", "ACCUMULATOR"}
                else 2
                if instruction.mode.name in {
                    "IMMEDIATE", "ZERO_PAGE", "ZERO_PAGE_X",
                    "ZERO_PAGE_Y", "RELATIVE", "INDEXED_INDIRECT",
                    "INDIRECT_INDEXED",
                }
                else 3
            )
            encoded = bytearray((opcode,))
            if length == 2:
                encoded.append(
                    0x00 if instruction.mode.name == "RELATIVE" else 0x20
                )
            elif length == 3:
                address = (
                    0x8100
                    if instruction.name in {"JMP", "JSR"}
                    else 0x0600
                )
                encoded.extend((address & 0xFF, address >> 8))
            for offset in range(0, length * 3, length):
                program[offset:offset + length] = encoded
            rom = make_ines(program=bytes(program))
            optimized = Console(
                Cartridge.from_bytes(rom, title=f"translated-{opcode:02x}"),
                sample_rate=0,
                fast_ppu=True,
            )
            reference = Console(
                Cartridge.from_bytes(rom, title=f"literal-{opcode:02x}"),
                sample_rate=0,
                fast_ppu=True,
            )

            def initialize(console: Console) -> None:
                cpu = console.cpu
                cpu.a = 0x5A
                cpu.x = 0x10
                cpu.y = 0x20
                cpu.p = 0x65
                cpu.sp = 0xFB if opcode == 0x60 else 0xFD
                cpu.pc = 0x8000
                cpu.total_cycles = 41
                cpu.last_opcode = 0xEA
                cpu._irq_inhibit = True
                console.bus.open_bus = 0x9B
                ram = console.bus.ram
                for index in range(len(ram)):
                    ram[index] = (index * 37 + 11) & 0xFF
                if instruction.mode.name == "INDEXED_INDIRECT":
                    ram[0x30] = 0x00
                    ram[0x31] = 0x06
                elif instruction.mode.name == "INDIRECT_INDEXED":
                    ram[0x20] = 0xE0
                    ram[0x21] = 0x05
                if opcode == 0x60:
                    ram[0x01FC] = 0x34
                    ram[0x01FD] = 0x81

            # Eight identical observations cross the production hotness gate.
            # Resetting architectural state between them makes compilation a
            # derived-cache change only, never part of the comparison state.
            for _ in range(8):
                initialize(optimized)
                self.assertGreater(
                    optimized.cpu.batch_safe_instructions(1),
                    0,
                )

            with self.subTest(opcode=f"{opcode:02X}"):
                self.assertGreater(
                    optimized.cpu.translated_block_compilations,
                    0,
                )
                initialize(optimized)
                initialize(reference)
                optimized_cycles = optimized.cpu.batch_safe_instructions(1)
                reference_cycles = reference.cpu.step()
                self.assertEqual(optimized_cycles, reference_cycles)
                self.assertEqual(
                    optimized.cpu.get_state(),
                    reference.cpu.get_state(),
                )
                self.assertEqual(optimized.bus.ram, reference.bus.ram)
                self.assertEqual(
                    optimized.bus.open_bus,
                    reference.bus.open_bus,
                )

    def test_full_translated_cache_keeps_the_established_working_set(
        self,
    ) -> None:
        rom = make_ines(program=b"\xEA\xEA\xEA\x4C\x00\x80")
        console = Console(Cartridge.from_bytes(rom), sample_rate=0)
        cpu = console.cpu
        existing_key = (b"existing-hot-bank", 0x8000)
        existing_block = lambda *args: args
        cpu._batch_block_cache[existing_key] = {0x9000: existing_block}
        cpu._translated_block_count = 1

        with patch("nes_from_scratch.cpu.BATCH_BLOCK_CACHE_LIMIT", 1):
            for _ in range(8):
                cpu.pc = 0x8000
                cpu.p = 0x24
                cpu._irq_inhibit = True
                self.assertGreater(cpu.batch_safe_instructions(1), 0)

        self.assertIs(
            cpu._batch_block_cache[existing_key][0x9000],
            existing_block,
        )
        self.assertEqual(cpu._translated_block_count, 1)

    def test_translated_indirect_guard_stops_before_a_device_pointer(
        self,
    ) -> None:
        """A prior RAM write cannot move a translated access into PPU space."""
        program = bytearray((0xEA,) * 0x4000)
        program[:6] = bytes((
            0xA9, 0x20,       # LDA #$20
            0x85, 0x21,       # STA $21: pointer becomes $2000
            0xB1, 0x20,       # LDA ($20),Y: must remain timed/literal
        ))
        rom = make_ines(program=bytes(program))
        optimized = Console(Cartridge.from_bytes(rom), sample_rate=0)
        reference = Console(Cartridge.from_bytes(rom), sample_rate=0)

        def initialize(console: Console) -> None:
            console.cpu.pc = 0x8000
            console.cpu.p = 0x24
            console.cpu._irq_inhibit = True
            console.cpu.total_cycles = 0
            console.bus.open_bus = 0
            console.bus.ram[0x20] = 0
            console.bus.ram[0x21] = 0x06

        for _ in range(8):
            initialize(optimized)
            self.assertEqual(
                optimized.cpu.batch_safe_instructions(100),
                5,
            )
        self.assertGreater(
            optimized.cpu.translated_block_compilations,
            0,
        )
        initialize(optimized)
        initialize(reference)

        self.assertEqual(
            optimized.cpu.batch_safe_instructions(100),
            reference.cpu.batch_safe_instructions(100),
        )
        self.assertEqual(optimized.cpu.get_state(), reference.cpu.get_state())
        self.assertEqual(optimized.bus.ram, reference.bus.ram)
        self.assertEqual(optimized.bus.open_bus, reference.bus.open_bus)

    def test_cpu_driven_pcm_sample_matches_literal_mapper9_loop(
        self,
    ) -> None:
        program = bytearray(0x4000)
        program[0x1018:0x1024] = (
            b"\xEE\xFF\x07\xEE\xFF\x07\xEE\xFF\x07\x4C\x57\x90"
        )
        program[0x1024:0x1063] = (
            b"\xE6\xFE\xD0\xF0\xE6\xFF\xA5\xFF\xC9\x9D\xD0\x08\xA9\x92"
            b"\x85\xFF\xA9\x00\x85\xFE\xEE\xE0\x07\xAD\xE0\x07\xAC\xE1"
            b"\x07\xD9\xC2\x90\xD0\x03\xEE\xE1\x07\xAC\xE2\x07\xD9\xF6"
            b"\x90\xD0\x06\xEE\xE2\x07\x20\xFF\x90\xA0\x00\xB1\xFE\x8D"
            b"\xC2\x07\xA9\x04\x8D\xC0\x07"
        )
        program[0x1063:0x108D] = (
            b"\xAD\xC2\x07\x29\x03\xAC\xC4\x07\xC0\x40\xB0\x02\x90\x02"
            b"\x09\x04\x0D\xC6\x07\xA8\xB9\x1A\x91\x10\x39\x18\x6D\xC4"
            b"\x07\x90\x02\xB0\x02\xA9\x00\x8D\xC4\x07\x4A\x8D\x11\x40"
        )
        program[0x108D:0x10B5] = (
            b"\x4E\xC2\x07\x4E\xC2\x07\xCE\xC0\x07\xF0\x8C\xAC\xE1\x07"
            b"\xB9\xDB\x90\xF0\x21\x8D\xC6\x07\xEE\xFF\x07\xEE\xFF\x07"
            b"\xEE\xFF\x07\xEE\xFF\x07\xEE\xFF\x07\x4C\x63\x90"
        )
        program[0x10B5:0x10C2] = (
            b"\x18\x6D\xC4\x07\xB0\x02\x90\xC9\xA9\xFF\xD0\xC5\x60"
        )
        program[0x10DB + 14] = 112
        program[0x111A + 115] = 20
        program[0x1201:0x1210] = bytes(
            (index * 37 + 11) & 0xFF for index in range(15)
        )
        rom = make_ines(
            mapper=9,
            prg_banks=8,
            program=bytes(program),
        )
        optimized = Console(Cartridge.from_bytes(rom), fast_ppu=True)
        reference = Console(Cartridge.from_bytes(rom), fast_ppu=True)
        for console in (optimized, reference):
            console.cpu.pc = 0x908D
            console.bus.ram[0x07C0] = 4
            console.bus.ram[0x07C2] = 0xFF
            console.bus.ram[0x07C4] = 51
            console.bus.ram[0x07C6] = 112
            console.bus.ram[0x07E1] = 14
            console.bus.ram[0x07FF] = 43
            console.apu.frame_irq_inhibit = True
            console.apu.frame_irq_pending = False
            console.cpu._irq_sampled = False
            console.cpu.cancel_nmi()
            console.ppu.ctrl = 0

        self.assertEqual(optimized.cpu._safe_batch_starts[0x908D], 0)
        result = optimized.cpu.batch_cpu_driven_pcm_sample(130)
        self.assertIsNotNone(result)
        assert result is not None
        literal_start = reference.cpu.total_cycles
        reference.cpu.step()
        while reference.cpu.pc != 0x908D:
            reference.cpu.step()

        self.assertEqual(result[0], reference.cpu.total_cycles - literal_start)
        self.assertEqual(result[1][-1][1], reference.apu.dmc.output_level)
        self.assertEqual(optimized.cpu.get_state(), reference.cpu.get_state())
        self.assertEqual(optimized.bus.ram, reference.bus.ram)

        for console in (optimized, reference):
            console.cpu.pc = 0x908D
            console.bus.ram[0x00FE] = 0x00
            console.bus.ram[0x00FF] = 0x92
            console.cpu._irq_sampled = False
            console.cpu.cancel_nmi()
            while console.ppu.take_nmi_event() is not None:
                pass
            console.bus.ram[0x07C0] = 1
            console.bus.ram[0x07C2] = 0x03
            console.bus.ram[0x07C4] = 51
            console.bus.ram[0x07C6] = 112
            console.bus.ram[0x07E1] = 14
            console.bus.ram[0x07FF] = 43

        multi_result = optimized.cpu.batch_cpu_driven_pcm_sample(1_000)
        self.assertIsNotNone(multi_result)
        assert multi_result is not None
        self.assertGreater(len(multi_result[1]), 4)
        literal_start = reference.cpu.total_cycles
        for event_index, _event in enumerate(multi_result[1]):
            reference.cpu.step()
            for _instruction in range(200):
                if reference.cpu.pc == 0x908D:
                    break
                reference.cpu.step()
            else:
                self.fail(
                    f"literal PCM period {event_index} did not return: "
                    f"PC=${reference.cpu.pc:04X}"
                )

        self.assertEqual(
            reference.cpu.total_cycles - literal_start,
            multi_result[0],
            multi_result,
        )
        self.assertEqual(optimized.cpu.get_state(), reference.cpu.get_state())
        self.assertEqual(optimized.bus.ram, reference.bus.ram)

    def test_ppu_status_wait_batch_matches_literal_polling(
        self,
    ) -> None:
        rom = make_ines(
            program=bytes(
                (
                    0xAD, 0x02, 0x20,       # loop: LDA $2002
                    0x10, 0xFB,             # BPL loop
                    0x4C, 0x00, 0x80,       # JMP loop
                )
            ),
        )
        optimized = Console(Cartridge.from_bytes(rom), fast_ppu=True)
        reference = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
            device_batching=False,
        )
        optimized.enable_diagnostics()

        for _ in range(3):
            optimized_frame = optimized.run_frame(copy_frame=False)
            reference_frame = reference.run_frame(copy_frame=False)

        self.assertGreater(optimized.diagnostic_ppu_wait_batches, 0)
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_mmc2_indirect_rom_stream_batch_matches_literal_state(
        self,
    ) -> None:
        program = bytearray(
            (
                0x78,                         # SEI
                0xA9, 0x00,                   # LDA #<$8100
                0x85, 0x00,                   # STA $00
                0xA9, 0x81,                   # LDA #>$8100
                0x85, 0x01,                   # STA $01
                0xA0, 0x00,                   # LDY #$00
                0xB1, 0x00,                   # loop: LDA ($00),Y
                0x99, 0x00, 0x02,             # STA $0200,Y
                0xC8,                         # INY
                0xD0, 0xF8,                   # BNE loop
                0x4C, 0x0B, 0x80,             # JMP loop
            )
        )
        program.extend(bytes(0x100 - len(program)))
        program.extend(bytes((index * 29 + 7) & 0xFF for index in range(256)))
        rom = make_ines(
            mapper=9,
            prg_banks=8,
            program=bytes(program),
        )
        optimized = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
            device_batching=False,
        )
        calls = 0
        cpu_step = optimized.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return cpu_step()

        optimized.cpu.step = counted_step
        optimized.run_frame(copy_frame=False)
        reference.run_frame(copy_frame=False)

        self.assertLess(calls, 100)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_indirect_stream_uses_literal_timing_during_dmc(self) -> None:
        rom = make_ines(
            program=bytes(
                (
                    0xB1, 0x00,                # LDA ($00),Y
                    0x4C, 0x00, 0x80,          # JMP $8000
                )
            ),
        )
        console = Console(Cartridge.from_bytes(rom), fast_ppu=True)
        console.bus.ram[0:2] = b"\x00\x81"
        console.apu.dmc.bytes_remaining = 1
        console.apu.dmc.sample_buffer = 0

        self.assertFalse(console.cpu._dynamic_batch_start_is_safe())

    def test_mmc2_forced_blank_stream_coalesces_presentation_work(self) -> None:
        rom = build_mmc2_forced_blank_stream_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="mmc2-forced-blank"),
            sample_rate=0,
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="mmc2-forced-blank-reference"),
            sample_rate=0,
            fast_ppu=True,
            device_batching=False,
        )

        optimized.run_frame(copy_frame=False)
        reference.run_frame(copy_frame=False)

        self.assertEqual(optimized.ppu.diagnostic_scanline_renders, 0)
        self.assertLessEqual(optimized.ppu.diagnostic_cache_invalidations, 2)
        # Stream-level application should avoid even the inexpensive
        # already-invalid cache call that older versions made per byte.
        self.assertLess(optimized.ppu.diagnostic_cache_coalesces, 100)
        self.assertGreater(
            optimized.ppu.diagnostic_deferred_data_writes,
            1000,
        )
        self.assertGreater(optimized.cpu.deferred_ppu_stream_batches, 0)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_mmc3_prg_churn_only_classifies_executed_code_bank(self) -> None:
        rom = build_mmc3_prg_bank_churn_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="mmc3-prg-churn"),
            sample_rate=0,
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="mmc3-prg-churn-reference"),
            sample_rate=0,
            fast_ppu=True,
        )
        reference.cpu._safe_batch_starts = None

        optimized.run_frame(copy_frame=False)
        reference.run_frame(copy_frame=False)

        self.assertGreater(optimized.cpu.segmented_map_deferrals, 0)
        self.assertEqual(optimized.cpu.segmented_map_compilations, 1)
        self.assertEqual(len(optimized.cpu._segmented_batch_tables or {}), 1)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_mmc3_stable_map_classifies_newly_entered_code_slot(self) -> None:
        """A PC transition must finish classification without a bank write."""
        rom = build_mmc3_cross_slot_scroll_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="mmc3-stable-slot"),
            sample_rate=0,
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="mmc3-stable-slot-reference"),
            sample_rate=0,
            fast_ppu=True,
            device_batching=False,
        )

        initial_compilations = optimized.cpu.segmented_map_compilations
        safe_view = optimized.cpu._segmented_safe_view
        assert safe_view is not None
        self.assertIsNone(safe_view.slots[2])

        optimized_frame = optimized.run_frame(copy_frame=False)
        reference_frame = reference.run_frame(copy_frame=False)

        self.assertIsNotNone(safe_view.slots[2])
        self.assertGreaterEqual(
            optimized.cpu.segmented_map_compilations,
            initial_compilations + 1,
        )
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_hot_block_compilation_is_bounded_to_one_per_frame(self) -> None:
        """New code regions cannot create a burst of host compile stalls."""
        program = bytearray((0xEA,) * 0x4000)
        console = Console(
            Cartridge.from_bytes(make_ines(program=bytes(program))),
            sample_rate=0,
        )
        cpu = console.cpu
        starts = (0x8000, 0x8100)

        def observe(start: int) -> None:
            cpu.pc = start
            cpu.p = 0x24
            cpu._irq_inhibit = True
            self.assertGreater(cpu.batch_safe_instructions(1), 0)

        # Seven observations leave both starts immediately below the normal
        # production hotness threshold.
        for _ in range(7):
            for start in starts:
                observe(start)

        cpu._translated_block_warmup_frames_remaining = 0
        cpu.begin_translated_block_frame()
        observe(starts[0])
        observe(starts[1])
        self.assertEqual(cpu.translated_block_compilations, 1)
        self.assertEqual(cpu.translated_block_peak_frame_compilations, 1)
        self.assertGreater(cpu.translated_block_deferrals, 0)

        cpu.begin_translated_block_frame()
        observe(starts[1])
        self.assertEqual(cpu.translated_block_compilations, 2)
        self.assertEqual(cpu.translated_block_peak_frame_compilations, 1)

    def test_mmc3_hot_blocks_chain_without_admitting_cold_targets(self) -> None:
        """Only previously translated cross-slot helpers share a deadline."""
        rom = build_mmc3_cross_slot_scroll_rom()
        chained = Console(
            Cartridge.from_bytes(rom, title="mmc3-hot-slot-chain"),
            sample_rate=0,
            fast_ppu=True,
        )
        unchained = Console(
            Cartridge.from_bytes(rom, title="mmc3-hot-slot-control"),
            sample_rate=0,
            fast_ppu=True,
        )
        configure_mmc2_gameplay_stress(chained)
        configure_mmc2_gameplay_stress(unchained)

        for _ in range(30):
            chained.run_frame(copy_frame=False)
            unchained.run_frame(copy_frame=False)
        self.assertGreater(chained.cpu.translated_block_compilations, 0)
        self.assertEqual(chained.get_state(), unchained.get_state())

        chained.enable_diagnostics()
        unchained.enable_diagnostics()
        for _ in range(30):
            chained_frame = chained.run_frame(copy_frame=False)
        with patch("nes_from_scratch.cpu.BATCH_HOT_SLOT_CHAIN_LIMIT", 0):
            for _ in range(30):
                unchained_frame = unchained.run_frame(copy_frame=False)

        self.assertEqual(chained_frame, unchained_frame)
        self.assertEqual(chained.get_state(), unchained.get_state())
        self.assertLess(
            chained.diagnostic_safe_batches,
            unchained.diagnostic_safe_batches,
        )

    def test_mmc3_churn_does_not_recompose_one_scanline_repeatedly(self) -> None:
        console = Console(
            Cartridge.from_bytes(
                build_mmc3_register_churn_rom(),
                title="mmc3-register-churn",
            ),
            sample_rate=0,
            fast_ppu=True,
        )
        render_count = 0
        render_scanline = console.ppu._render_scanline_fast

        def counted_render(scanline: int) -> int:
            nonlocal render_count
            render_count += 1
            return render_scanline(scanline)

        console.ppu._render_scanline_fast = counted_render
        console.run_frame(copy_frame=False)

        self.assertGreater(
            console.ppu.diagnostic_mapper_invalidations,
            500,
        )
        self.assertGreaterEqual(render_count, 240)
        self.assertLessEqual(render_count, 320)

    def test_mmc3_segmented_batch_matches_synchronized_transition(self) -> None:
        rom = build_mmc3_gameplay_stress_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="mmc3-optimized"),
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="mmc3-reference"),
            fast_ppu=True,
            idle_batching=False,
            device_batching=False,
        )
        configure_mmc2_gameplay_stress(optimized)
        configure_mmc2_gameplay_stress(reference)

        for _ in range(12):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()

        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())
        self.assertIsNotNone(optimized.bus._mapped_prg_banks)
        self.assertIsNotNone(optimized.cpu._safe_batch_starts)
        self.assertGreater(optimized.cpu.translated_block_compilations, 0)

    def test_mmc3_transition_uses_visible_rows_until_map_is_stable(self) -> None:
        console = Console(
            Cartridge.from_bytes(
                build_mmc3_gameplay_stress_rom(),
                title="mmc3-cache-admission",
            ),
            fast_ppu=True,
        )
        configure_mmc2_gameplay_stress(console)
        ppu = console.ppu
        ppu.mask = 0x1E
        ppu.invalidate_fast_cache()

        ppu._render_scanline_fast(0)
        self.assertFalse(ppu._fast_world_cache_active)
        self.assertFalse(ppu._fast_background_world_cache)
        ppu._render_scanline_fast(0)
        self.assertTrue(ppu._fast_world_cache_active)
        self.assertTrue(ppu._fast_background_world_cache)

        # Row versions and palette identities are complete content keys. A
        # sparse level-streaming write must not demote an already-proven world
        # cache and force the following scrolling frame through viewport-tile
        # assembly again.
        ppu.write_memory(0x2000, ppu.read_memory(0x2000) ^ 1)
        self.assertTrue(ppu._fast_world_cache_active)

    def test_disabled_mmc3_irq_edges_remain_inside_device_span(self) -> None:
        console = Console(
            Cartridge.from_bytes(
                build_mmc3_gameplay_stress_rom(),
                title="mmc3-disabled-irq-span",
            ),
            fast_ppu=True,
        )
        ppu = console.ppu
        mapper = console.cartridge.mapper
        ppu.scanline = 0
        ppu.cycle = 0
        ppu.mask = 0x18
        mapper.irq_enabled = False
        self.assertGreater(console._dots_to_next_fast_event(), 4)

        ppu.ctrl = 0x08
        mapper.irq_latch = 0
        mapper.irq_counter = 0
        mapper.irq_reload = False
        mapper.last_a12 = False
        mapper.a12_low_since = 0
        mapper.irq_enabled = True
        mapper.irq_pending = False
        # Dot 4 remains low; the filtered sprite-table rise at dot 260 is
        # the first transition capable of asserting this zero-latch IRQ.
        self.assertEqual(console._dots_to_next_fast_event(), 260)

    def test_mmc3_irq_prediction_stops_only_at_asserting_edge(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_mmc3_irq_stress_rom()),
            sample_rate=0,
            fast_ppu=True,
        )
        ppu = console.ppu
        mapper = console.cartridge.mapper
        ppu.scanline = 0
        ppu.cycle = 0
        ppu.master_clock = 0
        ppu.ctrl = 0x08
        ppu.mask = 0x18
        ppu._rendering_active = True
        ppu._rendering_target = True
        mapper.irq_latch = 3
        mapper.irq_counter = 3
        mapper.irq_reload = False
        mapper.irq_enabled = True
        mapper.irq_pending = False
        mapper.last_a12 = False
        mapper.a12_low_since = 0

        asserting_dot = 2 * 341 + 260
        self.assertEqual(
            ppu.dots_to_mmc3_irq_assertion(),
            asserting_dot,
        )
        self.assertEqual(
            console._dots_to_next_fast_event(),
            asserting_dot,
        )
        ppu.step_fast(asserting_dot - 1)
        self.assertFalse(mapper.irq_pending)
        ppu.step_fast(1)
        self.assertTrue(mapper.irq_pending)

    def test_constant_time_mmc3_irq_prediction_matches_phase_reference(
        self,
    ) -> None:
        """The O(1) predictor matches every modeled A12 phase transition."""
        console = Console(
            Cartridge.from_bytes(build_mmc3_irq_stress_rom()),
            sample_rate=0,
            fast_ppu=True,
        )
        ppu = console.ppu
        mapper = console.cartridge.mapper
        ppu.mask = 0x18
        ppu._rendering_active = True
        ppu._rendering_target = True
        mapper.irq_enabled = True
        mapper.irq_pending = False

        def phase_reference() -> int | None:
            background_address = 0x1000 if ppu.ctrl & 0x10 else 0
            sprite_address = 0x1000 if ppu.ctrl & 0x08 else 0
            last_a12 = bool(mapper.last_a12)
            low_since = int(mapper.a12_low_since)
            counter = int(mapper.irq_counter)
            reload_counter = bool(mapper.irq_reload)
            elapsed = 0
            for line in range(ppu.scanline, 240):
                line_start = elapsed - (
                    ppu.cycle if line == ppu.scanline else 0
                )
                first_cycle = ppu.cycle if line == ppu.scanline else 0
                for event_cycle, address in (
                    (4, background_address),
                    (260, sprite_address),
                    (324, background_address),
                ):
                    if event_cycle <= first_cycle:
                        continue
                    event_elapsed = line_start + event_cycle
                    event_clock = ppu.master_clock + event_elapsed
                    a12 = bool(address & 0x1000)
                    if not a12:
                        if last_a12:
                            low_since = event_clock
                    elif not last_a12 and event_clock - low_since >= 9:
                        if counter == 0 or reload_counter:
                            counter = mapper.irq_latch
                            reload_counter = False
                        else:
                            counter = (counter - 1) & 0xFF
                        if counter == 0:
                            return event_elapsed
                    last_a12 = a12
                elapsed = line_start + 341
            return None

        for ctrl in (0x00, 0x08, 0x10, 0x18):
            ppu.ctrl = ctrl
            for scanline in (0, 17, 238, 239):
                ppu.scanline = scanline
                for cycle in (0, 3, 4, 100, 259, 260, 323, 324, 340):
                    ppu.cycle = cycle
                    ppu.master_clock = 1000 + scanline * 341 + cycle
                    for last_a12 in (False, True):
                        mapper.last_a12 = last_a12
                        for low_age in (0, 8, 9, 500):
                            mapper.a12_low_since = (
                                ppu.master_clock - low_age
                            )
                            for counter in (0, 1, 3, 255):
                                mapper.irq_counter = counter
                                for latch in (0, 1, 3, 255):
                                    mapper.irq_latch = latch
                                    for reload_counter in (False, True):
                                        mapper.irq_reload = reload_counter
                                        self.assertEqual(
                                            ppu.dots_to_mmc3_irq_assertion(),
                                            phase_reference(),
                                            (
                                                ctrl,
                                                scanline,
                                                cycle,
                                                last_a12,
                                                low_age,
                                                counter,
                                                latch,
                                                reload_counter,
                                            ),
                                        )

    def test_complete_visible_line_batch_matches_generic_fast_events(
        self,
    ) -> None:
        """Whole-line dispatch preserves pixels, scroll, flags, and MMC3."""
        rom = build_mmc3_irq_stress_rom()
        for ctrl in (0x00, 0x08, 0x10, 0x18):
            optimized = Console(
                Cartridge.from_bytes(rom, title=f"line-batch-{ctrl}"),
                sample_rate=0,
                fast_ppu=True,
            )
            reference = Console(
                Cartridge.from_bytes(rom, title=f"line-events-{ctrl}"),
                sample_rate=0,
                fast_ppu=True,
            )
            for candidate in (optimized, reference):
                ppu = candidate.ppu
                ppu.ctrl = ctrl
                ppu.mask = 0x1E
                ppu._rendering_active = True
                ppu._rendering_target = True
                ppu.scanline = 7
                ppu.cycle = 0
                ppu.master_clock = 7000
                ppu.v = 0x21A3
                ppu.t = 0x2C41
                ppu.fine_x = 3
                configure_mmc2_gameplay_stress(candidate)
                ppu.oam[:] = bytes((0xFF, 0, 0, 0)) * 64
                for index in range(12):
                    base = index * 4
                    ppu.oam[base:base + 4] = bytes((
                        6 + index,
                        index + 1,
                        (index & 3) | (0x20 if index & 1 else 0),
                        8 + index * 17,
                    ))
                ppu.invalidate_fast_sprite_cache()
                mapper = candidate.cartridge.mapper
                mapper.irq_latch = 3
                mapper.irq_counter = 2
                mapper.irq_reload = True
                mapper.irq_enabled = True
                mapper.irq_pending = False
                mapper.last_a12 = False
                mapper.a12_low_since = ppu.master_clock - 20
            reference.ppu._disable_fast_visible_line_batch = True

            dots = 11 * 341 + 117
            optimized.ppu.step_fast(dots)
            reference.ppu.step_fast(dots)

            self.assertEqual(
                optimized.ppu.get_state(),
                reference.ppu.get_state(),
                f"PPUCTRL={ctrl:#04x}",
            )
            self.assertEqual(
                optimized.cartridge.mapper.get_state(),
                reference.cartridge.mapper.get_state(),
                f"PPUCTRL={ctrl:#04x}",
            )

    def test_mmc3_prerender_irq_keeps_an_explicit_edge(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_mmc3_irq_stress_rom()),
            sample_rate=0,
            fast_ppu=True,
        )
        ppu = console.ppu
        mapper = console.cartridge.mapper
        ppu.scanline = 261
        ppu.cycle = 4
        ppu.ctrl = 0x08
        ppu.mask = 0x18
        ppu._rendering_active = True
        ppu._rendering_target = True
        mapper.irq_latch = 0
        mapper.irq_counter = 0
        mapper.irq_enabled = True
        mapper.irq_pending = False

        self.assertEqual(console._dots_to_next_fast_event(), 256)

    def test_mmc3_irq_batch_matches_synchronized_reference(self) -> None:
        rom = build_mmc3_irq_stress_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="mmc3-irq-batched"),
            sample_rate=0,
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="mmc3-irq-reference"),
            sample_rate=0,
            fast_ppu=True,
            idle_batching=False,
            device_batching=False,
        )
        optimized.enable_diagnostics()

        for _ in range(12):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()

        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())
        self.assertLess(optimized.diagnostic_device_flushes, 500)
        self.assertTrue(
            any(
                optimized.cpu._poll_loop_starts[address] == 2
                for address in range(0xE000, 0x10000)
            )
        )
        self.assertGreater(optimized.diagnostic_poll_batches, 0)

    def test_mmc2_rom_boots_and_renders_through_the_fast_path(self) -> None:
        program = bytes(
            (
                0x78,                   # SEI
                0xA9, 0x01,
                0x8D, 0x00, 0xB0,       # FD bank at $0000
                0xA9, 0x02,
                0x8D, 0x00, 0xC0,       # FE bank at $0000
                0xA9, 0x03,
                0x8D, 0x00, 0xD0,       # FD bank at $1000
                0xA9, 0x04,
                0x8D, 0x00, 0xE0,       # FE bank at $1000
                0xA9, 0x00,
                0x8D, 0x00, 0xF0,       # Vertical mirroring
                0xA9, 0x0A,
                0x8D, 0x01, 0x20,       # Background and left edge on
                0x4C, 0x1F, 0x80,       # JMP $801F
            )
        )
        cartridge = Cartridge.from_bytes(
            make_ines(
                mapper=9,
                prg_banks=8,
                chr_banks=16,
                program=program,
            ),
            title="mmc2-original-validation",
        )
        cartridge.chr_memory[0x1000] = 0xFF
        console = Console(cartridge, fast_ppu=True)
        console.ppu.palette_ram[0] = 0x0F
        console.ppu.palette_ram[1] = 0x30
        console.ppu.nametable_ram[0] = 0xFD
        frame = console.run_frame()

        self.assertTrue(console.ppu.fast_mode)
        self.assertFalse(cartridge.mapper.latch_0000_fe)
        self.assertGreater(len(set(zip(*[iter(frame)] * 3))), 1)
        self.assertGreater(len(console.apu.samples), 0)

        saved = console.get_state()
        encoded = dumps(saved)
        cartridge.cpu_write(0xB000, 0x1F)
        cartridge.ppu_read(0x0FE8)
        console.set_state(loads(encoded))
        self.assertEqual(
            cartridge.mapper.get_state(),
            saved["cartridge"]["mapper"],
        )

    def test_mmc2_prg_switch_preserves_ppu_cache_and_restores_code_map(
        self,
    ) -> None:
        console = Console(
            Cartridge.from_bytes(
                make_ines(
                    mapper=9,
                    prg_banks=8,
                    chr_banks=16,
                    fill_banks=True,
                )
            ),
            fast_ppu=True,
        )
        ppu = console.ppu
        mapper = console.cartridge.mapper
        console.bus.write(0xB000, 1)
        console.bus.write(0xC000, 2)
        ppu.mask = 0x0A
        ppu.palette_ram[0] = 0x0F
        ppu.palette_ram[1] = 0x30
        ppu.nametable_ram[0] = 0xFD
        for _ in range(3):
            mapper.set_ppu_latch_state((True, True))
            ppu._render_scanline_fast(0)

        self.assertTrue(ppu._fast_latched_background_cache)
        cached_backgrounds = len(ppu._fast_latched_background_cache)
        self.assertEqual(console.bus.peek_code(0x8000), 0)

        console.bus.write(0xA000, 5)
        self.assertEqual(
            len(ppu._fast_latched_background_cache),
            cached_backgrounds,
        )
        self.assertEqual(console.bus.peek_code(0x8000), 2)
        saved = console.get_state()

        console.bus.write(0xA000, 7)
        self.assertEqual(console.bus.peek_code(0x8000), 3)
        console.set_state(saved)
        self.assertEqual(console.bus.peek_code(0x8000), 2)

        console.bus.write(0xB000, 3)
        self.assertFalse(ppu._fast_latched_background_cache)
        self.assertFalse(ppu._fast_latched_fetch_plan_cache)

    def test_original_gameplay_stress_rom_exercises_nmi_dma_and_audio(self) -> None:
        console = Console(
            Cartridge.from_bytes(
                build_gameplay_stress_rom(),
                title="gameplay-stress",
            ),
            fast_ppu=True,
        )
        for _ in range(8):
            console.run_frame(copy_frame=False)
        self.assertEqual(console.ppu.mask, 0x1E)
        self.assertGreater(console.ppu.oam[3], 0)
        self.assertTrue(console.apu.pulse1.enabled)
        self.assertGreater(len(console.apu.samples), 0)

    def test_nrom_cpu_span_batch_matches_literal_scheduler_state(self) -> None:
        rom = build_gameplay_stress_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="cpu-span-batched"),
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="cpu-span-reference"),
            fast_ppu=True,
        )
        reference.cpu._safe_batch_starts = None

        for _ in range(8):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_nrom_cpu_span_batch_removes_scheduler_dispatch_from_gameplay(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_gameplay_stress_rom()),
            fast_ppu=True,
        )
        for _ in range(5):
            console.run_frame(copy_frame=False)

        calls = 0
        original_step = console.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return original_step()

        console.cpu.step = counted_step
        console.run_frame(copy_frame=False)
        self.assertLess(calls, 100)

    def test_mmc2_cpu_span_batch_matches_literal_scheduler_state(self) -> None:
        rom = build_mmc2_gameplay_stress_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="mmc2-span-batched"),
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="mmc2-span-reference"),
            fast_ppu=True,
            idle_batching=False,
        )
        configure_mmc2_gameplay_stress(optimized)
        configure_mmc2_gameplay_stress(reference)
        reference.cpu._banked_batch_tables = None
        reference.cpu._safe_batch_starts = None
        reference.cpu._counter_loop_opcodes = None
        reference.cpu._poll_loop_starts = None

        for _ in range(20):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_mmc2_cpu_span_batch_removes_scheduler_dispatch(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_mmc2_gameplay_stress_rom()),
            fast_ppu=True,
        )
        configure_mmc2_gameplay_stress(console)
        for _ in range(5):
            console.run_frame(copy_frame=False)

        calls = 0
        original_step = console.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return original_step()

        console.cpu.step = counted_step
        console.run_frame(copy_frame=False)
        self.assertLess(calls, 100)

    def test_original_demo_boots_renders_and_reads_controller(self) -> None:
        console = Console(Cartridge.from_bytes(build_rom(), title="demo"))
        frame = b""
        for _ in range(5):
            frame = console.run_frame()
        colors_before = set(zip(*[iter(frame)] * 3))
        self.assertGreater(len(colors_before), 2)
        self.assertEqual(console.ppu.mask & 0x08, 0x08)

        console.set_button(1, Button.A, True)
        for _ in range(2):
            frame = console.run_frame()
        self.assertEqual(console.ppu.palette_ram[1], 0x16)
        self.assertGreater(len(set(zip(*[iter(frame)] * 3))), 2)

    def test_interactive_scanline_path_renders_the_demo(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_rom(), title="demo"),
            fast_ppu=True,
        )
        frame = b""
        for _ in range(5):
            frame = console.run_frame()
        self.assertGreater(len(set(zip(*[iter(frame)] * 3))), 2)
        self.assertTrue(console.ppu.fast_mode)

    def test_idle_batch_matches_instruction_by_instruction_state(self) -> None:
        rom = build_rom()
        optimized = Console(
            Cartridge.from_bytes(rom, title="optimized"),
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="reference"),
            fast_ppu=True,
            idle_batching=False,
            device_batching=False,
        )
        # Keep the synchronized control on the public Bus.read_code route so
        # this comparison also covers the NROM instruction-byte specialization.
        reference.cpu._nrom_prg = None

        for _ in range(5):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_idle_batch_reduces_python_instruction_dispatch(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_rom(), title="dispatch-count"),
            fast_ppu=True,
        )
        for _ in range(5):
            console.run_frame()

        calls = 0
        original_step = console.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return original_step()

        console.cpu.step = counted_step
        console.run_frame()
        self.assertLess(calls, 1_000)

    def test_device_clock_batch_matches_synchronized_busy_loop(self) -> None:
        program = bytes(
            (
                0xA9, 0x55,       # LDA #$55
                0x85, 0x10,       # STA $10
                0xE8,             # INX
                0xD0, 0xF9,       # BNE $8000
                0x4C, 0x00, 0x80, # JMP $8000
            )
        )
        rom = make_ines(program=program)
        optimized = Console(
            Cartridge.from_bytes(rom, title="device-batched"),
            fast_ppu=True,
            idle_batching=False,
        )
        reference = Console(
            Cartridge.from_bytes(rom, title="synchronized"),
            fast_ppu=True,
            idle_batching=False,
            device_batching=False,
        )

        for _ in range(3):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_device_batch_reduces_apu_scheduler_dispatch(self) -> None:
        program = bytes((0xE8, 0xD0, 0xFD, 0x4C, 0x00, 0x80))
        console = Console(
            Cartridge.from_bytes(make_ines(program=program)),
            fast_ppu=True,
            idle_batching=False,
        )
        for _ in range(2):
            console.run_frame()

        calls = 0
        original_step_many = console.apu.step_many

        def counted_step_many(count: int) -> None:
            nonlocal calls
            calls += 1
            original_step_many(count)

        console.apu.step_many = counted_step_many
        console.run_frame()
        self.assertLess(calls, 1_000)

    def test_counter_loop_batches_match_literal_instruction_state(self) -> None:
        for opcode, load_opcode in (
            (0xE8, 0xA2),  # INX / LDX
            (0xCA, 0xA2),  # DEX / LDX
            (0xC8, 0xA0),  # INY / LDY
            (0x88, 0xA0),  # DEY / LDY
        ):
            with self.subTest(opcode=f"{opcode:02X}"):
                program = bytes(
                    (
                        load_opcode, 0x20,
                        opcode,
                        0xD0, 0xFD,
                        0x4C, 0x02, 0x80,
                    )
                )
                rom = make_ines(program=program)
                optimized = Console(
                    Cartridge.from_bytes(rom),
                    fast_ppu=True,
                )
                reference = Console(
                    Cartridge.from_bytes(rom),
                    fast_ppu=True,
                    idle_batching=False,
                    device_batching=False,
                )
                for _ in range(3):
                    optimized_frame = optimized.run_frame()
                    reference_frame = reference.run_frame()
                self.assertEqual(optimized_frame, reference_frame)
                self.assertEqual(optimized.get_state(), reference.get_state())

    def test_counter_loop_batch_reduces_cpu_dispatch(self) -> None:
        program = bytes((0xE8, 0xD0, 0xFD, 0x4C, 0x00, 0x80))
        console = Console(
            Cartridge.from_bytes(make_ines(program=program)),
            fast_ppu=True,
        )
        console.run_frame()

        calls = 0
        original_step = console.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return original_step()

        console.cpu.step = counted_step
        console.run_frame()
        self.assertLess(calls, 2_000)

    def test_counter_loop_batch_preserves_branch_page_penalty(self) -> None:
        program = bytearray(0x103)
        program[:3] = b"\x4c\xfd\x80"  # JMP $80FD
        program[0xFD:0x103] = b"\xe8\xd0\xfd\x4c\xfd\x80"
        rom = make_ines(program=bytes(program))
        optimized = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
        )
        reference = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
            idle_batching=False,
            device_batching=False,
        )
        for _ in range(3):
            optimized_frame = optimized.run_frame()
            reference_frame = reference.run_frame()
        self.assertEqual(optimized_frame, reference_frame)
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_ram_poll_loop_batch_matches_literal_instruction_state(self) -> None:
        # Cover load, BIT, and compare forms, zero-page/absolute reads, and
        # every branch-flag family used by the recognizer.
        cases = (
            ("lda-bne", b"\xa5\x00\xd0\xfc", 0x01, "a", 0x00, 0x24),
            ("ldy-beq", b"\xa4\x00\xf0\xfc", 0x00, "y", 0x00, 0x24),
            ("ldx-bmi", b"\xae\x00\x08\x30\xfb", 0x80, "x", 0x00, 0x24),
            ("bit-bvs", b"\x2c\x00\x08\x70\xfb", 0x40, "a", 0x40, 0x24),
            ("bit-bvc", b"\x24\x00\x50\xfc", 0x01, "a", 0x01, 0x24),
            ("cmp-bcc", b"\xc5\x00\x90\xfc", 0x01, "a", 0x00, 0x24),
            ("cpx-bcs", b"\xec\x00\x08\xb0\xfb", 0x01, "x", 0x02, 0x24),
            ("cpy-bpl", b"\xc4\x00\x10\xfc", 0x01, "y", 0x02, 0x24),
        )
        for name, program, ram_value, register, value, status in cases:
            with self.subTest(name=name):
                rom = make_ines(program=program)
                optimized = Console(
                    Cartridge.from_bytes(rom),
                    fast_ppu=True,
                )
                reference = Console(
                    Cartridge.from_bytes(rom),
                    fast_ppu=True,
                    idle_batching=False,
                    device_batching=False,
                )
                for console in (optimized, reference):
                    console.bus.ram[0] = ram_value
                    setattr(console.cpu, register, value)
                    console.cpu.p = status

                for _ in range(2):
                    optimized_frame = optimized.run_frame()
                    reference_frame = reference.run_frame()
                self.assertEqual(optimized_frame, reference_frame)
                self.assertEqual(optimized.get_state(), reference.get_state())

    def test_ram_poll_loop_batch_reduces_cpu_dispatch(self) -> None:
        program = bytes(
            (
                0xA9, 0x01,
                0x85, 0x00,
                0xA5, 0x00,
                0xD0, 0xFC,
            )
        )
        console = Console(
            Cartridge.from_bytes(make_ines(program=program)),
            fast_ppu=True,
        )
        console.run_frame()
        calls = 0
        original_step = console.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return original_step()

        console.cpu.step = counted_step
        console.run_frame()
        self.assertLess(calls, 100)

    def test_mapper9_dmc_ram_poll_batches_dma_without_losing_cycles(
        self,
    ) -> None:
        # Punch-Out spends its DMC-heavy black-screen interval in this exact
        # family of RAM wait loops. Each sample fetch used to split the loop
        # into another Python scheduler/PPU/APU dispatch.
        rom = make_ines(
            mapper=9,
            prg_banks=8,
            program=bytes(
                (
                    0xA5, 0x1F,              # LDA $1F
                    0xD0, 0xFC,              # BNE $8000
                )
            ),
        )
        optimized = Console(Cartridge.from_bytes(rom), fast_ppu=True)
        reference = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
            idle_batching=False,
            device_batching=False,
        )
        for console in (optimized, reference):
            console.bus.ram[0x1F] = 1
            console.apu.write_register(0x4010, 0x4F)
            console.apu.write_register(0x4012, 0x00)
            console.apu.write_register(0x4013, 0x04)
            console.apu.write_register(0x4015, 0x10)

        optimized.enable_diagnostics()
        calls = 0
        original_step = optimized.cpu.step

        def counted_step() -> int:
            nonlocal calls
            calls += 1
            return original_step()

        optimized.cpu.step = counted_step
        for _ in range(3):
            optimized_frame = optimized.run_frame(copy_frame=False)
            reference_frame = reference.run_frame(copy_frame=False)
            self.assertEqual(optimized_frame, reference_frame)
            self.assertEqual(optimized.get_state(), reference.get_state())

        self.assertGreater(optimized.apu.dmc.fetch_count, 150)
        self.assertGreater(optimized.diagnostic_dmc_poll_batches, 10)
        self.assertGreater(optimized.diagnostic_dmc_poll_fetches, 100)
        self.assertLess(calls, 100)

    def test_sprite_hit_wait_batch_matches_literal_with_active_dmc(
        self,
    ) -> None:
        # LDA $2002 / AND #$40 / BEQ $8000 is the split-screen wait used by
        # Punch-Out. Keep a buffered DMC sample active to cover the scheduler
        # path that previously disabled broad indirect batching.
        program = bytes((0xAD, 0x02, 0x20, 0x29, 0x40, 0xF0, 0xF9))
        rom = make_ines(program=program)
        optimized = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
        )
        ppu = optimized.ppu
        ppu.mask = 0x1E
        ppu._rendering_active = True
        ppu._rendering_target = True
        ppu.scanline = 10
        ppu.cycle = 20
        ppu.status &= ~0x40
        ppu.oam[0] = 100
        optimized.cpu.p |= 0x40  # AND must preserve overflow.
        dmc = optimized.apu.dmc
        dmc.rate_index = 15
        dmc.timer_counter = 100
        dmc.bits_remaining = 8
        dmc.sample_buffer = 0xAA
        dmc.bytes_remaining = 4

        reference = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
        )
        reference.set_state(optimized.get_state())

        batched_cycles = optimized._batch_ppu_sprite_hit_wait(90)
        self.assertEqual(batched_cycles, 90)
        optimized._flush_pending_devices()
        for _ in range(30):
            reference.step_instruction()

        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_sprite_hit_wait_stops_for_exact_dmc_fetch_boundary(
        self,
    ) -> None:
        program = bytes((0xAD, 0x02, 0x20, 0x29, 0x40, 0xF0, 0xF9))
        rom = make_ines(program=program)
        optimized = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
        )
        ppu = optimized.ppu
        ppu.mask = 0x1E
        ppu._rendering_active = True
        ppu._rendering_target = True
        ppu.scanline = 10
        ppu.cycle = 20
        ppu.status &= ~0x40
        ppu.oam[0] = 100
        dmc = optimized.apu.dmc
        dmc.rate_index = 15
        dmc.timer_counter = 9
        dmc.bits_remaining = 1
        dmc.sample_buffer = 0xAA
        dmc.bytes_remaining = 4

        reference = Console(
            Cartridge.from_bytes(rom),
            fast_ppu=True,
        )
        reference.set_state(optimized.get_state())

        optimized._refresh_device_batch_limit()
        self.assertEqual(optimized._device_batch_limit, 10)
        self.assertEqual(optimized._batch_ppu_sprite_hit_wait(10), 9)
        optimized._flush_pending_devices()
        for _ in range(3):
            reference.step_instruction()
        self.assertEqual(optimized.get_state(), reference.get_state())

        # The following LDA reaches the DMC request. Both paths must replay
        # the held PPU-status read and drain the same four-cycle DMA stall.
        optimized.step_instruction()
        reference.step_instruction()
        optimized.step_instruction()
        reference.step_instruction()
        self.assertEqual(optimized.get_state(), reference.get_state())

    def test_sprite_hit_wait_stops_before_latched_scanline_commit(
        self,
    ) -> None:
        program = bytes((0xAD, 0x02, 0x20, 0x29, 0x40, 0xF0, 0xF9))
        console = Console(
            Cartridge.from_bytes(make_ines(program=program)),
            fast_ppu=True,
        )
        ppu = console.ppu
        ppu.mask = 0x1E
        ppu._rendering_active = True
        ppu._rendering_target = True
        ppu.scanline = 20
        ppu.cycle = 253
        ppu.status &= ~0x40
        ppu.oam[0] = 19
        ppu._fast_status_cycles = lambda _rendering: (0, 0)
        console._pending_cpu_cycles = 1

        # Dot 256 can reveal a mapper-latched sprite hit. The pending CPU
        # clock already reaches that checkpoint, so no taken iteration may
        # be speculated across it.
        self.assertEqual(console._batch_ppu_sprite_hit_wait(90), 0)

    def test_frontend_can_borrow_the_live_framebuffer_without_copying(self) -> None:
        console = Console(
            Cartridge.from_bytes(build_rom()),
            fast_ppu=True,
        )
        borrowed = console.run_frame(copy_frame=False)
        copied = console.run_frame()
        self.assertIs(borrowed, console.ppu.framebuffer)
        self.assertIsInstance(copied, bytes)
        self.assertEqual(bytes(borrowed), copied)

    def test_device_batch_matches_dmc_and_dma_fallbacks(self) -> None:
        programs = {
            "dmc": bytes(
                (
                    0x78,                   # SEI
                    0xA9, 0x4F,             # LDA #$4F (looping fast DMC)
                    0x8D, 0x10, 0x40,       # STA $4010
                    0x8D, 0x12, 0x40,       # STA $4012
                    0x8D, 0x13, 0x40,       # STA $4013
                    0xA9, 0x10,             # LDA #$10
                    0x8D, 0x15, 0x40,       # STA $4015
                    0xE8,                   # INX
                    0xD0, 0xFD,             # BNE $8011
                    0x4C, 0x11, 0x80,       # JMP $8011
                )
            ),
            "dma": bytes(
                (
                    0xA9, 0x02,             # LDA #$02
                    0x8D, 0x14, 0x40,       # STA $4014
                    0x4C, 0x05, 0x80,       # JMP $8005
                )
            ),
        }
        for name, program in programs.items():
            with self.subTest(path=name):
                rom = make_ines(program=program)
                optimized = Console(
                    Cartridge.from_bytes(rom),
                    fast_ppu=True,
                    idle_batching=False,
                )
                reference = Console(
                    Cartridge.from_bytes(rom),
                    fast_ppu=True,
                    idle_batching=False,
                    device_batching=False,
                )
                optimized_frame = optimized.run_frame()
                reference_frame = reference.run_frame()
                self.assertEqual(optimized_frame, reference_frame)
                self.assertEqual(optimized.get_state(), reference.get_state())


if __name__ == "__main__":
    unittest.main()
