"""Top-level NES console and clock-domain scheduler."""

from __future__ import annotations

from pathlib import Path

from .apu import (
    APU,
    DMC_PERIODS,
    FRAME_4_CLOCK,
    FRAME_4_END,
    FRAME_4_IRQ_START,
    FRAME_5_CLOCK,
    FRAME_5_END,
    FRAME_Q1,
    FRAME_Q2,
    FRAME_Q3,
)
from .bus import Bus
from .cartridge import Cartridge
from .constants import Button, DEFAULT_SAMPLE_RATE
from .controller import Controller
from .cpu import CPU
from .ppu import PPU
from .state import StateError, loads, save_atomic


class Console:
    def __init__(
        self,
        cartridge: Cartridge | str | Path,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        fast_ppu: bool = False,
        idle_batching: bool = True,
        device_batching: bool = True,
    ) -> None:
        self.cartridge = (
            cartridge
            if isinstance(cartridge, Cartridge)
            else Cartridge.from_file(cartridge)
        )
        self.idle_batching = bool(idle_batching)
        self.device_batching = bool(device_batching)
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        self._instruction_active = False
        self._instruction_clocked = 0
        self._instruction_start_cycle = 0
        self._interrupt_vector_selected = False
        self._dma_irq_equality_until = -1
        self.diagnostic_hot_cycles: list[int] | None = None
        self.diagnostic_literal_instructions = 0
        self.diagnostic_safe_batches = 0
        self.diagnostic_poll_batches = 0
        self.diagnostic_counter_batches = 0
        self.diagnostic_ppu_stream_batches = 0
        self.diagnostic_pcm_batches = 0
        self.diagnostic_pcm_writes = 0
        self.diagnostic_ppu_wait_batches = 0
        self.diagnostic_dmc_poll_batches = 0
        self.diagnostic_dmc_poll_fetches = 0
        self.diagnostic_device_flushes = 0
        self.diagnostic_device_flush_cycles = 0
        self.diagnostic_stall_spans = 0
        self.controller1 = Controller()
        self.controller2 = Controller()
        self.ppu = PPU(self.cartridge, fast_mode=fast_ppu)
        self.apu = APU(sample_rate)
        self.bus = Bus(
            self.cartridge,
            self.ppu,
            self.apu,
            self.controller1,
            self.controller2,
        )
        self.cpu = CPU(self.bus)
        self.cpu.external_irq_sampling = True
        self.bus.sync_cpu_access = self._sync_cpu_access
        self.bus.sync_interrupt_vector = self._sync_interrupt_vector
        self.bus.defer_ppu_data_write = self._defer_ppu_data_write
        self.apu.dmc_reader = self.bus.read_dmc
        self.powered = False
        self.power_on()

    def enable_diagnostics(self) -> None:
        """Enable low-overhead scheduler counters for a diagnostic run."""
        if self.diagnostic_hot_cycles is None:
            self.diagnostic_hot_cycles = [0] * 0x10000

    def _record_cpu_span(self, pc: int, cycles: int, kind: str) -> None:
        hot = self.diagnostic_hot_cycles
        if hot is None:
            return
        hot[int(pc) & 0xFFFF] += int(cycles)
        if kind == "literal":
            self.diagnostic_literal_instructions += 1
        elif kind == "safe":
            self.diagnostic_safe_batches += 1
        elif kind == "poll":
            self.diagnostic_poll_batches += 1
        elif kind == "counter":
            self.diagnostic_counter_batches += 1
        elif kind == "ppu":
            self.diagnostic_ppu_stream_batches += 1

    def power_on(self) -> None:
        self.bus.reset(clear_ram=True)
        self.ppu.reset()
        self.apu.reset()
        self.apu.dmc_reader = self.bus.read_dmc
        self.cpu.reset(cold=True)
        for _ in range(7):
            self._clock_cpu_cycle()
        self.powered = True

    def reset(self) -> None:
        self.bus.dma_stall_cycles = 0
        self.bus.dma_parity_pending = False
        self._dma_irq_equality_until = -1
        self.ppu.reset()
        self.apu.soft_reset()
        self.apu.dmc_reader = self.bus.read_dmc
        self.cpu.reset(cold=False)
        for _ in range(7):
            self._clock_cpu_cycle()

    def _clock_cpu_cycle(self) -> None:
        self.apu.step()
        if self.ppu.fast_mode:
            self.ppu.step_fast(3)
        else:
            self.ppu.step_accurate(3)
        self.bus.cpu_cycles += 1
        if not self._instruction_active:
            self._deliver_ppu_nmi()

    def _clock_cpu_cycles(self, count: int) -> None:
        if count <= 0:
            return
        self.apu.step_many(count)
        ppu = self.ppu
        if ppu.fast_mode:
            ppu.step_fast(count * 3)
            self.bus.cpu_cycles += count
            if not self._instruction_active:
                self._deliver_ppu_nmi()
            return
        ppu.step_accurate(count * 3)
        self.bus.cpu_cycles += count
        if not self._instruction_active:
            self._deliver_ppu_nmi()

    def _deliver_ppu_nmi(self) -> None:
        edge_clock = self.ppu.take_nmi_event()
        if edge_clock is None:
            return
        # Most instructions poll the interrupt inputs before their final bus
        # cycle. An edge at or after that point is not recognized until the
        # following instruction has completed.
        # The 2A03 samples near the end of phi2; one PPU-dot phase of the
        # NTSC divider remains before the nominal CPU-cycle boundary.
        poll_clock = (
            max(0, self.bus.cpu_cycles - 1) * 3
            - 1
            - int(not self.ppu.fast_mode)
        )
        self.cpu.request_nmi(
            defer=(
                self._interrupt_vector_selected
                or edge_clock >= poll_clock
            )
        )

    def _sync_cpu_access(self, access_cycle: int) -> None:
        """Clock devices to a register access inside the current instruction."""
        if not self._instruction_active:
            if self._pending_cpu_cycles:
                self._flush_pending_devices()
            return
        target = max(self._instruction_clocked, int(access_cycle))
        delta = target - self._instruction_clocked
        pending = self._pending_cpu_cycles
        count = pending + delta
        if count:
            # The pending CPU-only span is already reflected in
            # ``bus.cpu_cycles``; ``delta`` is the portion of this instruction
            # before the timed access. Clock both as one device span instead
            # of entering APU and PPU twice for every $2007/mapper write.
            # Clear first because an active DMC fetch can re-enter the bus.
            self._pending_cpu_cycles = 0
            self._device_batch_limit = 0
            self.apu.step_many(count)
            if self.ppu.fast_mode:
                self.ppu.step_fast(count * 3)
            else:
                self.ppu.step_accurate(count * 3)
            self.bus.cpu_cycles += delta
            self._instruction_clocked = target
        # The final device access normally follows the CPU's interrupt poll.
        # Sample before a status read can acknowledge the IRQ source.
        self._sample_irq(self._instruction_start_cycle + target)

    def _sync_interrupt_vector(self, access_cycle: int) -> bool:
        """Clock to vector selection and consume an NMI that hijacks it."""
        target = max(self._instruction_clocked, int(access_cycle))
        delta = target - self._instruction_clocked
        pending = self._pending_cpu_cycles
        count = pending + delta
        if count:
            self._pending_cpu_cycles = 0
            self._device_batch_limit = 0
            self.apu.step_many(count)
            if self.ppu.fast_mode:
                self.ppu.step_fast(count * 3)
            else:
                self.ppu.step_accurate(count * 3)
            self.bus.cpu_cycles += delta
            self._instruction_clocked = target
        self._interrupt_vector_selected = True
        return self.ppu.take_nmi_event() is not None

    def _defer_ppu_data_write(self, value: int) -> bool:
        """Commit an unobservable MMC2 forced-blank PPUDATA write directly."""
        if not self._instruction_active or not self._can_defer_ppu_data():
            return False

        address = self.ppu.v & 0x3FFF
        # MMC2 CHR latches are read-triggered, but CHR-space writes may still
        # target RAM on non-production/homebrew boards. Keep those uncommon
        # writes on the conservative synchronized path.
        if address < 0x2000:
            return False

        target = max(
            self._instruction_clocked,
            int(self.bus.cpu_access_cycle),
        )
        clocks = (
            self._pending_cpu_cycles
            + target
            - self._instruction_clocked
        )
        # Never move a register side effect across the next PPU/APU/DMC
        # deadline. Equality is kept conservative because the event and CPU
        # write can share a cycle boundary.
        if (
            clocks <= 0
            or self._device_batch_limit <= 0
            or clocks >= self._device_batch_limit
        ):
            return False

        self.ppu.write_data_deferred(
            value,
            self.ppu.master_clock + clocks * 3,
        )
        return True

    def _can_defer_ppu_data(self) -> bool:
        return bool(
            self.device_batching
            and self.ppu.fast_mode
            and self.cartridge.mapper_number == 9
            # Production PxROM uses CHR ROM. A CHR-RAM homebrew can observe
            # pattern writes immediately, so retain its literal timed path.
            and not self.cartridge.chr_is_ram
            and not self.ppu._rendering_active
            and not (self.ppu.mask & 0x18)
            and self.ppu._rendering_change_clock is None
            and not self.apu.cpu_stall_cycles
            and not self.apu.dmc.bytes_remaining
            and self.apu.dmc.sample_buffer is None
        )

    def _begin_instruction(self) -> None:
        self._instruction_start_cycle = self.bus.cpu_cycles
        self.bus.cpu_instruction_start_cycle = self._instruction_start_cycle
        self.bus.cpu_instruction_active = True
        self._instruction_clocked = 0
        self._interrupt_vector_selected = False
        self._instruction_active = True

    def _finish_instruction(self, cycles: int) -> int:
        remaining = int(cycles) - self._instruction_clocked
        if remaining < 0:
            raise AssertionError("CPU access exceeded instruction timing")
        return remaining

    def _sample_irq(self, poll_cycle: int) -> None:
        cpu = self.cpu
        if cpu._irq_sampled or cpu._irq_inhibit:
            return
        assert_cycle = self.bus.irq_assert_cycle
        # APU/mapper assertion timestamps denote the end of that CPU cycle.
        # An interrupt poll occurring in the same numbered cycle has already
        # sampled the pin; the new level is visible at the next poll.
        if (
            assert_cycle is not None
            and (
                assert_cycle < poll_cycle
                or (
                    assert_cycle == poll_cycle
                    and poll_cycle <= self._dma_irq_equality_until
                )
            )
        ):
            cpu.latch_irq()

    def _end_instruction(self, cycles: int) -> None:
        poll_cycle = self._instruction_start_cycle + int(cycles) - 1
        opcode = self.cpu.last_opcode
        if (
            opcode in (0x10, 0x30, 0x50, 0x70, 0x90, 0xB0, 0xD0, 0xF0)
            and cycles == 3
        ):
            # A taken branch that stays on the same page polls before its
            # final cycle, so a late IRQ waits through the next instruction.
            poll_cycle -= 1
        self._sample_irq(poll_cycle)
        self._instruction_active = False
        self.bus.cpu_instruction_active = False
        self._deliver_ppu_nmi()

    def _commit_cpu_batch(self, cycles: int) -> None:
        """Commit a CPU-only batch and sample an event at its final boundary."""
        self._pending_cpu_cycles += cycles
        self.bus.cpu_cycles += cycles
        if self._pending_cpu_cycles < self._device_batch_limit:
            return

        self._flush_pending_devices()
        final_cycles = self.cpu._last_batch_instruction_cycles
        if final_cycles <= 0:
            return
        poll_cycle = self.bus.cpu_cycles - 1
        if (
            self.cpu.last_opcode
            in (0x10, 0x30, 0x50, 0x70, 0x90, 0xB0, 0xD0, 0xF0)
            and final_cycles == 3
        ):
            poll_cycle -= 1
        self._sample_irq(poll_cycle)

    def _commit_cpu_driven_pcm_batch(
        self,
        cycles: int,
        events: tuple[tuple[int, int], ...],
    ) -> None:
        """Clock a recognized software-DAC span through every exact write."""
        pending = self._pending_cpu_cycles
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        elapsed = int(events[-1][0])
        device_advance = self.apu.step_many_with_dac_events(
            pending,
            events,
        )
        # $4011 has no PPU-visible side effect. The scheduler already bounded
        # the complete span before its next PPU event, so one PPU advance is
        # identical to entering step_fast once for every software sample.
        self.ppu.step_fast(device_advance * 3)
        self.bus.cpu_cycles += elapsed

        final_value = int(events[-1][1]) & 0xFF
        self.bus.apu.cpu_write_cycle = self.bus.cpu_cycles + 1
        self.bus.apu.cpu_write_address = 0x4011
        self.bus.open_bus = final_value

        remaining = int(cycles) - elapsed
        self._pending_cpu_cycles = remaining
        self.bus.cpu_cycles += remaining
        # The final DAC write can land on the vblank/NMI deadline. Ordinary
        # instruction commits consume that queued edge after the instruction
        # poll; the merged path must do the same before its next CPU span.
        self._deliver_ppu_nmi()
        self._refresh_device_batch_limit()

    def _batch_ppu_status_wait(self, max_cycles: int) -> int:
        """Collapse pre-vblank ``LDA $2002 / BPL`` polling iterations."""
        cpu = self.cpu
        ppu = self.ppu
        bus = self.bus
        pc = cpu.pc
        if (
            max_cycles < 7
            or pc < 0x8000
            or cpu.halted
            or cpu.nmi_pending
            or cpu._irq_sampled
            or not cpu._irq_inhibit
            or ppu.status & 0x80
            or ppu.rendering_enabled
            or self.apu.cpu_stall_cycles
            or self.apu.dmc.bytes_remaining
            or self.apu.dmc.sample_buffer is not None
        ):
            return 0
        peek = bus.peek_code
        if (
            peek(pc) != 0xAD
            or peek((pc + 1) & 0xFFFF) != 0x02
            or peek((pc + 2) & 0xFFFF) != 0x20
            or peek((pc + 3) & 0xFFFF) != 0x10
            or peek((pc + 4) & 0xFFFF) != 0xFB
        ):
            return 0

        repeats = max_cycles // 7
        if repeats <= 0:
            return 0
        cycles = repeats * 7
        # LDA's status-register read is its fourth bus cycle.  Earlier reads
        # only re-clear an already-clear vblank bit and reset the same write
        # toggle; replay the final one at its exact clock.
        read_offset = cycles - 4
        before_read = self._pending_cpu_cycles + read_offset
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        self.apu.step_many(before_read)
        ppu.step_fast(before_read * 3)
        bus.cpu_cycles += read_offset
        value = ppu.cpu_read_register(0x2002)

        cpu.a = value
        cpu.p = (
            (cpu.p & 0x7D)
            | 0x20
            | (0x02 if value == 0 else 0)
            | (value & 0x80)
        )
        cpu.pc = pc
        cpu.last_opcode = 0x10
        cpu.total_cycles += cycles
        cpu._last_batch_instruction_cycles = 3
        bus.open_bus = 0xFB

        remaining = cycles - read_offset
        self._pending_cpu_cycles = remaining
        bus.cpu_cycles += remaining
        self._refresh_device_batch_limit()
        if self.diagnostic_hot_cycles is not None:
            self.diagnostic_ppu_wait_batches += 1
            self._record_cpu_span(pc, cycles, "poll")
        return cycles

    def _batch_ppu_sprite_hit_wait(self, max_cycles: int) -> int:
        """Collapse a ``$2002`` sprite-zero wait without crossing its edge.

        Several MMC2 games synchronize split-screen scroll writes by first
        waiting for sprite-zero hit to clear and then waiting for it to set::

            LDA $2002
            AND #$40
            BNE/BEQ loop

        A literal implementation enters the CPU, bus, APU, and PPU dispatch
        paths three times per nine-cycle iteration. Punch-Out can execute
        roughly one thousand iterations per frame. The fast PPU already
        knows the first dot on the current scanline where the hit can change,
        so replay only the final still-taken status read before that edge.
        The edge-crossing iteration remains literal.
        """
        cpu = self.cpu
        ppu = self.ppu
        bus = self.bus
        pc = cpu.pc
        if (
            max_cycles < 9
            or pc < 0x8000
            or cpu.halted
            or cpu.nmi_pending
            or cpu._irq_sampled
            or not cpu._irq_inhibit
        ):
            return 0

        peek = bus.peek_code
        branch_opcode = peek((pc + 5) & 0xFFFF)
        if (
            peek(pc) != 0xAD
            or peek((pc + 1) & 0xFFFF) != 0x02
            or peek((pc + 2) & 0xFFFF) != 0x20
            or peek((pc + 3) & 0xFFFF) != 0x29
            or peek((pc + 4) & 0xFFFF) != 0x40
            or branch_opcode not in (0xD0, 0xF0)
            or peek((pc + 6) & 0xFFFF) != 0xF9
        ):
            return 0

        hit_set = bool(ppu.status & 0x40)
        waits_while_set = branch_opcode == 0xD0
        if hit_set != waits_while_set:
            # This is the loop's exiting iteration. Execute it literally so
            # the following scroll writes retain their ordinary timing.
            return 0

        rendering = ppu.rendering_enabled
        prerender = ppu.scanline == 261
        line_length = ppu._line_length(prerender, rendering)
        dots_to_transition = line_length - ppu.cycle
        if dots_to_transition <= 0:
            return 0

        if waits_while_set:
            # Sprite flags clear at pre-render dot 1. The lines leading to
            # that edge have no transition for bit 6, so skip them as one
            # device span (APU/NMI/DMC deadlines can still shorten it).
            if ppu.scanline < 261:
                dots_to_transition = (
                    (261 - ppu.scanline) * 341
                    - ppu.cycle
                    + 1
                )
            elif prerender and ppu.cycle < 1:
                dots_to_transition = 1 - ppu.cycle
        else:
            sprite_start = int(ppu.oam[0]) + 1
            sprite_height = 16 if ppu.ctrl & 0x20 else 8
            sprite_end = min(239, sprite_start + sprite_height - 1)
            if (
                0 <= ppu.scanline < sprite_start <= 239
                and sprite_start <= sprite_end
            ):
                # Scrolling and mapper latch state evolve while preceding
                # lines render, so stop at the first possible sprite row and
                # let the exact status planner inspect that resulting state.
                dots_to_transition = (
                    (sprite_start - ppu.scanline) * 341
                    - ppu.cycle
                )
            elif (
                sprite_start <= ppu.scanline <= sprite_end
                and rendering
            ):
                # The fast status planner returns either the exact hit dot or
                # a conservative candidate dot that triggers composition.
                # If this sprite row cannot hit, its line boundary is the
                # next checkpoint before trying the following row.
                _overflow_cycle, hit_cycle = ppu._fast_status_cycles(
                    rendering
                )
                if hit_cycle and ppu.cycle < hit_cycle:
                    dots_to_transition = min(
                        dots_to_transition,
                        hit_cycle - ppu.cycle,
                    )
                elif ppu.cycle < 256:
                    # Latch-based CHR mappers may discover the definitive
                    # overlap only when the ordered scanline fetch is
                    # committed at dot 256. Never replay a still-taken read
                    # across that fallback assertion point.
                    dots_to_transition = min(
                        dots_to_transition,
                        256 - ppu.cycle,
                    )

        # A status read is the fourth cycle of each nine-cycle iteration.
        # Device clocks currently trail the CPU by _pending_cpu_cycles, so
        # only replay reads whose synchronized clock is strictly before the
        # next possible flag transition.
        clocks_before_transition = (dots_to_transition - 1) // 3
        available_before_read = (
            clocks_before_transition - self._pending_cpu_cycles
        )
        repeats = min(
            max_cycles // 9,
            (available_before_read + 6) // 9,
        )
        if repeats <= 0:
            return 0

        cycles = repeats * 9
        read_offset = cycles - 6
        before_read = self._pending_cpu_cycles + read_offset
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        self.apu.step_many(before_read)
        ppu.step_fast(before_read * 3)
        bus.cpu_cycles += read_offset
        value = ppu.cpu_read_register(0x2002)

        result = value & 0x40
        cpu.a = result
        cpu.p = (
            (cpu.p & 0x7D)
            | 0x20
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
        cpu.pc = pc
        cpu.last_opcode = branch_opcode
        cpu.total_cycles += cycles
        cpu._last_batch_instruction_cycles = 3
        bus.open_bus = 0xF9

        remaining = cycles - read_offset
        self._pending_cpu_cycles = remaining
        bus.cpu_cycles += remaining
        self._refresh_device_batch_limit()
        if self.diagnostic_hot_cycles is not None:
            self.diagnostic_ppu_wait_batches += 1
            self._record_cpu_span(pc, cycles, "poll")
        return cycles

    def _batch_ppu_config_status_wait(self, max_cycles: int) -> int:
        """Collapse a forced-blank PPU setup/status polling loop."""
        cpu = self.cpu
        ppu = self.ppu
        bus = self.bus
        pc = cpu.pc
        if (
            max_cycles < 19
            or pc < 0x8000
            or cpu.halted
            or cpu.nmi_pending
            or cpu._irq_sampled
            or not cpu._irq_inhibit
            or ppu.status & 0x80
            or ppu.rendering_enabled
            or self.apu.cpu_stall_cycles
            or self.apu.dmc.bytes_remaining
            or self.apu.dmc.sample_buffer is not None
        ):
            return 0
        peek = bus.peek_code
        if peek(pc) != 0xA9:
            return 0
        ctrl_value = peek((pc + 1) & 0xFFFF)
        mask_value = peek((pc + 6) & 0xFFFF)
        if (
            ctrl_value is None
            or ctrl_value & 0x80
            or peek((pc + 2) & 0xFFFF) != 0x8D
            or peek((pc + 3) & 0xFFFF) != 0x00
            or peek((pc + 4) & 0xFFFF) != 0x20
            or peek((pc + 5) & 0xFFFF) != 0xA9
            or mask_value is None
            or mask_value & 0x18
            or peek((pc + 7) & 0xFFFF) != 0x8D
            or peek((pc + 8) & 0xFFFF) != 0x01
            or peek((pc + 9) & 0xFFFF) != 0x20
            or peek((pc + 10) & 0xFFFF) != 0xAD
            or peek((pc + 11) & 0xFFFF) != 0x02
            or peek((pc + 12) & 0xFFFF) != 0x20
            or peek((pc + 13) & 0xFFFF) != 0x10
            or peek((pc + 14) & 0xFFFF) != 0xF1
        ):
            return 0

        repeats = max_cycles // 19
        if repeats <= 0:
            return 0
        cycles = repeats * 19
        final_iteration = cycles - 19
        ctrl_offset = final_iteration + 5
        mask_offset = final_iteration + 11
        read_offset = final_iteration + 15

        before_ctrl = self._pending_cpu_cycles + ctrl_offset
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        self.apu.step_many(before_ctrl)
        ppu.step_fast(before_ctrl * 3)
        bus.cpu_cycles += ctrl_offset
        ppu.cpu_write_register(0x2000, ctrl_value)

        delta = mask_offset - ctrl_offset
        self.apu.step_many(delta)
        ppu.step_fast(delta * 3)
        bus.cpu_cycles += delta
        ppu.cpu_write_register(0x2001, mask_value)

        delta = read_offset - mask_offset
        self.apu.step_many(delta)
        ppu.step_fast(delta * 3)
        bus.cpu_cycles += delta
        value = ppu.cpu_read_register(0x2002)

        cpu.a = value
        cpu.p = (
            (cpu.p & 0x7D)
            | 0x20
            | (0x02 if value == 0 else 0)
            | (value & 0x80)
        )
        cpu.pc = pc
        cpu.last_opcode = 0x10
        cpu.total_cycles += cycles
        cpu._last_batch_instruction_cycles = 3
        bus.open_bus = 0xF1

        remaining = cycles - read_offset
        self._pending_cpu_cycles = remaining
        bus.cpu_cycles += remaining
        self._refresh_device_batch_limit()
        if self.diagnostic_hot_cycles is not None:
            self.diagnostic_ppu_wait_batches += 1
            self._record_cpu_span(pc, cycles, "poll")
        return cycles

    def _drain_stalls(self) -> int:
        cycles = 0
        if (
            self.diagnostic_hot_cycles is not None
            and (
                self.bus.dma_parity_pending
                or self.bus.dma_stall_cycles
                or self.apu.cpu_stall_cycles
            )
        ):
            self.diagnostic_stall_spans += 1
        oam_active = bool(
            self.bus.dma_parity_pending
            or self.bus.dma_stall_cycles > 0
        )
        if self.bus.dma_parity_pending:
            self.bus.dma_stall_cycles = 512 + (self.bus.cpu_cycles & 1)
            self.bus.dma_parity_pending = False
        while self.bus.dma_stall_cycles > 0 or self.apu.cpu_stall_cycles > 0:
            if self.bus.dma_stall_cycles > 0:
                count = self.bus.dma_stall_cycles
                self.bus.dma_stall_cycles = 0
                # DMC and OAM have independent DMA units. A DMC get has
                # priority over an OAM get, but its halt/dummy/alignment
                # cycles run while OAM already owns the CPU. Consequently an
                # overlapping four-cycle DMC request lengthens OAM by only
                # two cycles rather than being serialized after all 512
                # transfer cycles.
                #
                # A request on either of the final two instruction cycles is
                # still in its halt/dummy phase when OAM begins. Earlier
                # requests have completed and retain their full four-cycle
                # CPU hold.
                dmc_stalls_before = self.apu.cpu_stall_cycles
                dmc = self.apu.dmc
                fetch_cycle = dmc.last_fetch_cycle
                if (
                    dmc_stalls_before >= dmc.last_fetch_stall > 0
                    and fetch_cycle is not None
                    and fetch_cycle >= self.apu.cycle - 1
                ):
                    overlap = dmc.last_fetch_stall - 2
                    self.apu.cpu_stall_cycles -= overlap
                    dmc_stalls_before -= overlap
                fetch_count_before = dmc.fetch_count
                oam_start_cycle = self.apu.cycle
                self._clock_cpu_cycles(count)
                cycles += count

                # Requests raised while OAM was active overlap in the same
                # way. Convert each newly queued request to the two cycles by
                # which it normally extends the transfer. The two final OAM
                # slots are hardware edge cases measured by the public DMA
                # diagnostics: next-to-next-to-last adds one cycle, while a
                # request on the last transfer cycle adds three.
                new_requests = dmc.fetch_count - fetch_count_before
                if new_requests > 0:
                    extension = new_requests * 2
                    last_fetch_cycle = dmc.last_fetch_cycle
                    if last_fetch_cycle is not None:
                        offset = last_fetch_cycle - oam_start_cycle
                        if offset == count - 2:
                            extension -= 1
                        elif offset == count:
                            extension += 1
                    self.apu.cpu_stall_cycles = (
                        dmc_stalls_before + extension
                    )
                continue
            else:
                count = self.apu.cpu_stall_cycles
                self.apu.cpu_stall_cycles = 0
            # The CPU cannot observe an instruction boundary while held by
            # OAM or DMC DMA. Advance the same clocks as one exact span rather
            # than returning through Python for every one of the usual
            # 513/514 OAM-DMA cycles. DMC fetches that occur inside the span
            # append fresh stall cycles, which the loop drains next.
            self._clock_cpu_cycles(count)
            cycles += count
        if oam_active:
            # RDY releases the CPU back into the read it held while OAM DMA
            # owned the bus. For the next three polling cycles, an IRQ edge
            # coincident with the poll is visible on that resumed read phase
            # (rather than waiting through one more instruction as it does
            # during ordinary execution).
            self._dma_irq_equality_until = self.bus.cpu_cycles + 3
        return cycles

    def step_instruction(self) -> int:
        cycles = self._drain_stalls()
        if (
            self.cpu._banked_batch_tables is not None
            and self.bus._mapped_prg_window
            is not self.cpu._active_mapped_prg
        ):
            self.cpu._refresh_banked_fast_tables()
        if self.cpu._segmented_batch_tables is not None:
            mapped_banks = self.bus._mapped_prg_banks
            refresh_segmented = (
                mapped_banks is not self.cpu._active_mapped_prg_banks
            )
            if (
                not refresh_segmented
                and mapped_banks is not None
                and self.cpu.pc >= 0x8000
            ):
                safe_view = self.cpu._segmented_safe_view
                assert safe_view is not None
                refresh_segmented = safe_view.slots[
                    (self.cpu.pc - 0x8000) >> 13
                ] is None
            if refresh_segmented:
                # A stable bank still needs a second observation when code
                # first enters one of its previously unused 8 KiB slots.
                self.cpu._refresh_segmented_fast_tables()
        self._begin_instruction()
        instruction_cycles = self.cpu.step()
        self._clock_cpu_cycles(self._finish_instruction(instruction_cycles))
        self._end_instruction(instruction_cycles)
        return cycles + instruction_cycles

    def run_frame(self, *, copy_frame: bool = True) -> bytes | bytearray:
        ppu = self.ppu
        apu = self.apu
        cpu = self.cpu
        bus = self.bus
        cpu.begin_translated_block_frame()
        ppu.frame_complete = False

        if ppu.fast_mode and self.device_batching:
            return self._run_frame_batched_devices(copy_frame=copy_frame)

        if ppu.fast_mode:
            cpu_step = cpu.step
            apu_step_many = apu.step_many
            ppu_step_fast = ppu.step_fast
            while not ppu.frame_complete:
                if (
                    bus.dma_parity_pending
                    or bus.dma_stall_cycles
                    or apu.cpu_stall_cycles
                ):
                    self._drain_stalls()
                    continue
                instruction_pc = cpu.pc
                self._begin_instruction()
                cycles = cpu_step()
                remaining = self._finish_instruction(cycles)
                apu_step_many(remaining)
                ppu_step_fast(remaining * 3)
                bus.cpu_cycles += remaining
                self._end_instruction(cycles)
                if (
                    self.idle_batching
                    and cycles == 3
                    and cpu.last_opcode == 0x4C
                    and cpu.pc == instruction_pc
                    and instruction_pc >= 0x8000
                    and not ppu.frame_complete
                    and not cpu.nmi_pending
                    and not cpu._irq_sampled
                    and (cpu._irq_inhibit or not bus.irq_pending)
                    and not bus.dma_parity_pending
                    and not bus.dma_stall_cycles
                    and not apu.cpu_stall_cycles
                    and not apu.dmc.bytes_remaining
                    and apu.dmc.sample_buffer is None
                ):
                    self._batch_cartridge_self_jump()
            return ppu.take_frame(copy=copy_frame)

        cpu_step = cpu.step
        apu_step_many = apu.step_many
        ppu_step_accurate = ppu.step_accurate
        while not ppu.frame_complete:
            if (
                bus.dma_parity_pending
                or bus.dma_stall_cycles
                or apu.cpu_stall_cycles
            ):
                self._drain_stalls()
                continue
            self._begin_instruction()
            cycles = cpu_step()
            remaining = self._finish_instruction(cycles)
            apu_step_many(remaining)
            ppu_step_accurate(remaining * 3)
            bus.cpu_cycles += remaining
            self._end_instruction(cycles)
        return ppu.take_frame(copy=copy_frame)

    def _flush_pending_devices(self) -> None:
        """Apply clocks deferred across CPU-only RAM/ROM instructions."""
        count = self._pending_cpu_cycles
        # A register access may change rendering or APU-frame timing after this
        # pre-access callback returns, so recalculate the deadline before the
        # next CPU-only span even when there were no pending clocks.
        self._device_batch_limit = 0
        if count <= 0:
            return
        if self.diagnostic_hot_cycles is not None:
            self.diagnostic_device_flushes += 1
            self.diagnostic_device_flush_cycles += count
        # Clear first because an active DMC read re-enters the CPU bus.
        self._pending_cpu_cycles = 0
        self.apu.step_many(count)
        self.ppu.step_fast(count * 3)
        if not self._instruction_active:
            self._deliver_ppu_nmi()

    def _dots_to_next_fast_event(self) -> int:
        """Return the next PPU boundary observable by the fast scheduler."""
        ppu = self.ppu
        cycle = ppu.cycle
        scanline = ppu.scanline
        if (
            not ppu._rendering_active
            and not (ppu.mask & 0x18)
            and ppu._rendering_change_clock is None
            and self.cartridge.mapper_number != 4
        ):
            if 0 <= scanline < 240:
                return (240 - scanline) * 341 - cycle
            if scanline == 241 and cycle < 1:
                return 1 - cycle
            return 341 - cycle
        rendering = ppu.rendering_enabled
        prerender = scanline == 261
        line_length = ppu._line_length(prerender, rendering)
        next_cycle = line_length
        dots: int | None = None
        if 0 <= scanline < 240:
            # The fast PPU processes scanline composition, sprite flags, and
            # mapper address-bus events internally. Let it advance to frame
            # completion unless an enabled MMC3 counter can assert sooner.
            # CPU register accesses still synchronize at their exact cycle.
            dots = (240 - scanline) * 341 - cycle
        elif scanline == 241 and cycle < 1:
            # NMI must be delivered at the first following CPU instruction
            # boundary, so vblank remains an explicit scheduler deadline.
            next_cycle = 1
        if (
            self.cartridge.mapper_number == 4
            and rendering
            and (0 <= scanline < 240 or prerender)
            and self.cartridge.mapper.irq_enabled
            and not self.cartridge.mapper.irq_pending
        ):
            # Count forward without mutating the mapper and stop only at the
            # A12 edge which can actually assert IRQ. Previous releases
            # stopped at all three candidate phases on all 240 scanlines,
            # reducing hot MMC3 CPU batches to only a few dozen cycles.
            irq_dots = ppu.dots_to_mmc3_irq_assertion()
            if irq_dots is not None:
                assert dots is not None
                dots = min(dots, irq_dots)
            elif prerender:
                # The predictor deliberately spans only visible lines. Keep
                # the three pre-render address phases explicit so an IRQ
                # asserted there is sampled before another instruction can
                # begin. This costs at most three boundaries per frame.
                for event_cycle in (4, 260, 324):
                    if cycle < event_cycle:
                        next_cycle = min(next_cycle, event_cycle)
                        break
        if dots is None:
            dots = next_cycle - cycle
        rendering_change = ppu.dots_to_rendering_change()
        if rendering_change is not None and rendering_change > 0:
            dots = min(dots, rendering_change)
        return dots

    def _cycles_to_next_apu_frame_event(self) -> int:
        frame_cycle = self.apu.frame_cycle
        reset_delay = self.apu._frame_counter_reset_delay
        if frame_cycle < FRAME_Q1:
            event = FRAME_Q1 - frame_cycle
        elif frame_cycle < FRAME_Q2:
            event = FRAME_Q2 - frame_cycle
        elif frame_cycle < FRAME_Q3:
            event = FRAME_Q3 - frame_cycle
        elif self.apu.five_step:
            if frame_cycle < FRAME_5_CLOCK:
                event = FRAME_5_CLOCK - frame_cycle
            else:
                event = FRAME_5_END - frame_cycle
        elif frame_cycle < FRAME_4_IRQ_START:
            event = FRAME_4_IRQ_START - frame_cycle
        elif frame_cycle < FRAME_4_CLOCK:
            event = FRAME_4_CLOCK - frame_cycle
        else:
            event = FRAME_4_END - frame_cycle
        if reset_delay:
            event = min(event, reset_delay)
        return event

    def _refresh_device_batch_limit(self) -> None:
        dots = self._dots_to_next_fast_event()
        limit = max(
            1,
            min(
                (dots + 2) // 3,
                self._cycles_to_next_apu_frame_event(),
            ),
        )
        dmc_fetch = self.apu.cycles_to_dmc_fetch()
        if dmc_fetch is not None:
            limit = min(limit, dmc_fetch)
        self._device_batch_limit = limit

    def _batch_dmc_ram_poll(self) -> int:
        """Advance a read-only RAM wait across several DMC DMA requests.

        A DMC request normally becomes a scheduler deadline because RDY can
        delay a write and because an interrupt may be observed between the
        surrounding instructions.  Neither condition applies while the CPU
        repeats an already-classified RAM-read/branch loop with interrupts
        unchanged.  Execute whole loop iterations, advance APU/PPU time once,
        and include every four-cycle DMC hold in the physical clock span.
        """
        if self.cartridge.mapper_number != 9:
            return 0
        cpu = self.cpu
        apu = self.apu
        dmc = apu.dmc
        starts = cpu._poll_loop_starts
        pc = cpu.pc
        if (
            starts is None
            or pc < 0x8000
            or not starts[pc]
            or dmc.bytes_remaining <= 0
            or dmc.irq_enabled
            or apu.cpu_stall_cycles
            or self.bus.dma_parity_pending
            or self.bus.dma_stall_cycles
            or cpu.halted
            or cpu.nmi_pending
            or cpu._irq_sampled
            or (not cpu._irq_inhibit and self.bus.irq_pending)
        ):
            return 0

        if self._pending_cpu_cycles:
            self._flush_pending_devices()
            if apu.cpu_stall_cycles:
                return 0

        # PPU/NMI and APU-frame edges remain ordinary instruction-boundary
        # deadlines. Leave one physical cycle before the closest edge so no
        # collapsed branch can sample a newly asserted line.
        physical_budget = min(
            max(1, (self._dots_to_next_fast_event() + 2) // 3),
            self._cycles_to_next_apu_frame_event(),
        ) - 1
        if physical_budget < 12:
            return 0

        first_fetch = apu.cycles_to_dmc_fetch()
        if first_fetch is None or first_fetch > physical_budget:
            return 0
        fetch_interval = DMC_PERIODS[dmc.rate_index] * 8
        maximum_fetches = (
            1
            + max(0, physical_budget - first_fetch) // fetch_interval
        )
        if not dmc.loop:
            maximum_fetches = min(maximum_fetches, dmc.bytes_remaining)
        cpu_budget = physical_budget - maximum_fetches * 4
        if cpu_budget < 6:
            return 0

        # The classifier admits only a load/compare/BIT from mirrored internal
        # RAM followed by a taken branch to itself. DMC cannot change that
        # value, so the branch result is invariant throughout this span.
        cycles = cpu.batch_poll_loop(cpu_budget)
        if cycles <= 0:
            return 0

        fetches_before = dmc.fetch_count
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        apu.step_many(cycles)
        stall_cycles = apu.cpu_stall_cycles
        apu.cpu_stall_cycles = 0
        physical_cycles = cycles
        while stall_cycles:
            physical_cycles += stall_cycles
            apu.step_many(stall_cycles)
            stall_cycles = apu.cpu_stall_cycles
            apu.cpu_stall_cycles = 0
        if physical_cycles > physical_budget:
            raise AssertionError("DMC RAM-poll batch crossed its device edge")

        self.ppu.step_fast(physical_cycles * 3)
        self.bus.cpu_cycles += physical_cycles
        self._deliver_ppu_nmi()
        self._record_cpu_span(pc, cycles, "poll")
        if self.diagnostic_hot_cycles is not None:
            fetches = dmc.fetch_count - fetches_before
            self.diagnostic_dmc_poll_batches += 1
            self.diagnostic_dmc_poll_fetches += fetches
            self.diagnostic_stall_spans += fetches
        self._refresh_device_batch_limit()
        return physical_cycles

    def _run_frame_batched_devices(
        self,
        *,
        copy_frame: bool = True,
    ) -> bytes | bytearray:
        """Run a fast frame while coalescing unobservable device clocks.

        CPU instructions still execute and sample interrupt lines one at a
        time. APU and PPU clocks are coalesced only until the next PPU/APU
        event, device-register access, DMA/DMC stall, or idle-loop batch.
        """
        ppu = self.ppu
        apu = self.apu
        cpu = self.cpu
        bus = self.bus
        cpu_step = cpu.step
        mapper_number = self.cartridge.mapper_number
        can_defer_ppu_stream = mapper_number == 9
        can_batch_cpu_dac = mapper_number == 9
        self._pending_cpu_cycles = 0
        self._device_batch_limit = 0
        bus.sync_devices = self._flush_pending_devices
        try:
            while not ppu.frame_complete:
                if (
                    bus.dma_parity_pending
                    or bus.dma_stall_cycles
                    or apu.cpu_stall_cycles
                ):
                    self._flush_pending_devices()
                    self._drain_stalls()
                    continue

                poll_starts = cpu._poll_loop_starts
                if (
                    can_batch_cpu_dac
                    and apu.dmc.bytes_remaining
                    and poll_starts is not None
                    and poll_starts[cpu.pc]
                    and self._batch_dmc_ram_poll()
                ):
                    continue

                if self._device_batch_limit <= 0:
                    self._refresh_device_batch_limit()
                remaining_batch_cycles = (
                    self._device_batch_limit - self._pending_cpu_cycles
                )
                if (
                    cpu._banked_batch_tables is not None
                    and bus._mapped_prg_window
                    is not cpu._active_mapped_prg
                ):
                    cpu._refresh_banked_fast_tables()
                if cpu._segmented_batch_tables is not None:
                    mapped_banks = bus._mapped_prg_banks
                    refresh_segmented = (
                        mapped_banks is not cpu._active_mapped_prg_banks
                    )
                    if (
                        not refresh_segmented
                        and mapped_banks is not None
                        and cpu.pc >= 0x8000
                    ):
                        safe_view = cpu._segmented_safe_view
                        assert safe_view is not None
                        refresh_segmented = safe_view.slots[
                            (cpu.pc - 0x8000) >> 13
                        ] is None
                    if refresh_segmented:
                        # The bank tuple can remain unchanged while control
                        # flow enters a new slot. Complete that slot's
                        # stability gate instead of leaving it literal.
                        cpu._refresh_segmented_fast_tables()
                safe_starts = cpu._safe_batch_starts
                safe_kind = (
                    0 if safe_starts is None else safe_starts[cpu.pc]
                )
                # Mapper 9's stream/DAC executors intentionally join safe
                # work across timed writes and therefore retain the ordering
                # below. Every other mapper can dispatch an ordinary safe
                # block immediately: dedicated counter/poll/device entries
                # are already zeroed in the immutable classifier tables.
                if (
                    mapper_number != 9
                    and remaining_batch_cycles >= 4
                    and safe_kind != 0
                ):
                    span_pc = cpu.pc
                    cycles = cpu.batch_safe_instructions(
                        remaining_batch_cycles
                    )
                    if cycles:
                        self._record_cpu_span(span_pc, cycles, "safe")
                        self._commit_cpu_batch(cycles)
                        continue
                if (
                    self.idle_batching
                    and remaining_batch_cycles >= 5
                ):
                    counter_starts = cpu._counter_loop_opcodes
                    poll_starts = cpu._poll_loop_starts
                    pc = cpu.pc
                    cycles = 0
                    if counter_starts is None or counter_starts[pc]:
                        cycles = cpu.batch_counter_loop(
                            remaining_batch_cycles
                        )
                        if cycles:
                            self._record_cpu_span(pc, cycles, "counter")
                    if (
                        not cycles
                        and poll_starts is not None
                        and poll_starts[pc]
                    ):
                        cycles = cpu.batch_poll_loop(
                            remaining_batch_cycles
                        )
                        if cycles:
                            self._record_cpu_span(pc, cycles, "poll")
                    if cycles:
                        self._commit_cpu_batch(cycles)
                        continue
                # Inspect the first immutable opcode once and dispatch only
                # the matching PPU polling specialist. The vblank/setup
                # loops require forced blank, while sprite-zero split waits
                # are specifically useful during active rendering.
                if (
                    remaining_batch_cycles >= 7
                    and cpu.pc >= 0x8000
                ):
                    wait_opcode = bus.peek_code(cpu.pc)
                    if (
                        wait_opcode == 0xA9
                        and remaining_batch_cycles >= 19
                        and not (ppu.status & 0x80)
                        and not ppu.rendering_enabled
                        and self._batch_ppu_config_status_wait(
                            remaining_batch_cycles
                        )
                    ):
                        continue
                    if (
                        wait_opcode == 0xAD
                        and not (ppu.status & 0x80)
                        and not ppu.rendering_enabled
                        and self._batch_ppu_status_wait(
                            remaining_batch_cycles
                        )
                    ):
                        continue
                    if (
                        wait_opcode == 0xAD
                        and remaining_batch_cycles >= 9
                        and bus.peek_code((cpu.pc + 3) & 0xFFFF)
                        == 0x29
                        and self._batch_ppu_sprite_hit_wait(
                            remaining_batch_cycles
                        )
                    ):
                        continue
                if (
                    can_defer_ppu_stream
                    and remaining_batch_cycles >= 8
                    and self._can_defer_ppu_data()
                ):
                    span_pc = cpu.pc
                    ppu_batch = cpu.batch_deferred_ppu_stream(
                        remaining_batch_cycles
                    )
                    if ppu_batch is not None:
                        cycles, write_values, last_write_offset = ppu_batch
                        if write_values:
                            ppu.write_data_deferred_many(
                                write_values,
                                ppu.master_clock
                                + (
                                    self._pending_cpu_cycles
                                    + last_write_offset
                                )
                                * 3,
                            )
                        self._record_cpu_span(span_pc, cycles, "ppu")
                        self._commit_cpu_batch(cycles)
                        continue
                if (
                    can_batch_cpu_dac
                    and cpu.pc == 0x908D
                    and remaining_batch_cycles >= 130
                    and not apu.cpu_stall_cycles
                    and not apu.dmc.bytes_remaining
                    and apu.dmc.sample_buffer is None
                    and not cpu.nmi_pending
                    and not cpu._irq_sampled
                    and cpu._irq_inhibit
                ):
                    pcm_batch = cpu.batch_cpu_driven_pcm_sample(
                        remaining_batch_cycles
                    )
                    if pcm_batch is not None:
                        cycles, events = pcm_batch
                        if self.diagnostic_hot_cycles is not None:
                            self.diagnostic_pcm_batches += 1
                            self.diagnostic_pcm_writes += len(events)
                        self._record_cpu_span(
                            0x908D,
                            cycles,
                            "safe",
                        )
                        self._commit_cpu_driven_pcm_batch(
                            cycles,
                            events,
                        )
                        continue
                if (
                    remaining_batch_cycles >= 4
                    and safe_starts is not None
                    and safe_kind != 0
                ):
                    span_pc = cpu.pc
                    cycles = cpu.batch_safe_instructions(
                        remaining_batch_cycles
                    )
                    if cycles:
                        self._record_cpu_span(span_pc, cycles, "safe")
                        self._commit_cpu_batch(cycles)
                        continue
                instruction_pc = cpu.pc
                self._begin_instruction()
                cycles = cpu_step()
                self._record_cpu_span(instruction_pc, cycles, "literal")
                remaining = self._finish_instruction(cycles)
                self._pending_cpu_cycles += remaining
                bus.cpu_cycles += remaining
                if (
                    self._device_batch_limit > 0
                    and self._pending_cpu_cycles
                    >= self._device_batch_limit
                ):
                    # The instruction reached or crossed the next observable
                    # PPU/APU edge. Apply the deferred clocks before polling
                    # the interrupt pins at the instruction boundary.
                    #
                    # Flushing after _end_instruction() let the CPU sample a
                    # stale APU frame-IRQ line for the one instruction that
                    # straddled the edge. That was both inaccurate and made
                    # interrupt timing depend on whether device batching was
                    # enabled.
                    self._flush_pending_devices()
                self._end_instruction(cycles)
                if self._device_batch_limit <= 0:
                    # A device-register access flushed the preceding span and
                    # invalidated the deadline during cpu_step().
                    self._refresh_device_batch_limit()

                should_idle_batch = (
                    self.idle_batching
                    and cycles == 3
                    and cpu.last_opcode == 0x4C
                    and cpu.pc == instruction_pc
                    and instruction_pc >= 0x8000
                    and not cpu.nmi_pending
                    and not cpu._irq_sampled
                    and (cpu._irq_inhibit or not bus.irq_pending)
                    and not bus.dma_parity_pending
                    and not bus.dma_stall_cycles
                    and not apu.cpu_stall_cycles
                )
                if (
                    should_idle_batch
                    or bus.dma_parity_pending
                    or bus.dma_stall_cycles
                    or self._pending_cpu_cycles >= self._device_batch_limit
                ):
                    self._flush_pending_devices()

                if (
                    should_idle_batch
                    and not ppu.frame_complete
                    and not cpu.nmi_pending
                    and not cpu._irq_sampled
                    and (cpu._irq_inhibit or not bus.irq_pending)
                    and not bus.dma_parity_pending
                    and not bus.dma_stall_cycles
                    and not apu.cpu_stall_cycles
                ):
                    self._batch_cartridge_self_jump()
        finally:
            self._flush_pending_devices()
            bus.sync_devices = None
        return ppu.take_frame(copy=copy_frame)

    def _batch_cartridge_self_jump(self) -> int:
        """Fast-forward side-effect-free ``JMP $self`` iterations.

        The batch stops before the next PPU line/NMI boundary and APU frame
        event. MMC3 batches also stop before its scanline-counter A12 edge, so
        interrupts are still sampled at the same instruction boundary. PRG ROM
        reads are immutable between mapper writes, and a self-jump performs no
        such write.
        """
        ppu = self.ppu
        apu = self.apu
        cpu = self.cpu
        bus = self.bus

        prerender = ppu.scanline == 261
        line_length = ppu._line_length(
            prerender,
            ppu.rendering_enabled,
        )
        dots_to_boundary = line_length - ppu.cycle
        if (
            self.cartridge.mapper_number != 4
            and 0 <= ppu.scanline < 240
        ):
            dots_to_boundary = (240 - ppu.scanline) * 341 - ppu.cycle
        if ppu.scanline == 241 and ppu.cycle < 1:
            dots_to_boundary = min(dots_to_boundary, 1 - ppu.cycle)
        if (
            self.cartridge.mapper_number == 4
            and ppu.rendering_enabled
            and (
                0 <= ppu.scanline < 240
                or ppu.scanline == 261
            )
        ):
            for event_cycle in (4, 260, 324):
                if ppu.cycle < event_cycle:
                    dots_to_boundary = min(
                        dots_to_boundary,
                        event_cycle - ppu.cycle,
                    )
                    break
        rendering_change = ppu.dots_to_rendering_change()
        if rendering_change is not None and rendering_change > 0:
            dots_to_boundary = min(
                dots_to_boundary,
                rendering_change,
            )
        ppu_repeats = (dots_to_boundary - 1) // 9
        if ppu_repeats <= 0:
            return 0

        frame_cycle = apu.frame_cycle
        if frame_cycle < FRAME_Q1:
            next_frame_event = FRAME_Q1
        elif frame_cycle < FRAME_Q2:
            next_frame_event = FRAME_Q2
        elif frame_cycle < FRAME_Q3:
            next_frame_event = FRAME_Q3
        elif apu.five_step and frame_cycle < FRAME_5_CLOCK:
            next_frame_event = FRAME_5_CLOCK
        elif not apu.five_step and frame_cycle < FRAME_4_IRQ_START:
            next_frame_event = FRAME_4_IRQ_START
        elif not apu.five_step and frame_cycle < FRAME_4_CLOCK:
            next_frame_event = FRAME_4_CLOCK
        else:
            next_frame_event = FRAME_5_END if apu.five_step else FRAME_4_END
        if apu._frame_counter_reset_delay:
            next_frame_event = min(
                next_frame_event,
                frame_cycle + apu._frame_counter_reset_delay,
            )
        apu_repeats = (next_frame_event - frame_cycle - 1) // 3
        repeats = min(ppu_repeats, apu_repeats)
        dmc_fetch = apu.cycles_to_dmc_fetch()
        if dmc_fetch is not None:
            repeats = min(repeats, (dmc_fetch + 2) // 3)
        if repeats <= 0:
            return 0

        cycles = repeats * 3
        cpu.total_cycles += cycles
        apu.step_many(cycles)
        ppu.step_fast(cycles * 3)
        bus.cpu_cycles += cycles
        self._deliver_ppu_nmi()
        return cycles

    def run_cpu_cycles(self, minimum_cycles: int) -> int:
        elapsed = 0
        while elapsed < minimum_cycles:
            elapsed += self.step_instruction()
        return elapsed

    def set_button(self, player: int, button: Button, pressed: bool) -> None:
        if player == 1:
            self.controller1.set_button(button, pressed)
        elif player == 2:
            self.controller2.set_button(button, pressed)
        else:
            raise ValueError("player must be 1 or 2")

    def get_state(self) -> dict:
        return {
            "rom_sha256": self.cartridge.sha256,
            "cpu": self.cpu.get_state(),
            "ppu": self.ppu.get_state(),
            "apu": self.apu.get_state(),
            "bus": self.bus.get_state(),
            "cartridge": self.cartridge.get_state(),
        }

    def set_state(self, state: dict) -> None:
        if state.get("rom_sha256") != self.cartridge.sha256:
            raise StateError("save state belongs to a different cartridge image")
        # Restore memory and mapper state before CPU/PPU execution state.
        self.cartridge.set_state(state["cartridge"])
        self.bus.set_state(state["bus"])
        self.apu.set_state(state["apu"])
        self.apu.dmc_reader = self.bus.read_dmc
        self.ppu.set_state(state["ppu"])
        self.cpu.set_state(state["cpu"])

    def save_state(self, path: str | Path) -> None:
        save_atomic(path, self.get_state())

    def load_state(self, path: str | Path) -> None:
        self.set_state(loads(Path(path).read_bytes()))

    def save_battery(self, path: str | Path) -> bool:
        return self.cartridge.save_battery(path)

    def load_battery(self, path: str | Path) -> bool:
        return self.cartridge.load_battery(path)
