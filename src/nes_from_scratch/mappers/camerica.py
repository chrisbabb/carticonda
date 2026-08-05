from __future__ import annotations

from ..constants import Mirroring
from .base import Mapper, split_banks


class Camerica(Mapper):
    """Camerica/Codemasters BF909x boards (iNES mapper 71)."""

    mapper_number = 71

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.prg_bank = 0
        self._mirroring = cartridge.header_mirroring
        banks = split_banks(cartridge.prg_rom, 0x4000)
        fixed = banks[-1]
        self._cpu_code_windows = tuple(bank + fixed for bank in banks)

    @property
    def mirroring(self) -> Mirroring:
        if self.cart.header_mirroring == Mirroring.FOUR_SCREEN:
            return Mirroring.FOUR_SCREEN
        return self._mirroring

    def cpu_code_windows(self) -> tuple[bytes, ...]:
        return self._cpu_code_windows

    def cpu_code_window(self) -> bytes:
        return self._cpu_code_windows[
            self.prg_bank % len(self._cpu_code_windows)
        ]

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            return self.cart.prg_ram[
                (address - 0x6000) % len(self.cart.prg_ram)
            ]
        if address >= 0x8000:
            return self.cpu_code_window()[address - 0x8000]
        return 0

    def cpu_write(self, address: int, value: int) -> None:
        value &= 0xFF
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            self.cart.prg_ram[
                (address - 0x6000) % len(self.cart.prg_ram)
            ] = value
        elif 0x8000 <= address < 0xA000:
            # Only Fire Hawk's BF9097 board decodes this register. Legacy
            # mapper-71 images cannot carry a submapper, and other games do
            # not write here, so accepting it for submapper 0 is compatible.
            if (
                self.cart.submapper in (0, 1)
                and self.cart.header_mirroring != Mirroring.FOUR_SCREEN
            ):
                self._mirroring = (
                    Mirroring.SINGLE_UPPER
                    if value & 0x10
                    else Mirroring.SINGLE_LOWER
                )
        elif address >= 0xC000:
            # Camerica's ASIC prevents the bus conflicts found on UNROM.
            self.prg_bank = value & 0x0F

    def ppu_cache_token(self) -> tuple:
        return (self._mirroring,)

    def get_state(self) -> dict:
        return {
            **super().get_state(),
            "prg_bank": self.prg_bank,
            "mirroring": self._mirroring.value,
        }

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.prg_bank = int(state["prg_bank"]) & 0x0F
        self._mirroring = Mirroring(state["mirroring"])
