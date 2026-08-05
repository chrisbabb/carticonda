"""CPU address decoding for the NES main board."""

from __future__ import annotations

from collections.abc import Callable

from .apu import APU
from .cartridge import Cartridge
from .controller import Controller
from .ppu import PPU


class Bus:
    def __init__(
        self,
        cartridge: Cartridge,
        ppu: PPU,
        apu: APU,
        controller1: Controller,
        controller2: Controller,
    ) -> None:
        self.cartridge = cartridge
        self.mapper = cartridge.mapper
        self._mapper_cpu_read = self.mapper.cpu_read
        self._mapper_cpu_write = self.mapper.cpu_write_timed
        code_windows = getattr(self.mapper, "cpu_code_windows", None)
        code_window = getattr(self.mapper, "cpu_code_window", None)
        if callable(code_windows) and callable(code_window):
            self._mapped_prg_windows: tuple[bytes, ...] | None = code_windows()
            self._mapped_prg_window: bytes | None = code_window()
            self._mapped_prg_window_provider = code_window
        else:
            self._mapped_prg_windows = None
            self._mapped_prg_window = None
            self._mapped_prg_window_provider = None
        code_banks = getattr(self.mapper, "cpu_code_banks", None)
        if callable(code_banks):
            self._mapped_prg_banks: tuple[bytes, ...] | None = code_banks()
            self._mapped_prg_banks_provider = code_banks
        else:
            self._mapped_prg_banks = None
            self._mapped_prg_banks_provider = None
        if (
            cartridge.mapper_number == 0
            and len(cartridge.prg_rom) in (0x4000, 0x8000)
        ):
            self._nrom_prg = cartridge.prg_rom
            self._nrom_prg_mask = len(cartridge.prg_rom) - 1
            counter_loop_opcodes = bytearray(0x10000)
            for opcode in (0x88, 0xC8, 0xCA, 0xE8):
                pattern = bytes((opcode, 0xD0, 0xFD))
                offset = self._nrom_prg.find(pattern)
                while offset >= 0:
                    counter_loop_opcodes[0x8000 + offset] = opcode
                    if len(self._nrom_prg) == 0x4000:
                        counter_loop_opcodes[0xC000 + offset] = opcode
                    offset = self._nrom_prg.find(pattern, offset + 1)
            self.counter_loop_opcodes: bytes | None = bytes(
                counter_loop_opcodes
            )

            # Preclassify side-effect-free ``read RAM; branch back`` waits.
            # NMI-driven games commonly park the CPU in one of these loops
            # after finishing their work for the current video frame. Looking
            # them up through a byte table is cheaper than speculative ROM
            # reads at every ordinary instruction.
            poll_loop_starts = bytearray(0x10000)
            read_lengths = {
                0xA4: 2, 0xA5: 2, 0xA6: 2, 0x24: 2,
                0xC4: 2, 0xC5: 2, 0xE4: 2,
                0xAC: 3, 0xAD: 3, 0xAE: 3, 0x2C: 3,
                0xCC: 3, 0xCD: 3, 0xEC: 3,
            }
            branch_opcodes = {
                0x10, 0x30, 0x50, 0x70,
                0x90, 0xB0, 0xD0, 0xF0,
            }
            mask = self._nrom_prg_mask
            prg = self._nrom_prg

            def rom_byte(address: int) -> int:
                return prg[(address - 0x8000) & mask]

            for address in range(0x8000, 0xFFFC):
                instruction_length = read_lengths.get(rom_byte(address))
                if instruction_length is None:
                    continue
                if instruction_length == 3:
                    read_address = (
                        rom_byte(address + 1)
                        | (rom_byte(address + 2) << 8)
                    )
                    if read_address >= 0x2000:
                        continue
                branch_address = address + instruction_length
                if rom_byte(branch_address) not in branch_opcodes:
                    continue
                relative = rom_byte(branch_address + 1)
                if relative & 0x80:
                    relative -= 0x100
                branch_base = (branch_address + 2) & 0xFFFF
                if (branch_base + relative) & 0xFFFF == address:
                    poll_loop_starts[address] = 1
            self.poll_loop_starts: bytes | None = bytes(poll_loop_starts)
        else:
            self._nrom_prg = None
            self._nrom_prg_mask = 0
            self.counter_loop_opcodes = None
            self.poll_loop_starts = None
        self.ppu = ppu
        self.apu = apu
        self.controller1 = controller1
        self.controller2 = controller2
        self.ram = bytearray(0x0800)
        self.open_bus = 0
        self.cpu_cycles = 0
        self.dma_stall_cycles = 0
        self.dma_parity_pending = False
        # The fast scheduler may defer APU/PPU clocks across ordinary RAM and
        # ROM-only instructions. Device-register accesses call this hook first
        # so their side effects still occur at the same instruction boundary.
        self.sync_devices: Callable[[], None] | None = None
        # During an instruction, the CPU publishes the zero-based bus-cycle
        # offset of its next timed access. The Console uses this second hook
        # to bring PPU/APU clocks to the exact access cycle rather than
        # applying every register side effect at instruction start.
        self.cpu_access_cycle = 0
        self.cpu_instruction_start_cycle = 0
        self.cpu_instruction_active = False
        self.sync_cpu_access: Callable[[int], None] | None = None
        # BRK and hardware-interrupt entry select their vector partway through
        # the seven-cycle sequence. The Console clocks to that point and
        # reports whether a newly asserted NMI hijacked the lower-priority
        # IRQ/BRK vector.
        self.sync_interrupt_vector: Callable[[int], bool] | None = None
        # The fast Console can commit a forced-blank PPUDATA write at its
        # exact logical PPU clock without first entering both device
        # steppers. The hook returns true only when doing so is invisible to
        # CPU-observable PPU/APU/DMA timing.
        self.defer_ppu_data_write: Callable[[int], bool] | None = None

    def _sync_timed_access(self, cycle_offset: int = 0) -> None:
        sync_cpu_access = self.sync_cpu_access
        if sync_cpu_access is not None:
            sync_cpu_access(self.cpu_access_cycle + int(cycle_offset))
            return
        sync_devices = self.sync_devices
        if sync_devices is not None:
            sync_devices()

    def _dmc_read_replay_count(self, fetch_count_before: int) -> int:
        """Return bus-visible CPU read repeats caused by a coincident DMC DMA.

        RDY can only stop the 2A03 on a read cycle.  Once stopped, the CPU
        leaves the interrupted read address on its bus during the DMA halt and
        dummy cycles, plus an alignment cycle when the request began on the
        DMA unit's odd (put) phase.  PPU registers therefore observe two or
        three extra reads before the CPU's original read completes.

        The controller clock is a separate special case handled by ``read``:
        the uninterrupted address pulse shifts it only once even though the
        PPU register interface observes every held cycle.
        """
        if not self.cpu_instruction_active:
            return 0
        dmc = self.apu.dmc
        if dmc.fetch_count == fetch_count_before:
            return 0
        fetch_cycle = dmc.last_fetch_cycle
        # ``last_fetch_cycle`` is the APU request edge.  RDY is sampled by the
        # CPU on the following bus cycle, so a request one clock before the
        # register access is the conflict; a request stamped on the access
        # cycle itself cannot hold that already-started read.
        if fetch_cycle is None or fetch_cycle + 1 != self.apu.cycle:
            return 0
        return 2 + (fetch_cycle & 1)

    @property
    def irq_pending(self) -> bool:
        return self.apu.irq_pending or self.mapper.irq_pending

    @property
    def irq_assert_cycle(self) -> int | None:
        """Earliest known CPU cycle at which the active IRQ was asserted."""
        cycles: list[int] = []
        if self.apu.frame_irq_pending:
            frame_cycle = self.apu.frame_irq_assert_cycle
            cycles.append(
                self.cpu_cycles
                if frame_cycle is None
                else frame_cycle
            )
        if self.apu.dmc.irq_pending:
            cycles.append(self.cpu_cycles)
        if self.mapper.irq_pending:
            mapper_cycle = getattr(self.mapper, "irq_assert_cycle", None)
            cycles.append(
                self.cpu_cycles
                if mapper_cycle is None
                else int(mapper_cycle)
            )
        return min(cycles) if cycles else None

    def reset(self, clear_ram: bool = False) -> None:
        if clear_ram:
            self.ram[:] = bytes(len(self.ram))
        self.open_bus = 0
        self.cpu_cycles = 0
        self.dma_stall_cycles = 0
        self.dma_parity_pending = False
        self.cpu_instruction_start_cycle = 0
        self.cpu_instruction_active = False

    def read(self, address: int) -> int:
        address &= 0xFFFF
        if address < 0x2000:
            value = self.ram[address & 0x07FF]
        elif address < 0x4000:
            fetch_count = self.apu.dmc.fetch_count
            self._sync_timed_access()
            for _ in range(self._dmc_read_replay_count(fetch_count)):
                self.ppu.cpu_read_register(address)
            value = self.ppu.cpu_read_register(address)
        elif address == 0x4015:
            fetch_count = self.apu.dmc.fetch_count
            self._sync_timed_access()
            for _ in range(self._dmc_read_replay_count(fetch_count)):
                self.apu.read_status()
            value = self.apu.read_status()
        elif address == 0x4016:
            fetch_count = self.apu.dmc.fetch_count
            self._sync_timed_access()
            if self._dmc_read_replay_count(fetch_count):
                self.controller1.read()
            value = (self.open_bus & 0xE0) | 0x40 | self.controller1.read()
        elif address == 0x4017:
            fetch_count = self.apu.dmc.fetch_count
            self._sync_timed_access()
            if self._dmc_read_replay_count(fetch_count):
                self.controller2.read()
            value = (self.open_bus & 0xE0) | 0x40 | self.controller2.read()
        elif address >= 0x8000 and self._nrom_prg is not None:
            value = self._nrom_prg[
                (address - 0x8000) & self._nrom_prg_mask
            ]
        elif address >= 0x8000 and self._mapped_prg_window is not None:
            value = self._mapped_prg_window[address - 0x8000]
        elif address >= 0x8000 and self._mapped_prg_banks is not None:
            value = self._mapped_prg_banks[(address - 0x8000) >> 13][
                address & 0x1FFF
            ]
        elif 0x4020 <= address < 0x6000:
            # No currently supported board maps the cartridge expansion
            # region. With no device driving D0-D7, reads retain CPU open bus.
            value = self.open_bus
        elif address >= 0x6000:
            value = self._mapper_cpu_read(address)
        else:
            value = self.open_bus
        self.open_bus = value & 0xFF
        return self.open_bus

    def read_code(self, address: int) -> int:
        """Read an instruction-stream byte through the shortest valid route."""
        address &= 0xFFFF
        if address >= 0x8000 and self._nrom_prg is not None:
            value = self._nrom_prg[
                (address - 0x8000) & self._nrom_prg_mask
            ]
            self.open_bus = value
            return value
        if address >= 0x8000 and self._mapped_prg_window is not None:
            value = self._mapped_prg_window[address - 0x8000]
            self.open_bus = value
            return value
        if address >= 0x8000 and self._mapped_prg_banks is not None:
            value = self._mapped_prg_banks[(address - 0x8000) >> 13][
                address & 0x1FFF
            ]
            self.open_bus = value
            return value
        if 0x4020 <= address < 0x6000:
            return self.read(address)
        if address >= 0x6000:
            value = self._mapper_cpu_read(address) & 0xFF
            self.open_bus = value
            return value
        return self.read(address)

    def peek_code(self, address: int) -> int | None:
        """Inspect immutable cartridge code without changing CPU open bus.

        The fast scheduler uses this only to recognize short, side-effect-free
        counter loops in PRG ROM. Device space is deliberately not exposed.
        """
        address &= 0xFFFF
        if address < 0x8000:
            return None
        if self._nrom_prg is not None:
            return self._nrom_prg[
                (address - 0x8000) & self._nrom_prg_mask
            ]
        if self._mapped_prg_window is not None:
            return self._mapped_prg_window[address - 0x8000]
        if self._mapped_prg_banks is not None:
            return self._mapped_prg_banks[(address - 0x8000) >> 13][
                address & 0x1FFF
            ]
        return self._mapper_cpu_read(address) & 0xFF

    def read_dmc(self, address: int) -> int:
        return self.read(address)

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        # Publish the exact write cycle before synchronizing devices. DMC DMA
        # can be requested while the APU advances to this access and RDY
        # cannot halt the 6502 until the following read cycle.
        self.apu.cpu_write_cycle = (
            self.cpu_instruction_start_cycle
            + self.cpu_access_cycle
            + 1
            if self.cpu_instruction_active
            else self.cpu_cycles + 1
        )
        self.apu.cpu_write_address = address
        if address < 0x2000:
            self.open_bus = value
            self.ram[address & 0x07FF] = value
            return

        if (
            address < 0x4000
            and (address & 7) == 7
            and self.defer_ppu_data_write is not None
            and self.defer_ppu_data_write(value)
        ):
            self.open_bus = value
            return

        self._sync_timed_access()
        self.open_bus = value
        if address < 0x4000:
            self.ppu.cpu_write_register(address, value)
        elif 0x4000 <= address <= 0x4013:
            self.apu.write_register(address, value)
        elif address == 0x4014:
            page_address = value << 8
            if value < 0x20:
                # CPU RAM occupies $0000-$07FF and is mirrored through
                # $1FFF. OAM DMA always starts on a 256-byte boundary, so a
                # mirrored RAM page is one contiguous slice after masking.
                # This is the overwhelmingly common game path ($0200 in
                # particular) and avoids 256 Python bus-dispatch calls.
                start = page_address & 0x07FF
                page = self.ram[start:start + 0x100]
                self.open_bus = page[-1]
            else:
                # Device, expansion, cartridge-RAM, and ROM pages retain
                # literal reads because those accesses may have side effects.
                page = bytes(
                    self.read(page_address + offset)
                    for offset in range(256)
                )
            self.ppu.oam_dma(page)
            # The timed write's halt cycle is committed as the instruction's
            # final scheduler cycle. The remaining 256 get/put pairs plus an
            # optional alignment cycle therefore require 512/513 additional
            # clocks here, for the hardware's documented 513/514 total.
            self.dma_stall_cycles = 512
            # Resolve alignment parity after the writing instruction's final
            # clock, immediately before the CPU is held.
            self.dma_parity_pending = True
        elif address == 0x4015:
            self.apu.write_register(address, value)
        elif address == 0x4016:
            self.controller1.write_strobe(value)
            self.controller2.write_strobe(value)
        elif address == 0x4017:
            self.apu.write_register(address, value)
        elif address >= 0x4020:
            ppu_cache_before = self.mapper.ppu_cache_token()
            self._mapper_cpu_write(address, value, self.cpu_cycles)
            if self._mapped_prg_window_provider is not None:
                self._mapped_prg_window = self._mapped_prg_window_provider()
            if self._mapped_prg_banks_provider is not None:
                self._mapped_prg_banks = self._mapped_prg_banks_provider()
            ppu_cache_after = self.mapper.ppu_cache_token()
            if (
                (
                    ppu_cache_before is None
                    and address >= 0x8000
                )
                or (
                    ppu_cache_before is not None
                    and ppu_cache_before != ppu_cache_after
                )
            ):
                if self.cartridge.mapper_number == 4:
                    self.ppu.invalidate_fast_mapper_cache()
                else:
                    self.ppu.invalidate_fast_cache()

    def get_state(self) -> dict:
        return {
            "ram": bytes(self.ram),
            "open_bus": self.open_bus,
            "cpu_cycles": self.cpu_cycles,
            "dma_stall_cycles": self.dma_stall_cycles,
            "dma_parity_pending": self.dma_parity_pending,
            "controller1": self.controller1.get_state(),
            "controller2": self.controller2.get_state(),
        }

    def set_state(self, state: dict) -> None:
        ram = bytes(state["ram"])
        if len(ram) != len(self.ram):
            raise ValueError("save state has an incompatible CPU RAM size")
        self.ram[:] = ram
        self.open_bus = int(state["open_bus"]) & 0xFF
        self.cpu_cycles = int(state["cpu_cycles"])
        self.dma_stall_cycles = int(state["dma_stall_cycles"])
        self.dma_parity_pending = bool(state["dma_parity_pending"])
        self.controller1.set_state(state["controller1"])
        self.controller2.set_state(state["controller2"])
        if self._mapped_prg_window_provider is not None:
            self._mapped_prg_window = self._mapped_prg_window_provider()
        if self._mapped_prg_banks_provider is not None:
            self._mapped_prg_banks = self._mapped_prg_banks_provider()
