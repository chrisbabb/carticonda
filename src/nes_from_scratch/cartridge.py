"""iNES/NES 2.0 cartridge image parsing and mapper ownership."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .constants import Mirroring
from .mappers import MapperError, create_mapper


class CartridgeError(ValueError):
    pass


BATTERY_MAGIC = b"CARTBATT\x01"


def _nes2_rom_size(lsb: int, msb: int, unit: int) -> int:
    """Decode either linear or exponent/multiplier NES 2.0 ROM sizing."""
    if msb != 0x0F:
        return (lsb | (msb << 8)) * unit
    exponent = lsb >> 2
    multiplier = ((lsb & 0x03) << 1) | 1
    return (1 << exponent) * multiplier


def _shift_size(value: int) -> int:
    """Decode a NES 2.0 RAM/NVRAM shift count."""
    return 0 if value == 0 else 64 << value


@dataclass(frozen=True)
class CartridgeInfo:
    title: str
    mapper: int
    submapper: int
    prg_rom_size: int
    chr_size: int
    chr_is_ram: bool
    prg_ram_size: int
    prg_nvram_size: int
    chr_ram_size: int
    chr_nvram_size: int
    battery: bool
    mirroring: Mirroring
    format_name: str
    sha256: str


class Cartridge:
    def __init__(
        self,
        *,
        prg_rom: bytes,
        chr_data: bytes,
        chr_is_ram: bool,
        mapper_number: int,
        submapper: int = 0,
        mirroring: Mirroring = Mirroring.HORIZONTAL,
        battery: bool = False,
        prg_ram_size: int = 0,
        prg_nvram_size: int | None = None,
        chr_ram_size: int = 0,
        chr_nvram_size: int = 0,
        title: str = "cartridge",
        format_name: str = "iNES",
        source_bytes: bytes | None = None,
    ) -> None:
        if not prg_rom:
            raise CartridgeError("cartridge has no PRG ROM")
        self.prg_rom = bytes(prg_rom)
        self.chr_is_ram = bool(chr_is_ram)
        requested_chr_ram = max(0, int(chr_ram_size)) + max(
            0, int(chr_nvram_size)
        )
        if self.chr_is_ram:
            # Legacy iNES images imply 8 KiB CHR RAM when no explicit NES 2.0
            # RAM size is present.
            requested_chr_ram = requested_chr_ram or len(chr_data) or 0x2000
            self.chr_memory = bytearray(requested_chr_ram)
            if chr_data:
                self.chr_memory[: min(len(chr_data), requested_chr_ram)] = (
                    chr_data[:requested_chr_ram]
                )
            self.chr_ram = self.chr_memory
        else:
            self.chr_memory = bytearray(chr_data)
            # A NES 2.0 board may contain CHR ROM and additional CHR RAM.
            # Mappers opt into this auxiliary storage explicitly.
            self.chr_ram = bytearray(requested_chr_ram)
        self.mapper_number = int(mapper_number)
        self.submapper = int(submapper)
        self.header_mirroring = mirroring
        self.prg_ram = bytearray(max(0, prg_ram_size))
        requested_prg_nvram = (
            None
            if prg_nvram_size is None
            else max(0, int(prg_nvram_size))
        )
        if (
            requested_prg_nvram is not None
            and requested_prg_nvram > len(self.prg_ram)
        ):
            raise CartridgeError("PRG NVRAM exceeds total PRG RAM")
        requested_chr_nvram = max(0, int(chr_nvram_size))
        if requested_chr_nvram > len(self.chr_ram):
            raise CartridgeError("CHR NVRAM exceeds total CHR RAM")
        self.prg_nvram_size = (
            requested_prg_nvram
            if requested_prg_nvram is not None
            else (len(self.prg_ram) if battery else 0)
        )
        self.chr_nvram_size = requested_chr_nvram
        self.battery = bool(
            battery or self.prg_nvram_size or self.chr_nvram_size
        )
        self.title = title
        self.format_name = format_name
        digest_source = (
            source_bytes
            if source_bytes is not None
            else self.prg_rom + bytes(self.chr_memory)
        )
        self.sha256 = hashlib.sha256(digest_source).hexdigest()
        self.mapper = create_mapper(self.mapper_number, self)

    @classmethod
    def from_file(cls, path: str | Path) -> "Cartridge":
        rom_path = Path(path)
        return cls.from_bytes(rom_path.read_bytes(), title=rom_path.stem)

    @classmethod
    def from_bytes(cls, data: bytes, title: str = "cartridge") -> "Cartridge":
        if len(data) < 16 or data[:4] != b"NES\x1a":
            raise CartridgeError("file does not have a valid iNES header")
        header = data[:16]
        flags6 = header[6]
        flags7 = header[7]
        nes2 = (flags7 & 0x0C) == 0x08
        archaic_ines = not nes2 and any(header[12:16])
        mapper = flags6 >> 4
        if not archaic_ines:
            mapper |= flags7 & 0xF0
        submapper = 0

        if nes2:
            mapper |= (header[8] & 0x0F) << 8
            submapper = header[8] >> 4
            prg_msb = header[9] & 0x0F
            chr_msb = header[9] >> 4
            prg_size = _nes2_rom_size(header[4], prg_msb, 0x4000)
            chr_size = _nes2_rom_size(header[5], chr_msb, 0x2000)
            volatile_shift = header[10] & 0x0F
            battery_shift = header[10] >> 4
            prg_volatile_size = _shift_size(volatile_shift)
            prg_nvram_size = _shift_size(battery_shift)
            prg_ram_size = prg_volatile_size + prg_nvram_size
            chr_volatile_size = _shift_size(header[11] & 0x0F)
            chr_nvram_size = _shift_size(header[11] >> 4)
            chr_ram_size = chr_volatile_size
            format_name = "NES 2.0"
        else:
            prg_size = header[4] * 0x4000
            chr_size = header[5] * 0x2000
            # In a clean iNES header, byte 8 is measured in 8 KiB units and a
            # zero value conventionally infers one 8 KiB bank for compatibility.
            # Archaic headers often contain copier garbage in bytes 7-15, so
            # retain conservative board-based recovery for those images.
            units = 0 if archaic_ines else header[8]
            if units:
                prg_ram_size = units * 0x2000
            elif not archaic_ines:
                prg_ram_size = 0x2000
            elif (
                mapper in (1, 4)
                or (mapper == 34 and chr_size != 0)
                or flags6 & 0x02
            ):
                prg_ram_size = 0x2000
            else:
                prg_ram_size = 0
            prg_nvram_size = prg_ram_size if flags6 & 0x02 else 0
            if chr_size == 0:
                chr_ram_size = 0x4000 if mapper == 13 else 0x2000
            else:
                chr_ram_size = 0
            chr_nvram_size = 0
            format_name = "Archaic iNES" if archaic_ines else "iNES"

        if prg_size == 0:
            raise CartridgeError("iNES header declares zero bytes of PRG ROM")
        has_trainer = bool(flags6 & 0x04)
        trainer = data[16:528] if has_trainer else b""
        if has_trainer and len(trainer) != 512:
            raise CartridgeError("truncated ROM: trainer is not 512 bytes")
        if has_trainer:
            prg_ram_size = max(prg_ram_size, 0x2000)
            if flags6 & 0x02 and not nes2:
                prg_nvram_size = prg_ram_size
        cursor = 16 + (512 if has_trainer else 0)
        end_prg = cursor + prg_size
        end_chr = end_prg + chr_size
        if len(data) < end_chr:
            raise CartridgeError(
                f"truncated ROM: header requires {end_chr} bytes, file has {len(data)}"
            )
        prg_rom = data[cursor:end_prg]
        chr_is_ram = chr_size == 0
        chr_data = b"" if chr_is_ram else data[end_prg:end_chr]

        if flags6 & 0x08:
            mirroring = Mirroring.FOUR_SCREEN
        elif flags6 & 0x01:
            mirroring = Mirroring.VERTICAL
        else:
            mirroring = Mirroring.HORIZONTAL

        try:
            cartridge = cls(
                prg_rom=prg_rom,
                chr_data=chr_data,
                chr_is_ram=chr_is_ram,
                mapper_number=mapper,
                submapper=submapper,
                mirroring=mirroring,
                battery=bool(flags6 & 0x02),
                prg_ram_size=prg_ram_size,
                prg_nvram_size=prg_nvram_size,
                chr_ram_size=chr_ram_size,
                chr_nvram_size=chr_nvram_size,
                title=title,
                format_name=format_name,
                source_bytes=data,
            )
            if trainer:
                cartridge.prg_ram[0x1000:0x1200] = trainer
            return cartridge
        except MapperError:
            raise

    @property
    def info(self) -> CartridgeInfo:
        return CartridgeInfo(
            title=self.title,
            mapper=self.mapper_number,
            submapper=self.submapper,
            prg_rom_size=len(self.prg_rom),
            chr_size=len(self.chr_memory),
            chr_is_ram=self.chr_is_ram,
            prg_ram_size=len(self.prg_ram),
            prg_nvram_size=self.prg_nvram_size,
            chr_ram_size=len(self.chr_ram),
            chr_nvram_size=self.chr_nvram_size,
            battery=self.battery,
            mirroring=self.mapper.mirroring,
            format_name=self.format_name,
            sha256=self.sha256,
        )

    def cpu_read(self, address: int) -> int:
        return self.mapper.cpu_read(address & 0xFFFF)

    def cpu_write(self, address: int, value: int) -> None:
        self.mapper.cpu_write(address & 0xFFFF, value & 0xFF)

    def ppu_read(self, address: int, ppu_clock: int = 0) -> int:
        address &= 0x1FFF
        self.mapper.notify_ppu_address(address, ppu_clock)
        return self.mapper.ppu_read(address)

    def ppu_write(self, address: int, value: int, ppu_clock: int = 0) -> None:
        address &= 0x1FFF
        self.mapper.notify_ppu_address(address, ppu_clock)
        self.mapper.ppu_write(address, value & 0xFF)

    def get_state(self) -> dict:
        return {
            "chr_memory": bytes(self.chr_memory) if self.chr_is_ram else b"",
            "chr_aux_ram": (
                bytes(self.chr_ram) if not self.chr_is_ram else b""
            ),
            "prg_ram": bytes(self.prg_ram),
            "mapper": self.mapper.get_state(),
        }

    def set_state(self, state: dict) -> None:
        if self.chr_is_ram:
            chr_memory = bytes(state["chr_memory"])
            if len(chr_memory) != len(self.chr_memory):
                raise CartridgeError("save state has an incompatible CHR RAM size")
            self.chr_memory[:] = chr_memory
        else:
            chr_aux_ram = bytes(state.get("chr_aux_ram", b""))
            if chr_aux_ram:
                if len(chr_aux_ram) != len(self.chr_ram):
                    raise CartridgeError(
                        "save state has an incompatible auxiliary CHR RAM size"
                    )
                self.chr_ram[:] = chr_aux_ram
        prg_ram = bytes(state["prg_ram"])
        if len(prg_ram) != len(self.prg_ram):
            raise CartridgeError("save state has an incompatible PRG RAM size")
        self.prg_ram[:] = prg_ram
        self.mapper.set_state(state["mapper"])

    def load_battery(self, path: str | Path) -> bool:
        if not self.battery or not (
            self.prg_nvram_size or self.chr_nvram_size
        ):
            return False
        save_path = Path(path)
        if not save_path.exists():
            return False
        data = save_path.read_bytes()
        if data.startswith(BATTERY_MAGIC):
            header_end = len(BATTERY_MAGIC) + 8
            if len(data) < header_end:
                raise CartridgeError("battery save header is truncated")
            prg_size = int.from_bytes(
                data[len(BATTERY_MAGIC):len(BATTERY_MAGIC) + 4], "little"
            )
            chr_size = int.from_bytes(
                data[len(BATTERY_MAGIC) + 4:header_end], "little"
            )
            if (
                prg_size != self.prg_nvram_size
                or chr_size != self.chr_nvram_size
                or len(data) != header_end + prg_size + chr_size
            ):
                raise CartridgeError(
                    "battery save has incompatible PRG/CHR NVRAM sizes"
                )
            cursor = header_end
            if prg_size:
                self.prg_ram[-prg_size:] = data[cursor:cursor + prg_size]
                cursor += prg_size
            if chr_size:
                self.chr_ram[-chr_size:] = data[cursor:cursor + chr_size]
            return True

        # Preserve the raw .sav format used by earlier Cartaconda versions
        # when the cartridge contains only one all-battery PRG RAM window.
        if (
            self.chr_nvram_size
            or self.prg_nvram_size != len(self.prg_ram)
            or len(data) != self.prg_nvram_size
        ):
            raise CartridgeError(
                "legacy battery save is incompatible with this cartridge"
            )
        self.prg_ram[:] = data
        return True

    def save_battery(self, path: str | Path) -> bool:
        if not self.battery or not (
            self.prg_nvram_size or self.chr_nvram_size
        ):
            return False
        save_path = Path(path)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = save_path.with_suffix(save_path.suffix + ".tmp")
        if (
            not self.chr_nvram_size
            and self.prg_nvram_size == len(self.prg_ram)
        ):
            payload = bytes(self.prg_ram)
        else:
            payload = (
                BATTERY_MAGIC
                + self.prg_nvram_size.to_bytes(4, "little")
                + self.chr_nvram_size.to_bytes(4, "little")
                + (
                    bytes(self.prg_ram[-self.prg_nvram_size:])
                    if self.prg_nvram_size
                    else b""
                )
                + (
                    bytes(self.chr_ram[-self.chr_nvram_size:])
                    if self.chr_nvram_size
                    else b""
                )
            )
        temporary.write_bytes(payload)
        temporary.replace(save_path)
        return True
