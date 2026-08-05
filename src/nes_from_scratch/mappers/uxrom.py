from __future__ import annotations

from .base import Mapper, split_banks


class UxROM(Mapper):
    mapper_number = 2

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.bank = 0
        banks = split_banks(cartridge.prg_rom, 0x4000)
        fixed = banks[-1]
        self._cpu_code_windows = tuple(bank + fixed for bank in banks)

    @property
    def bank_count(self) -> int:
        return len(self._cpu_code_windows)

    def cpu_code_windows(self) -> tuple[bytes, ...]:
        return self._cpu_code_windows

    def cpu_code_window(self) -> bytes:
        return self._cpu_code_windows[self.bank % self.bank_count]

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            return self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)]
        if address >= 0x8000:
            return self.cpu_code_window()[address - 0x8000]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            self.cart.prg_ram[(address - 0x6000) % len(self.cart.prg_ram)] = value
        elif address >= 0x8000:
            value = self._bus_conflict(address, value)
            self.bank = value % self.bank_count

    def ppu_cache_token(self) -> tuple:
        return ()

    def get_state(self) -> dict:
        return {**super().get_state(), "bank": self.bank}

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.bank = int(state["bank"]) % self.bank_count
