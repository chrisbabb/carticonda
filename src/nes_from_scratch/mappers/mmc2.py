from __future__ import annotations

from ..constants import Mirroring
from .base import Mapper


class MMC2(Mapper):
    """Nintendo MMC2 / PxROM latch-based mapper (iNES mapper 9)."""

    mapper_number = 9
    ppu_reads_have_side_effects = True

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.prg_bank = 0
        self.chr_fd_0000 = 0
        self.chr_fe_0000 = 0
        self.chr_fd_1000 = 0
        self.chr_fe_1000 = 0
        # Both MMC2 CHR latches contain $FE after reset.
        self.latch_0000_fe = True
        self.latch_1000_fe = True
        self._mirroring = cartridge.header_mirroring
        self._prg_8k_bank_count = max(1, len(cartridge.prg_rom) // 0x2000)
        self._chr_4k_bank_count = max(1, len(cartridge.chr_memory) // 0x1000)
        prg = cartridge.prg_rom
        banks = tuple(
            prg[offset:offset + 0x2000]
            for offset in range(0, len(prg), 0x2000)
        )
        selectable = min(16, len(banks))
        fixed = (
            banks[(len(banks) - 3) % len(banks)]
            + banks[(len(banks) - 2) % len(banks)]
            + banks[(len(banks) - 1) % len(banks)]
        )
        self._cpu_code_windows = tuple(
            banks[bank] + fixed for bank in range(selectable)
        )
        self._refresh_chr_bases()

    @property
    def mirroring(self) -> Mirroring:
        if self.cart.header_mirroring == Mirroring.FOUR_SCREEN:
            return Mirroring.FOUR_SCREEN
        return self._mirroring

    @property
    def prg_8k_banks(self) -> int:
        return self._prg_8k_bank_count

    @property
    def chr_4k_banks(self) -> int:
        return self._chr_4k_bank_count

    def _prg_bank_at(self, slot: int) -> int:
        if slot == 0:
            return (self.prg_bank & 0x0F) % self.prg_8k_banks
        # MMC2 fixes the final three 8 KiB banks at $A000-$FFFF.
        return (self.prg_8k_banks - (4 - slot)) % self.prg_8k_banks

    def cpu_code_windows(self) -> tuple[bytes, ...]:
        """Return every immutable 32 KiB CPU mapping selected by register A."""
        return self._cpu_code_windows

    def cpu_code_window(self) -> bytes:
        """Return the currently mapped immutable $8000-$FFFF PRG window."""
        bank = (self.prg_bank & 0x0F) % len(self._cpu_code_windows)
        return self._cpu_code_windows[bank]

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            return self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)]
        if address >= 0x8000:
            return self.cpu_code_window()[address - 0x8000]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        value &= 0xFF
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)] = value
            return
        if address < 0xA000:
            return

        register = address & 0xF000
        ppu_register_changed = False
        if register == 0xA000:
            self.prg_bank = value & 0x0F
        elif register == 0xB000:
            self.chr_fd_0000 = value & 0x1F
            ppu_register_changed = True
            self._chr_fd_0000_base = (
                self.chr_fd_0000 % self._chr_4k_bank_count
            ) * 0x1000
            if not self.latch_0000_fe:
                self._chr_0000_base = self._chr_fd_0000_base
        elif register == 0xC000:
            self.chr_fe_0000 = value & 0x1F
            ppu_register_changed = True
            self._chr_fe_0000_base = (
                self.chr_fe_0000 % self._chr_4k_bank_count
            ) * 0x1000
            if self.latch_0000_fe:
                self._chr_0000_base = self._chr_fe_0000_base
        elif register == 0xD000:
            self.chr_fd_1000 = value & 0x1F
            ppu_register_changed = True
            self._chr_fd_1000_base = (
                self.chr_fd_1000 % self._chr_4k_bank_count
            ) * 0x1000
            if not self.latch_1000_fe:
                self._chr_1000_base = self._chr_fd_1000_base
        elif register == 0xE000:
            self.chr_fe_1000 = value & 0x1F
            ppu_register_changed = True
            self._chr_fe_1000_base = (
                self.chr_fe_1000 % self._chr_4k_bank_count
            ) * 0x1000
            if self.latch_1000_fe:
                self._chr_1000_base = self._chr_fe_1000_base
        elif register == 0xF000 and self.cart.header_mirroring != Mirroring.FOUR_SCREEN:
            self._mirroring = (
                Mirroring.HORIZONTAL if value & 1 else Mirroring.VERTICAL
            )
            ppu_register_changed = True
        if ppu_register_changed:
            self._refresh_ppu_cache_token()

    def ppu_cache_token(self) -> tuple:
        return self._ppu_cache_token

    def _refresh_ppu_cache_token(self) -> None:
        self._ppu_cache_token = (
            self.chr_fd_0000,
            self.chr_fe_0000,
            self.chr_fd_1000,
            self.chr_fe_1000,
            self._mirroring,
        )

    def ppu_latch_state(self) -> tuple[bool, bool]:
        return self.latch_0000_fe, self.latch_1000_fe

    def set_ppu_latch_state(self, state: tuple[bool, bool]) -> None:
        self.latch_0000_fe = bool(state[0])
        self.latch_1000_fe = bool(state[1])
        self._chr_0000_base = (
            self._chr_fe_0000_base
            if self.latch_0000_fe
            else self._chr_fd_0000_base
        )
        self._chr_1000_base = (
            self._chr_fe_1000_base
            if self.latch_1000_fe
            else self._chr_fd_1000_base
        )

    def begin_fast_latched_pairs(self) -> tuple:
        """Expose local state for one ordered fast-PPU pattern-fetch span."""
        return (
            self.cart.chr_memory,
            self._chr_0000_base,
            self._chr_1000_base,
            self._chr_fd_0000_base,
            self._chr_fe_0000_base,
            self._chr_fd_1000_base,
            self._chr_fe_1000_base,
            self.latch_0000_fe,
            self.latch_1000_fe,
        )

    def end_fast_latched_pairs(
        self,
        base_0000: int,
        base_1000: int,
        latch_0000_fe: bool,
        latch_1000_fe: bool,
    ) -> None:
        """Commit the latch state produced by a fast-PPU fetch span."""
        self._chr_0000_base = base_0000
        self._chr_1000_base = base_1000
        self.latch_0000_fe = latch_0000_fe
        self.latch_1000_fe = latch_1000_fe

    def _refresh_chr_bases(self) -> None:
        count = self._chr_4k_bank_count
        self._chr_fd_0000_base = (self.chr_fd_0000 % count) * 0x1000
        self._chr_fe_0000_base = (self.chr_fe_0000 % count) * 0x1000
        self._chr_fd_1000_base = (self.chr_fd_1000 % count) * 0x1000
        self._chr_fe_1000_base = (self.chr_fe_1000 % count) * 0x1000
        self.set_ppu_latch_state(
            (self.latch_0000_fe, self.latch_1000_fe)
        )
        self._refresh_ppu_cache_token()

    def _chr_bank(self, address: int) -> int:
        if address & 0x1000:
            bank = (
                self.chr_fe_1000
                if self.latch_1000_fe
                else self.chr_fd_1000
            )
        else:
            bank = (
                self.chr_fe_0000
                if self.latch_0000_fe
                else self.chr_fd_0000
            )
        return bank % self.chr_4k_banks

    def _chr_offset(self, address: int) -> int:
        address &= 0x1FFF
        bank_base = (
            self._chr_1000_base if address & 0x1000 else self._chr_0000_base
        )
        return bank_base + (address & 0x0FFF)

    def _update_latch_after_read(self, address: int) -> None:
        address &= 0x1FFF
        # MMC2 latch 0 decodes only these two exact addresses. Latch 1
        # decodes the complete second-plane row ($xFD8-$xFDF/$xFE8-$xFEF).
        if address == 0x0FD8:
            self.latch_0000_fe = False
            self._chr_0000_base = self._chr_fd_0000_base
        elif address == 0x0FE8:
            self.latch_0000_fe = True
            self._chr_0000_base = self._chr_fe_0000_base
        elif (address & 0x1FF8) == 0x1FD8:
            self.latch_1000_fe = False
            self._chr_1000_base = self._chr_fd_1000_base
        elif (address & 0x1FF8) == 0x1FE8:
            self.latch_1000_fe = True
            self._chr_1000_base = self._chr_fe_1000_base

    def ppu_read(self, address: int) -> int:
        address &= 0x1FFF
        value = self.cart.chr_memory[self._chr_offset(address)]
        # The access that trips a latch is served by the old bank. The newly
        # selected FD/FE bank is visible starting with the following access.
        self._update_latch_after_read(address)
        return value

    def ppu_read_pair(self, address: int) -> tuple[int, int]:
        """Read a low/high pattern row with one delayed latch edge."""
        address &= 0x1FFF
        bank_base = (
            self._chr_1000_base if address & 0x1000 else self._chr_0000_base
        )
        row_address = address & 0x0FFF
        memory = self.cart.chr_memory
        low = memory[bank_base + row_address]
        high = memory[bank_base + ((row_address + 8) & 0x0FFF)]

        # This optimized entry point is used only for ordinary PPU pattern-row
        # pairs whose first address is in the low bitplane ($xxx0-$xxx7).
        # The second access can trip a latch, after both bytes use the old bank.
        trigger = address & 0x1FF8
        if trigger == 0x0FD0 and address == 0x0FD0:
            self.latch_0000_fe = False
            self._chr_0000_base = self._chr_fd_0000_base
        elif trigger == 0x0FE0 and address == 0x0FE0:
            self.latch_0000_fe = True
            self._chr_0000_base = self._chr_fe_0000_base
        elif trigger == 0x1FD0:
            self.latch_1000_fe = False
            self._chr_1000_base = self._chr_fd_1000_base
        elif trigger == 0x1FE0:
            self.latch_1000_fe = True
            self._chr_1000_base = self._chr_fe_1000_base
        return low, high

    def ppu_write(self, address: int, value: int) -> None:
        # Production PxROM boards use CHR ROM. Retaining mapped writes for a
        # NES 2.0/homebrew CHR-RAM image is useful and does not clock a latch.
        if self.cart.chr_is_ram:
            self.cart.chr_memory[self._chr_offset(address)] = value & 0xFF

    def get_state(self) -> dict:
        return {
            **super().get_state(),
            "prg_bank": self.prg_bank,
            "chr_fd_0000": self.chr_fd_0000,
            "chr_fe_0000": self.chr_fe_0000,
            "chr_fd_1000": self.chr_fd_1000,
            "chr_fe_1000": self.chr_fe_1000,
            "latch_0000_fe": self.latch_0000_fe,
            "latch_1000_fe": self.latch_1000_fe,
            "mirroring": self._mirroring.value,
        }

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.prg_bank = int(state["prg_bank"]) & 0x0F
        self.chr_fd_0000 = int(state["chr_fd_0000"]) & 0x1F
        self.chr_fe_0000 = int(state["chr_fe_0000"]) & 0x1F
        self.chr_fd_1000 = int(state["chr_fd_1000"]) & 0x1F
        self.chr_fe_1000 = int(state["chr_fe_1000"]) & 0x1F
        self.latch_0000_fe = bool(state["latch_0000_fe"])
        self.latch_1000_fe = bool(state["latch_1000_fe"])
        self._mirroring = Mirroring(state["mirroring"])
        self._refresh_chr_bases()
