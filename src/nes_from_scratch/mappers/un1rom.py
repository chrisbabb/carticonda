from __future__ import annotations

from .uxrom import UxROM


class UN1ROM(UxROM):
    """HVC-UN1ROM, an UxROM register shifted left two bits (mapper 94)."""

    mapper_number = 94

    def cpu_write(self, address: int, value: int) -> None:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            self.cart.prg_ram[
                (address - 0x6000) % len(self.cart.prg_ram)
            ] = value & 0xFF
        elif address >= 0x8000:
            value = self._bus_conflict(address, value)
            self.bank = ((value >> 2) & 0x07) % self.bank_count
