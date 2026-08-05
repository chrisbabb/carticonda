from __future__ import annotations

from .base import Mapper, split_banks


class Mapper34(Mapper):
    """NINA-001/NINA-002 or BNROM (iNES mapper 34).

    NES 2.0 submapper 1 identifies NINA-001 and submapper 2 identifies
    BNROM. Legacy iNES images are distinguished by their CHR medium:
    NINA-001 uses CHR ROM while BNROM uses CHR RAM.
    """

    mapper_number = 34

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self.nina = (
            cartridge.submapper == 1
            or (
                cartridge.submapper == 0
                and not cartridge.chr_is_ram
            )
        )
        self.prg_bank = 0
        self.chr_bank_0 = 0
        self.chr_bank_1 = 1
        self._cpu_code_windows = split_banks(cartridge.prg_rom, 0x8000)
        self._chr_4k_bank_count = max(
            1, (len(cartridge.chr_memory) + 0x0FFF) // 0x1000
        )

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

        if self.nina:
            if address == 0x7FFD:
                self.prg_bank = value & 0x01
            elif address == 0x7FFE:
                self.chr_bank_0 = value & 0x0F
            elif address == 0x7FFF:
                self.chr_bank_1 = value & 0x0F
        elif address >= 0x8000:
            value = self._bus_conflict(address, value)
            self.prg_bank = value

    def _chr_offset(self, address: int) -> int:
        address &= 0x1FFF
        if not self.nina:
            return address % len(self.cart.chr_memory)
        bank = self.chr_bank_1 if address & 0x1000 else self.chr_bank_0
        return (
            (bank % self._chr_4k_bank_count) * 0x1000
            + (address & 0x0FFF)
        ) % len(self.cart.chr_memory)

    def ppu_read(self, address: int) -> int:
        return self.cart.chr_memory[self._chr_offset(address)]

    def ppu_write(self, address: int, value: int) -> None:
        if self.cart.chr_is_ram:
            self.cart.chr_memory[self._chr_offset(address)] = value & 0xFF

    def ppu_cache_token(self) -> tuple:
        if self.nina:
            return (self.chr_bank_0, self.chr_bank_1)
        return ()

    def get_state(self) -> dict:
        return {
            **super().get_state(),
            "prg_bank": self.prg_bank,
            "chr_bank_0": self.chr_bank_0,
            "chr_bank_1": self.chr_bank_1,
        }

    def set_state(self, state: dict) -> None:
        super().set_state(state)
        self.prg_bank = int(state["prg_bank"])
        self.chr_bank_0 = int(state["chr_bank_0"]) & 0x0F
        self.chr_bank_1 = int(state["chr_bank_1"]) & 0x0F
