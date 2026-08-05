from __future__ import annotations

import unittest

from nes_from_scratch.constants import StatusFlag as F
from nes_from_scratch.cpu import CPU, OPCODES
from support import FlatBus


class CPUTests(unittest.TestCase):
    def cpu(self, program: bytes) -> tuple[CPU, FlatBus]:
        bus = FlatBus.with_program(program)
        cpu = CPU(bus)
        cpu.reset()
        return cpu, bus

    def test_decode_matrix_is_complete(self) -> None:
        self.assertEqual(len(OPCODES), 256)
        self.assertTrue(all(item.cycles >= 2 for item in OPCODES))
        self.assertEqual(sum(not item.unofficial for item in OPCODES), 151)

    def test_load_add_and_overflow_flags(self) -> None:
        cpu, _ = self.cpu(bytes((0xA9, 0x50, 0x18, 0x69, 0x50)))
        self.assertEqual(cpu.step(), 2)
        self.assertEqual(cpu.a, 0x50)
        cpu.step()
        self.assertEqual(cpu.step(), 2)
        self.assertEqual(cpu.a, 0xA0)
        self.assertTrue(cpu.p & F.OVERFLOW)
        self.assertTrue(cpu.p & F.NEGATIVE)
        self.assertFalse(cpu.p & F.CARRY)

    def test_decimal_flag_does_not_enable_bcd(self) -> None:
        cpu, _ = self.cpu(bytes((0xF8, 0xA9, 0x09, 0x18, 0x69, 0x01)))
        for _ in range(4):
            cpu.step()
        cpu.step()
        self.assertEqual(cpu.a, 0x0A)

    def test_one_byte_opcode_fetches_and_discards_following_byte(self) -> None:
        cpu, bus = self.cpu(bytes((0xEA, 0xA5)))
        bus.reads.clear()

        self.assertEqual(cpu.step(), 2)

        self.assertEqual(bus.reads[:2], [0x8000, 0x8001])

    def test_absolute_x_page_penalty(self) -> None:
        cpu, bus = self.cpu(bytes((0xA2, 0x01, 0xBD, 0xFF, 0x80)))
        bus.memory[0x8100] = 0x7B
        cpu.step()
        self.assertEqual(cpu.step(), 5)
        self.assertEqual(cpu.a, 0x7B)

    def test_jsr_rts_and_stack_order(self) -> None:
        program = bytes((
            0x20, 0x06, 0x80,  # JSR $8006
            0xA9, 0x11,        # LDA #$11
            0x00,
            0xA9, 0x42,        # LDA #$42
            0x60,              # RTS
        ))
        cpu, bus = self.cpu(program)
        self.assertEqual(cpu.step(), 6)
        self.assertEqual(cpu.pc, 0x8006)
        self.assertEqual(bus.memory[0x01FD], 0x80)
        self.assertEqual(bus.memory[0x01FC], 0x02)
        cpu.step()
        self.assertEqual(cpu.a, 0x42)
        cpu.step()
        self.assertEqual(cpu.pc, 0x8003)
        cpu.step()
        self.assertEqual(cpu.a, 0x11)

    def test_indirect_jump_wraps_high_byte_within_page(self) -> None:
        cpu, bus = self.cpu(bytes((0x6C, 0xFF, 0x02)))
        bus.memory[0x02FF] = 0x34
        bus.memory[0x0200] = 0x12
        bus.memory[0x0300] = 0x99
        cpu.step()
        self.assertEqual(cpu.pc, 0x1234)

    def test_branch_cycles_include_taken_and_page_cross(self) -> None:
        bus = FlatBus.with_program(bytes((0xD0, 0x80)), start=0x80FE)
        cpu = CPU(bus)
        cpu.reset()
        cpu.p &= ~int(F.ZERO)
        self.assertEqual(cpu.step(), 4)
        self.assertEqual(cpu.pc, 0x8080)

    def test_nmi_pushes_pc_and_loads_vector(self) -> None:
        cpu, bus = self.cpu(bytes((0xEA,)))
        bus.memory[0xFFFA:0xFFFC] = bytes((0x00, 0x90))
        cpu.request_nmi()
        self.assertEqual(cpu.step(), 7)
        self.assertEqual(cpu.pc, 0x9000)
        self.assertEqual(cpu.sp, 0xFA)
        self.assertEqual(bus.memory[0x01FD], 0x80)
        self.assertEqual(bus.memory[0x01FC], 0x00)

    def test_nmi_hijacks_brk_vector_but_preserves_break_flag(self) -> None:
        cpu, bus = self.cpu(bytes((0x00, 0xEA)))
        bus.memory[0xFFFA:0xFFFC] = bytes((0x00, 0x90))
        bus.memory[0xFFFE:0x10000] = bytes((0x00, 0xA0))
        selection_cycles: list[int] = []

        def sync_vector(cycle: int) -> bool:
            selection_cycles.append(cycle)
            return True

        bus.sync_interrupt_vector = sync_vector
        self.assertEqual(cpu.step(), 7)
        self.assertEqual(cpu.pc, 0x9000)
        self.assertEqual(selection_cycles, [3])
        self.assertTrue(bus.memory[0x01FB] & int(F.BREAK))

    def test_nmi_hijacks_irq_vector_without_setting_break_flag(self) -> None:
        cpu, bus = self.cpu(bytes((0xEA,)))
        bus.memory[0xFFFA:0xFFFC] = bytes((0x00, 0x90))
        bus.memory[0xFFFE:0x10000] = bytes((0x00, 0xA0))
        bus.irq_pending = True
        cpu.p &= ~int(F.INTERRUPT)
        cpu._irq_inhibit = False
        selection_cycles: list[int] = []

        def sync_vector(cycle: int) -> bool:
            selection_cycles.append(cycle)
            return True

        bus.sync_interrupt_vector = sync_vector
        self.assertEqual(cpu.step(), 7)
        self.assertEqual(cpu.pc, 0x9000)
        self.assertEqual(selection_cycles, [4])
        self.assertFalse(bus.memory[0x01FB] & int(F.BREAK))

    def test_nmi_wins_and_discards_a_simultaneously_sampled_irq(self) -> None:
        cpu, bus = self.cpu(bytes((0xEA,)))
        bus.memory[0xFFFA:0xFFFC] = bytes((0x00, 0x90))
        cpu.external_irq_sampling = True
        cpu._irq_sampled = True
        cpu.request_nmi()

        self.assertEqual(cpu.step(), 7)
        self.assertEqual(cpu.pc, 0x9000)
        self.assertFalse(cpu._irq_sampled)

    def test_soft_reset_preserves_registers_and_only_decrements_stack(
        self,
    ) -> None:
        cpu, bus = self.cpu(bytes((0xEA,)))
        bus.memory[0xFFFC:0xFFFE] = bytes((0x00, 0x90))
        cpu.a, cpu.x, cpu.y = 0x34, 0x56, 0x78
        cpu.p = 0xE9
        cpu.sp = 0x12
        stack_before = bytes(bus.memory[0x0100:0x0200])

        cpu.reset(cold=False)

        self.assertEqual((cpu.a, cpu.x, cpu.y), (0x34, 0x56, 0x78))
        self.assertEqual(cpu.p, 0xED)
        self.assertEqual(cpu.sp, 0x0F)
        self.assertEqual(cpu.pc, 0x9000)
        self.assertEqual(bytes(bus.memory[0x0100:0x0200]), stack_before)

    def test_cli_delays_irq_polling_for_one_instruction(self) -> None:
        cpu, bus = self.cpu(bytes((0x58, 0xEA, 0xEA)))
        bus.memory[0xFFFE:0x10000] = bytes((0x00, 0x90))
        bus.irq_pending = True
        self.assertEqual(cpu.step(), 2)  # CLI
        self.assertEqual(cpu.pc, 0x8001)
        self.assertEqual(cpu.step(), 2)  # delayed NOP
        self.assertEqual(cpu.pc, 0x8002)
        self.assertEqual(cpu.step(), 7)  # IRQ
        self.assertEqual(cpu.pc, 0x9000)

    def test_cli_sei_polls_the_pre_sei_interrupt_flag(self) -> None:
        cpu, bus = self.cpu(bytes((0x58, 0x78, 0xEA)))
        bus.memory[0xFFFE:0x10000] = bytes((0x00, 0x90))
        bus.irq_pending = True
        cpu.step()  # CLI
        cpu.step()  # SEI still polls the old, clear I flag
        self.assertTrue(cpu.p & F.INTERRUPT)
        self.assertEqual(cpu.step(), 7)
        self.assertEqual(cpu.pc, 0x9000)

    def test_stable_unofficial_lax_and_sax(self) -> None:
        cpu, bus = self.cpu(bytes((0xA7, 0x10, 0x87, 0x11)))
        bus.memory[0x10] = 0xA5
        cpu.step()
        self.assertEqual((cpu.a, cpu.x), (0xA5, 0xA5))
        cpu.step()
        self.assertEqual(bus.memory[0x11], 0xA5)

    def test_unstable_store_uses_base_high_byte_and_corrupts_crossed_page(
        self,
    ) -> None:
        # SHY $12FF,X with X=1 stores Y & ($12 + 1) = $11. Since indexing
        # crosses the page, that value also replaces the effective high byte.
        cpu, bus = self.cpu(bytes((0x9C, 0xFF, 0x12)))
        cpu.x = 1
        cpu.y = 0x55
        self.assertEqual(cpu.step(), 5)
        self.assertEqual(bus.writes[-1], (0x1100, 0x11))
        self.assertEqual(bus.memory[0x1300], 0)

    def test_unstable_store_keeps_normal_address_without_page_cross(
        self,
    ) -> None:
        cpu, bus = self.cpu(bytes((0x9E, 0x10, 0x12)))
        cpu.x = 0x55
        cpu.y = 1
        self.assertEqual(cpu.step(), 5)
        self.assertEqual(bus.writes[-1], (0x1211, 0x11))

    def test_indexed_page_cross_performs_wrong_page_dummy_read(self) -> None:
        cpu, bus = self.cpu(bytes((0xBD, 0xFF, 0x12)))
        cpu.x = 1
        bus.memory[0x1300] = 0xA5
        bus.reads.clear()
        self.assertEqual(cpu.step(), 5)
        self.assertEqual(bus.reads[-2:], [0x1200, 0x1300])
        self.assertEqual(cpu.a, 0xA5)

    def test_indexed_store_always_performs_intermediate_read(self) -> None:
        cpu, bus = self.cpu(bytes((0x99, 0x10, 0x12)))
        cpu.a = 0x5A
        cpu.y = 1
        bus.reads.clear()
        self.assertEqual(cpu.step(), 5)
        self.assertEqual(bus.reads[-1], 0x1211)
        self.assertEqual(bus.writes[-1], (0x1211, 0x5A))

    def test_zero_page_index_and_indirect_x_perform_dummy_reads(
        self,
    ) -> None:
        cpu, bus = self.cpu(bytes((
            0xB5, 0x10,  # LDA $10,X
            0xA1, 0x20,  # LDA ($20,X)
        )))
        cpu.x = 1
        bus.memory[0x11] = 0x33
        bus.memory[0x21:0x23] = bytes((0x00, 0x40))
        bus.memory[0x4000] = 0x44
        bus.reads.clear()
        cpu.step()
        self.assertEqual(bus.reads[-2:], [0x0010, 0x0011])
        cpu.step()
        self.assertEqual(bus.reads[-4:], [0x0020, 0x0021, 0x0022, 0x4000])

    def test_memory_rmw_performs_dummy_then_final_write(self) -> None:
        cpu, bus = self.cpu(bytes((0xE6, 0x10)))  # INC $10
        bus.memory[0x10] = 0x7F
        self.assertEqual(cpu.step(), 5)
        self.assertEqual(
            bus.writes,
            [(0x0010, 0x7F), (0x0010, 0x80)],
        )
        self.assertEqual(bus.memory[0x10], 0x80)

    def test_specialized_opcodes_match_the_generic_decoder(self) -> None:
        probe, _ = self.cpu(bytes((0xEA,)))
        specialized = [
            opcode
            for opcode, handler in enumerate(probe._fast_opcode_handlers)
            if handler is not None
        ]
        self.assertGreaterEqual(len(specialized), 140)

        for opcode in specialized:
            with self.subTest(opcode=f"{opcode:02X}"):
                program = bytes((opcode, 0xF8, 0x12, 0xEA))
                fast_bus = FlatBus.with_program(program)
                generic_bus = FlatBus(bytearray(fast_bus.memory))
                fast = CPU(fast_bus)
                generic = CPU(generic_bus)
                for core in (fast, generic):
                    core.a = 0x5A
                    core.x = 0x11
                    core.y = 0x22
                    core.sp = 0xFD
                    # Deliberately clear the normally hard-wired UNUSED bit
                    # so flag-mutating handlers also cover that edge case.
                    core.p = 0x45
                    core.pc = 0x8000
                    core.total_cycles = 7
                generic._fast_opcode_handlers = (None,) * 256

                self.assertEqual(fast.step(), generic.step())
                self.assertEqual(fast.get_state(), generic.get_state())
                self.assertEqual(fast_bus.memory, generic_bus.memory)


if __name__ == "__main__":
    unittest.main()
