from __future__ import annotations

from ..constants import Mirroring
from .base import Mapper, split_banks


class AxROM(Mapper):
    mapper_number = 7

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.prg_bank = 0
        self.upper_screen = False
        self._cpu_code_windows = split_banks(cartridge.prg_rom, 0x8000)

    @property
    def mirroring(self) -> Mirroring:
        return Mirroring.SINGLE_UPPER if self.upper_screen else Mirroring.SINGLE_LOWER

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
            self.prg_bank = value & 0x0F
            self.upper_screen = bool(value & 0x10)

    def ppu_cache_token(self) -> tuple:
        return (self.upper_screen,)

    def get_state(self) -> dict:
        return {
            **super().get_state(),
            "prg_bank": self.prg_bank,
            "upper_screen": self.upper_screen,
        }

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.prg_bank = int(state["prg_bank"])
        self.upper_screen = bool(state["upper_screen"])
