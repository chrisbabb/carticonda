from __future__ import annotations

from .mmc2 import MMC2


class MMC4(MMC2):
    """Nintendo MMC4 / FxROM latch-based mapper (iNES mapper 10)."""

    mapper_number = 10

    def __init__(self, cartridge) -> None:
        super().__init__(cartridge)
        self._prg_16k_bank_count = max(
            1, len(cartridge.prg_rom) // 0x4000
        )
        # MMC4's reset value is $0E. On smaller ROMs the unconnected high
        # address lines naturally mirror that to the second-last bank.
        self.prg_bank = 0x0E
        prg = cartridge.prg_rom
        banks = tuple(
            prg[offset:offset + 0x4000]
            for offset in range(0, len(prg), 0x4000)
        )
        selectable = min(16, len(banks))
        fixed = banks[-1]
        self._cpu_code_windows = tuple(
            banks[bank] + fixed for bank in range(selectable)
        )

    @property
    def prg_16k_banks(self) -> int:
        return self._prg_16k_bank_count

    def cpu_code_window(self) -> bytes:
        bank = (self.prg_bank & 0x0F) % len(self._cpu_code_windows)
        return self._cpu_code_windows[bank]

    def cpu_read(self, address: int) -> int:
        if 0x6000 <= address < 0x8000 and self.cart.prg_ram:
            return self.cart.prg_ram[
                (address - 0x6000) % len(self.cart.prg_ram)
            ]
        if address >= 0x8000:
            return self.cpu_code_window()[address - 0x8000]
        return 0
