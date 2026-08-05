"""Base mapper interface and mapper factory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..constants import Mirroring

if TYPE_CHECKING:
    from ..cartridge import Cartridge


class MapperError(ValueError):
    pass


class Mapper:
    mapper_number = -1
    ppu_reads_have_side_effects = False

    def __init__(self, cartridge: "Cartridge") -> None:
        self.cart = cartridge
        self.irq_pending = False

    @property
    def mirroring(self) -> Mirroring:
        return self.cart.header_mirroring

    def cpu_read(self, address: int) -> int:
        raise NotImplementedError

    def cpu_write(self, address: int, value: int) -> None:
        raise NotImplementedError

    def cpu_write_timed(
        self,
        address: int,
        value: int,
        cpu_clock: int,
    ) -> None:
        """Write with the current instruction-boundary CPU clock.

        Most mappers do not care about the write's cycle identity. Serial
        hardware such as MMC1 uses it to reject the second write of a 6502
        read/modify/write instruction, whose two writes occur on consecutive
        CPU cycles.
        """
        del cpu_clock
        self.cpu_write(address, value)

    def ppu_read(self, address: int) -> int:
        if 0 <= address < 0x2000:
            return self.cart.chr_memory[address % len(self.cart.chr_memory)]
        return 0

    def ppu_write(self, address: int, value: int) -> None:
        if self.cart.chr_is_ram and 0 <= address < 0x2000:
            self.cart.chr_memory[address % len(self.cart.chr_memory)] = value & 0xFF

    def ppu_read_pair(self, address: int) -> tuple[int, int]:
        return self.ppu_read(address), self.ppu_read(address + 8)

    def notify_ppu_address(self, address: int, ppu_clock: int) -> None:
        del address, ppu_clock

    def ppu_cache_token(self) -> tuple | None:
        """Return render-visible mapper state, or ``None`` if unknown.

        The fast PPU uses this only to avoid discarding presentation caches
        after a CPU mapper write that changed PRG state but did not change
        anything visible to the PPU. Mappers that do not provide a token keep
        the conservative invalidate-on-write behavior.
        """
        return None

    def ppu_pattern_cache_token(self, pattern_base: int) -> object | None:
        """Return the effective CHR mapping for one 4 KiB pattern table.

        Most boards expose one indivisible PPU mapping, so the conservative
        default is their complete render token. Mappers with independently
        banked pattern-table regions can override this and let the fast PPU
        retain an unchanged background when only sprite-side CHR changes.
        """
        del pattern_base
        return self.ppu_cache_token()

    def _bus_conflict(self, address: int, value: int) -> int:
        """Apply explicitly declared discrete-logic AND bus conflicts.

        NES 2.0 submapper 2 identifies boards where CPU and PRG ROM both
        drive the data bus during a mapper write. Submapper 1 explicitly has
        no conflict; ambiguous legacy/submapper-0 images retain the previous
        compatibility behavior.
        """
        value &= 0xFF
        if self.cart.submapper == 2 and address >= 0x8000:
            return value & self.cpu_read(address)
        return value

    def get_state(self) -> dict:
        return {"irq_pending": self.irq_pending}

    def set_state(self, state: dict) -> None:
        self.irq_pending = bool(state.get("irq_pending", False))


def split_banks(data: bytes | bytearray, size: int) -> tuple[bytes, ...]:
    """Return fixed-size immutable banks, mirroring a short final bank.

    Cartridge ROM sizes are normally exact multiples of their board's bank
    width. A few historical dumps use a smaller ROM whose address lines are
    physically mirrored. Normalizing that case once at load time keeps every
    instruction fetch on the short immutable-window path.
    """
    if size <= 0:
        raise ValueError("bank size must be positive")
    source = bytes(data)
    if not source:
        raise ValueError("cannot split empty ROM data")
    count = max(1, (len(source) + size - 1) // size)
    banks: list[bytes] = []
    for index in range(count):
        start = index * size
        bank = source[start:start + size]
        if len(bank) < size:
            missing = size - len(bank)
            repeats = (missing + len(source) - 1) // len(source)
            bank += (source * repeats)[:missing]
        banks.append(bank)
    return tuple(banks)


def create_mapper(number: int, cartridge: "Cartridge") -> Mapper:
    if number == 0:
        from .nrom import NROM
        return NROM(cartridge)
    if number == 1:
        from .mmc1 import MMC1
        return MMC1(cartridge)
    if number == 2:
        from .uxrom import UxROM
        return UxROM(cartridge)
    if number == 3:
        from .cnrom import CNROM
        return CNROM(cartridge)
    if number == 4:
        from .mmc3 import MMC3
        return MMC3(cartridge)
    if number == 7:
        from .axrom import AxROM
        return AxROM(cartridge)
    if number == 9:
        from .mmc2 import MMC2
        return MMC2(cartridge)
    if number == 10:
        from .mmc4 import MMC4
        return MMC4(cartridge)
    if number == 11:
        from .color_dreams import ColorDreams
        return ColorDreams(cartridge)
    if number == 13:
        from .cprom import CPROM
        return CPROM(cartridge)
    if number == 34:
        from .mapper34 import Mapper34
        return Mapper34(cartridge)
    if number == 66:
        from .gxrom import GxROM
        return GxROM(cartridge)
    if number == 71:
        from .camerica import Camerica
        return Camerica(cartridge)
    if number == 87:
        from .mapper87 import Mapper87
        return Mapper87(cartridge)
    if number == 94:
        from .un1rom import UN1ROM
        return UN1ROM(cartridge)
    if number == 140:
        from .jaleco_jf import JalecoJF11
        return JalecoJF11(cartridge)
    if number == 180:
        from .unrom180 import UNROM180
        return UNROM180(cartridge)
    raise MapperError(
        f"mapper {number} is not implemented; supported mappers are "
        "0 (NROM), 1 (MMC1), 2 (UxROM), 3 (CNROM), 4 (MMC3), "
        "7 (AxROM), 9 (MMC2), 10 (MMC4), 11 (Color Dreams), "
        "13 (CPROM), 34 (BNROM/NINA-001), 66 (GxROM), "
        "71 (Camerica), 87 (Jaleco), 94 (UN1ROM), "
        "140 (Jaleco JF-11/JF-14), and 180 (UNROM reverse)"
    )
