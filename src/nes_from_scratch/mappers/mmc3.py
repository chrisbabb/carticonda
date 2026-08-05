from __future__ import annotations

from ..constants import Mirroring
from .base import Mapper, split_banks


class MMC3(Mapper):
    """Nintendo MMC3/MMC6-style bank switching and scanline IRQ counter."""

    mapper_number = 4

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        # MMC3 changes four 8 KiB PRG and eight 1 KiB CHR slots, not the
        # underlying ROM. Normalize the immutable PRG banks and materialize
        # the current slot maps only when a register changes. Previously every
        # CPU/PPU read rebuilt this mapping in Python; a cache-invalidating
        # transition could perform millions of divisions, tuple allocations,
        # and mapper calls in one frame.
        self._prg_banks = split_banks(cartridge.prg_rom, 0x2000)
        self._prg_bank_count = len(self._prg_banks)
        self._chr_bank_count = max(1, len(cartridge.chr_memory) // 0x0400)
        self.bank_select = 0
        self.bank_registers = [0] * 8
        self._prg_slots: tuple[bytes, bytes, bytes, bytes]
        self._chr_offsets: tuple[int, int, int, int, int, int, int, int]
        self._refresh_prg_slots()
        self._refresh_chr_offsets()
        self._mirroring = cartridge.header_mirroring
        self._ppu_cache_token: tuple = ()
        self._ppu_pattern_tokens: tuple[
            tuple[int, int, int, int],
            tuple[int, int, int, int],
        ]
        self._refresh_ppu_cache_token()
        self.prg_ram_enabled = True
        self.prg_ram_write_protected = False
        self.irq_latch = 0
        self.irq_counter = 0
        self.irq_reload = False
        self.irq_enabled = False
        self.irq_assert_cycle: int | None = None
        self.last_a12 = False
        self.a12_low_since = 0

    @property
    def mirroring(self) -> Mirroring:
        if self.cart.header_mirroring == Mirroring.FOUR_SCREEN:
            return Mirroring.FOUR_SCREEN
        return self._mirroring

    @property
    def prg_8k_banks(self) -> int:
        return self._prg_bank_count

    @property
    def chr_1k_banks(self) -> int:
        return self._chr_bank_count

    def _refresh_prg_slots(self) -> None:
        last = self._prg_bank_count - 1
        second_last = max(0, last - 1)
        bank_6 = self.bank_registers[6] % self._prg_bank_count
        bank_7 = self.bank_registers[7] % self._prg_bank_count
        if self.bank_select & 0x40:
            indices = (second_last, bank_7, bank_6, last)
        else:
            indices = (bank_6, bank_7, second_last, last)
        banks = self._prg_banks
        self._prg_slots = (
            banks[indices[0]],
            banks[indices[1]],
            banks[indices[2]],
            banks[indices[3]],
        )

    def _refresh_chr_offsets(self) -> None:
        registers = self.bank_registers
        if self.bank_select & 0x80:
            indices = (
                registers[2],
                registers[3],
                registers[4],
                registers[5],
                registers[0] & 0xFE,
                registers[0] | 1,
                registers[1] & 0xFE,
                registers[1] | 1,
            )
        else:
            indices = (
                registers[0] & 0xFE,
                registers[0] | 1,
                registers[1] & 0xFE,
                registers[1] | 1,
                registers[2],
                registers[3],
                registers[4],
                registers[5],
            )
        count = self._chr_bank_count
        self._chr_offsets = tuple(
            (bank % count) * 0x0400 for bank in indices
        )

    def _refresh_ppu_cache_token(self) -> None:
        # Key the renderer by the effective map, not raw register values.
        # Equivalent modulo-bank writes can then retain their tile rows, and
        # every scanline reuses one immutable tuple instead of allocating it.
        self._ppu_cache_token = (self._chr_offsets, self._mirroring)
        offsets = self._chr_offsets
        # Keep the complete bank indices. Packing them into fixed-width
        # fields would make sufficiently large NES 2.0 CHR images alias in a
        # host-only render key.
        self._ppu_pattern_tokens = (
            tuple(offset >> 10 for offset in offsets[:4]),
            tuple(offset >> 10 for offset in offsets[4:]),
        )

    def cpu_code_banks(self) -> tuple[bytes, bytes, bytes, bytes]:
        """Return the active immutable 8 KiB CPU code slots."""
        return self._prg_slots

    def _prg_bank_at(self, slot: int) -> int:
        last = self._prg_bank_count - 1
        second_last = max(0, last - 1)
        mode = bool(self.bank_select & 0x40)
        if slot == 0:
            return (
                second_last
                if mode
                else self.bank_registers[6] % self._prg_bank_count
            )
        if slot == 1:
            return self.bank_registers[7] % self._prg_bank_count
        if slot == 2:
            return (
                self.bank_registers[6] % self._prg_bank_count
                if mode
                else second_last
            )
        return last

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000:
            if self.prg_ram_enabled and self.cart.prg_ram:
                return self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)]
            return 0
        if address >= 0x8000:
            return self._prg_slots[(address - 0x8000) >> 13][
                address & 0x1FFF
            ]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        value &= 0xFF
        if 0x6000 <= address < 0x8000:
            if (
                self.prg_ram_enabled
                and not self.prg_ram_write_protected
                and self.cart.prg_ram
            ):
                self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)] = value
            return
        if address < 0x8000:
            return

        even = not (address & 1)
        region = address & 0xE000
        if region == 0x8000:
            if even:
                old_select = self.bank_select
                self.bank_select = value
                changed = old_select ^ value
                if changed & 0x40:
                    self._refresh_prg_slots()
                if changed & 0x80:
                    self._refresh_chr_offsets()
                    self._refresh_ppu_cache_token()
            else:
                index = self.bank_select & 7
                if self.bank_registers[index] != value:
                    self.bank_registers[index] = value
                    if index < 6:
                        self._refresh_chr_offsets()
                        self._refresh_ppu_cache_token()
                    else:
                        self._refresh_prg_slots()
        elif region == 0xA000:
            if even and self.cart.header_mirroring != Mirroring.FOUR_SCREEN:
                mirroring = (
                    Mirroring.HORIZONTAL if value & 1 else Mirroring.VERTICAL
                )
                if mirroring != self._mirroring:
                    self._mirroring = mirroring
                    self._refresh_ppu_cache_token()
            elif not even:
                self.prg_ram_enabled = bool(value & 0x80)
                self.prg_ram_write_protected = bool(value & 0x40)
        elif region == 0xC000:
            if even:
                self.irq_latch = value
            else:
                self.irq_reload = True
        elif region == 0xE000:
            if even:
                self.irq_enabled = False
                self.irq_pending = False
                self.irq_assert_cycle = None
            else:
                self.irq_enabled = True

    def _chr_bank_at(self, slot: int) -> int:
        return self._chr_offsets[slot] >> 10

    def _chr_offset(self, address: int) -> int:
        address &= 0x1FFF
        return self._chr_offsets[address >> 10] + (address & 0x03FF)

    def ppu_read(self, address: int) -> int:
        return self.cart.chr_memory[self._chr_offset(address)]

    def ppu_read_pair(self, address: int) -> tuple[int, int]:
        address &= 0x1FFF
        high_address = (address + 8) & 0x1FFF
        memory = self.cart.chr_memory
        offsets = self._chr_offsets
        return (
            memory[offsets[address >> 10] + (address & 0x03FF)],
            memory[
                offsets[high_address >> 10] + (high_address & 0x03FF)
            ],
        )

    def ppu_write(self, address: int, value: int) -> None:
        if self.cart.chr_is_ram:
            self.cart.chr_memory[self._chr_offset(address)] = value & 0xFF

    def ppu_cache_token(self) -> tuple:
        return self._ppu_cache_token

    def ppu_pattern_cache_token(
        self,
        pattern_base: int,
    ) -> tuple[int, int, int, int]:
        """Return only the four effective slots fetched by one pattern table."""
        return self._ppu_pattern_tokens[bool(pattern_base & 0x1000)]

    def notify_ppu_address(self, address: int, ppu_clock: int) -> None:
        a12 = bool(address & 0x1000)
        if not a12:
            if self.last_a12:
                self.a12_low_since = ppu_clock
        elif not self.last_a12 and ppu_clock - self.a12_low_since >= 9:
            self._clock_irq(ppu_clock)
        self.last_a12 = a12

    def _clock_irq(self, ppu_clock: int) -> None:
        if self.irq_counter == 0 or self.irq_reload:
            self.irq_counter = self.irq_latch
            self.irq_reload = False
        else:
            self.irq_counter = (self.irq_counter - 1) & 0xFF
        if self.irq_counter == 0 and self.irq_enabled:
            self.irq_pending = True
            # PPU clocks are in the same absolute time domain as the CPU
            # scheduler at a 3:1 ratio. Keep the assertion timestamp stable:
            # deriving it lazily from the current CPU clock made a pending
            # level appear perpetually "new" and prevented the CPU from ever
            # accepting an MMC3 IRQ.
            # A PPU edge can occur on any of the three dots inside a CPU
            # cycle. Record the end of the containing CPU cycle so an edge
            # after that cycle's interrupt-poll phase is not accepted early.
            self.irq_assert_cycle = (int(ppu_clock) + 2) // 3

    def get_state(self) -> dict:
        return {
            **super().get_state(),
            "bank_select": self.bank_select,
            "bank_registers": list(self.bank_registers),
            "mirroring": self._mirroring.value,
            "prg_ram_enabled": self.prg_ram_enabled,
            "prg_ram_write_protected": self.prg_ram_write_protected,
            "irq_latch": self.irq_latch,
            "irq_counter": self.irq_counter,
            "irq_reload": self.irq_reload,
            "irq_enabled": self.irq_enabled,
            "irq_assert_cycle": self.irq_assert_cycle,
            "last_a12": self.last_a12,
            "a12_low_since": self.a12_low_since,
        }

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.bank_select = int(state["bank_select"])
        self.bank_registers = [int(value) for value in state["bank_registers"]]
        self._mirroring = Mirroring(state["mirroring"])
        self.prg_ram_enabled = bool(state["prg_ram_enabled"])
        self.prg_ram_write_protected = bool(state["prg_ram_write_protected"])
        self.irq_latch = int(state["irq_latch"])
        self.irq_counter = int(state["irq_counter"])
        self.irq_reload = bool(state["irq_reload"])
        self.irq_enabled = bool(state["irq_enabled"])
        assert_cycle = state.get("irq_assert_cycle")
        self.irq_assert_cycle = (
            None if assert_cycle is None else int(assert_cycle)
        )
        self.last_a12 = bool(state["last_a12"])
        self.a12_low_since = int(state["a12_low_since"])
        self._refresh_prg_slots()
        self._refresh_chr_offsets()
        self._refresh_ppu_cache_token()
