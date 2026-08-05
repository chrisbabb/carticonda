from __future__ import annotations

from .base import Mapper, split_banks


class GxROM(Mapper):
    mapper_number = 66

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.prg_bank = 0
        self.chr_bank = 0
        self._cpu_code_windows = split_banks(cartridge.prg_rom, 0x8000)

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
        if address >= 0x8000:
            value = self._bus_conflict(address, value)
            self.prg_bank = (value >> 4) & 0x03
            self.chr_bank = value & 0x03

    def ppu_read(self, address: int) -> int:
        banks = max(1, len(self.cart.chr_memory) // 0x2000)
        offset = (self.chr_bank % banks) * 0x2000 + (address & 0x1FFF)
        return self.cart.chr_memory[offset]

    def ppu_write(self, address: int, value: int) -> None:
        if self.cart.chr_is_ram:
            banks = max(1, len(self.cart.chr_memory) // 0x2000)
            offset = (self.chr_bank % banks) * 0x2000 + (address & 0x1FFF)
            self.cart.chr_memory[offset] = value & 0xFF

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
        self.prg_bank = int(state["prg_bank"])
        self.chr_bank = int(state["chr_bank"])
