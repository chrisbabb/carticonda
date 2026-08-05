from __future__ import annotations

from .base import Mapper


class NROM(Mapper):
    mapper_number = 0

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            return self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)]
        if address >= 0x8000:
            return self.cart.prg_rom[(address - 0x8000) % len(self.cart.prg_rom)]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)] = value

    def ppu_cache_token(self) -> tuple:
        return ()
