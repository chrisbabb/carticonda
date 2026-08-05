from __future__ import annotations

from .base import Mapper, split_banks


class JalecoJF11(Mapper):
    """Jaleco JF-11/JF-14, GNROM-style banking at $6000 (mapper 140)."""

    mapper_number = 140

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.prg_bank = 0
        self.chr_bank = 0
        self._cpu_code_windows = split_banks(cartridge.prg_rom, 0x8000)
        self._chr_bank_count = max(
            1, (len(cartridge.chr_memory) + 0x1FFF) // 0x2000
        )

    def cpu_code_windows(self) -> tuple[bytes, ...]:
        return self._cpu_code_windows

    def cpu_code_window(self) -> bytes:
        return self._cpu_code_windows[
            self.prg_bank % len(self._cpu_code_windows)
        ]

    def cpu_read(self, address: int) -> int:
        if address >= 0x8000:
            return self.cpu_code_window()[address - 0x8000]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        if 0x6000 <= address < 0x8000:
            value &= 0xFF
            self.prg_bank = (value >> 4) & 0x03
            self.chr_bank = value & 0x0F

    def _chr_offset(self, address: int) -> int:
        bank = self.chr_bank % self._chr_bank_count
        return (
            bank * 0x2000 + (address & 0x1FFF)
        ) % len(self.cart.chr_memory)

    def ppu_read(self, address: int) -> int:
        return self.cart.chr_memory[self._chr_offset(address)]

    def ppu_write(self, address: int, value: int) -> None:
        if self.cart.chr_is_ram:
            self.cart.chr_memory[self._chr_offset(address)] = value & 0xFF

    def ppu_cache_token(self) -> tuple:
        return (self.chr_bank,)

    def get_state(self) -> dict:
        return {
            **super().get_state(),
            "prg_bank": self.prg_bank,
            "chr_bank": self.chr_bank,
        }

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.prg_bank = int(state["prg_bank"]) & 0x03
        self.chr_bank = int(state["chr_bank"]) & 0x0F
