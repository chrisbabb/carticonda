from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FlatBus:
    memory: bytearray
    irq_pending: bool = False
    writes: list[tuple[int, int]] = field(default_factory=list)
    reads: list[int] = field(default_factory=list)

    @classmethod
    def with_program(cls, program: bytes, start: int = 0x8000) -> "FlatBus":
        memory = bytearray(0x10000)
        memory[start:start + len(program)] = program
        memory[0xFFFA:0x10000] = bytes(
            (start & 0xFF, start >> 8, start & 0xFF, start >> 8, start & 0xFF, start >> 8)
        )
        return cls(memory)

    def read(self, address: int) -> int:
        address &= 0xFFFF
        self.reads.append(address)
        return self.memory[address]

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        self.writes.append((address, value))
        self.memory[address] = value


def make_ines(
    *,
    mapper: int = 0,
    prg_banks: int = 1,
    chr_banks: int = 1,
    vertical: bool = False,
    battery: bool = False,
    program: bytes | None = None,
    fill_banks: bool = False,
) -> bytes:
    flags6 = ((mapper & 0x0F) << 4) | int(vertical) | (int(battery) << 1)
    flags7 = mapper & 0xF0
    header = b"NES\x1a" + bytes((prg_banks, chr_banks, flags6, flags7)) + bytes(8)
    prg = bytearray(prg_banks * 0x4000)
    if fill_banks:
        for bank in range(prg_banks):
            start = bank * 0x4000
            prg[start:start + 0x4000] = bytes((bank & 0xFF,)) * 0x4000
    if program:
        prg[:len(program)] = program
    vector = 0x8000
    for offset in (len(prg) - 6, len(prg) - 4, len(prg) - 2):
        prg[offset:offset + 2] = bytes((vector & 0xFF, vector >> 8))
    chr_data = bytes(chr_banks * 0x2000)
    return header + bytes(prg) + chr_data
