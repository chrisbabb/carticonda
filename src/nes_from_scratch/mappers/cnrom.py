from __future__ import annotations

from .base import Mapper, split_banks


class CNROM(Mapper):
    mapper_number = 3

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.chr_bank = 0
        self._cpu_code_windows = (
            split_banks(cartridge.prg_rom, 0x8000)[0],
        )

    def cpu_code_windows(self) -> tuple[bytes, ...]:
        return self._cpu_code_windows

    def cpu_code_window(self) -> bytes:
        return self._cpu_code_windows[0]

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            return self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)]
        if address >= 0x8000:
            return self._cpu_code_windows[0][address - 0x8000]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)] = value
        elif address >= 0x8000:
            value = self._bus_conflict(address, value)
            banks = max(1, len(self.cart.chr_memory) // 0x2000)
            self.chr_bank = value % banks

    def ppu_read(self, address: int) -> int:
        offset = self.chr_bank * 0x2000 + (address & 0x1FFF)
        return self.cart.chr_memory[offset % len(self.cart.chr_memory)]

    def ppu_write(self, address: int, value: int) -> None:
        if self.cart.chr_is_ram:
            offset = self.chr_bank * 0x2000 + (address & 0x1FFF)
            self.cart.chr_memory[offset % len(self.cart.chr_memory)] = value & 0xFF

    def ppu_cache_token(self) -> tuple:
        return (self.chr_bank,)

    def get_state(self) -> dict:
        return {**super().get_state(), "chr_bank": self.chr_bank}

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.chr_bank = int(state["chr_bank"])
