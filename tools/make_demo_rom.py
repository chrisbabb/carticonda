#!/usr/bin/env python3
"""Generate an original NROM demo/diagnostic cartridge."""

from __future__ import annotations

import argparse
from pathlib import Path


class Assembler:
    OPCODES = {
        ("SEI", "imp"): 0x78, ("CLI", "imp"): 0x58,
        ("CLD", "imp"): 0xD8,
        ("LDX", "imm"): 0xA2, ("LDY", "imm"): 0xA0, ("LDA", "imm"): 0xA9,
        ("LDA", "zp"): 0xA5, ("LDA", "zpx"): 0xB5,
        ("LDA", "abs"): 0xAD, ("LDA", "abx"): 0xBD,
        ("STX", "zp"): 0x86, ("STX", "abs"): 0x8E, ("STA", "zp"): 0x85,
        ("STA", "zpx"): 0x95, ("STA", "abs"): 0x8D, ("STA", "abx"): 0x9D,
        ("TXS", "imp"): 0x9A, ("INX", "imp"): 0xE8, ("DEX", "imp"): 0xCA,
        ("DEY", "imp"): 0x88, ("TXA", "imp"): 0x8A, ("PHA", "imp"): 0x48,
        ("PLA", "imp"): 0x68, ("TAX", "imp"): 0xAA, ("TAY", "imp"): 0xA8,
        ("TYA", "imp"): 0x98, ("BIT", "abs"): 0x2C,
        ("AND", "imm"): 0x29, ("EOR", "imm"): 0x49,
        ("CLC", "imp"): 0x18, ("ADC", "imm"): 0x69,
        ("CMP", "imm"): 0xC9, ("INC", "zp"): 0xE6,
        ("LSR", "acc"): 0x4A, ("ROR", "acc"): 0x6A,
        ("ROL", "zp"): 0x26, ("RTI", "imp"): 0x40,
        ("RTS", "imp"): 0x60, ("JSR", "abs"): 0x20,
        ("BPL", "rel"): 0x10, ("BCS", "rel"): 0xB0,
        ("BNE", "rel"): 0xD0, ("BEQ", "rel"): 0xF0,
        ("BMI", "rel"): 0x30, ("JMP", "abs"): 0x4C,
    }

    def __init__(self, origin: int = 0x8000) -> None:
        self.origin = origin
        self.code = bytearray()
        self.labels: dict[str, int] = {}
        self.fixups: list[tuple[int, str, str]] = []

    @property
    def pc(self) -> int:
        return self.origin + len(self.code)

    def label(self, name: str) -> None:
        self.labels[name] = self.pc

    def emit(self, mnemonic: str, mode: str = "imp", operand: int | str | None = None) -> None:
        self.code.append(self.OPCODES[(mnemonic, mode)])
        if mode in ("imm", "zp", "zpx"):
            self.code.append(int(operand) & 0xFF)
        elif mode in ("abs", "abx"):
            if isinstance(operand, str):
                self.fixups.append((len(self.code), operand, "abs"))
                self.code.extend((0, 0))
            else:
                value = int(operand)
                self.code.extend((value & 0xFF, value >> 8))
        elif mode == "rel":
            self.fixups.append((len(self.code), str(operand), "rel"))
            self.code.append(0)
        elif operand is not None:
            raise ValueError(f"{mnemonic} {mode} does not take an operand")

    def data(self, values: bytes | bytearray | tuple[int, ...]) -> None:
        self.code.extend(values)

    def finish(self) -> bytes:
        for offset, label, kind in self.fixups:
            target = self.labels[label]
            if kind == "abs":
                self.code[offset] = target & 0xFF
                self.code[offset + 1] = target >> 8
            else:
                displacement = target - (self.origin + offset + 1)
                if not -128 <= displacement <= 127:
                    raise ValueError(f"branch to {label} is out of range")
                self.code[offset] = displacement & 0xFF
        return bytes(self.code)


def build_rom() -> bytes:
    asm = Assembler()
    asm.label("reset")
    asm.emit("SEI")
    asm.emit("CLD")
    asm.emit("LDX", "imm", 0x40)
    asm.emit("STX", "abs", 0x4017)
    asm.emit("LDX", "imm", 0xFF)
    asm.emit("TXS")
    asm.emit("INX")
    asm.emit("STX", "abs", 0x2000)
    asm.emit("STX", "abs", 0x2001)
    asm.emit("STX", "abs", 0x4010)
    asm.label("vblank1")
    asm.emit("BIT", "abs", 0x2002)
    asm.emit("BPL", "rel", "vblank1")

    asm.emit("LDA", "imm", 0)
    asm.emit("TAX")
    asm.label("clear")
    for page in range(8):
        asm.emit("STA", "abx", page << 8)
    asm.emit("INX")
    asm.emit("BNE", "rel", "clear")

    asm.label("vblank2")
    asm.emit("BIT", "abs", 0x2002)
    asm.emit("BPL", "rel", "vblank2")
    asm.emit("LDA", "imm", 0x3F)
    asm.emit("STA", "abs", 0x2006)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2006)
    asm.emit("LDX", "imm", 0)
    asm.label("palette_loop")
    asm.emit("LDA", "abx", "palette")
    asm.emit("STA", "abs", 0x2007)
    asm.emit("INX")
    # CPX is represented directly to keep the tiny assembler intentionally tiny.
    asm.data((0xE0, 0x20))
    asm.emit("BNE", "rel", "palette_loop")

    asm.emit("LDA", "imm", 0x20)
    asm.emit("STA", "abs", 0x2006)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2006)
    asm.emit("LDY", "imm", 4)
    asm.label("name_page")
    asm.emit("LDX", "imm", 0)
    asm.label("name_loop")
    asm.emit("TXA")
    asm.emit("AND", "imm", 1)
    asm.emit("CLC")
    asm.emit("ADC", "imm", 1)
    asm.emit("STA", "abs", 0x2007)
    asm.emit("INX")
    asm.emit("BNE", "rel", "name_loop")
    asm.emit("DEY")
    asm.emit("BNE", "rel", "name_page")
    asm.emit("LDA", "imm", 0x80)
    asm.emit("STA", "abs", 0x2000)
    asm.emit("LDA", "imm", 0x0A)
    asm.emit("STA", "abs", 0x2001)
    asm.label("forever")
    asm.emit("JMP", "abs", "forever")

    asm.label("nmi")
    asm.emit("PHA")
    asm.emit("TXA")
    asm.emit("PHA")
    asm.emit("LDA", "imm", 1)
    asm.emit("STA", "abs", 0x4016)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x4016)
    asm.emit("STA", "zp", 0)
    asm.emit("LDX", "imm", 8)
    asm.label("read_pad")
    asm.emit("LDA", "abs", 0x4016)
    asm.emit("LSR", "acc")
    asm.emit("ROL", "zp", 0)
    asm.emit("DEX")
    asm.emit("BNE", "rel", "read_pad")
    asm.emit("LDA", "imm", 0x3F)
    asm.emit("STA", "abs", 0x2006)
    asm.emit("LDA", "imm", 1)
    asm.emit("STA", "abs", 0x2006)
    asm.emit("LDA", "zp", 0)
    asm.emit("BMI", "rel", "a_pressed")
    asm.emit("LDA", "imm", 0x21)
    asm.emit("BNE", "rel", "write_color")
    asm.label("a_pressed")
    asm.emit("LDA", "imm", 0x16)
    asm.label("write_color")
    asm.emit("STA", "abs", 0x2007)
    # PPUADDR writes replace the temporary nametable bits. Reassert nametable 0
    # before the scroll pair so the next pre-render transfer returns to $2000.
    asm.emit("LDA", "imm", 0x80)
    asm.emit("STA", "abs", 0x2000)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2005)
    asm.emit("STA", "abs", 0x2005)
    asm.emit("PLA")
    asm.emit("TAX")
    asm.emit("PLA")
    asm.emit("RTI")

    asm.label("palette")
    asm.data((
        0x0F, 0x21, 0x11, 0x30, 0x0F, 0x16, 0x27, 0x30,
        0x0F, 0x19, 0x29, 0x30, 0x0F, 0x12, 0x22, 0x30,
        0x0F, 0x21, 0x11, 0x30, 0x0F, 0x16, 0x27, 0x30,
        0x0F, 0x19, 0x29, 0x30, 0x0F, 0x12, 0x22, 0x30,
    ))
    code = asm.finish()
    if len(code) > 0x3FFA:
        raise ValueError("demo program does not fit in NROM-128")
    prg = bytearray((0xEA,) * 0x4000)
    prg[:len(code)] = code
    nmi = asm.labels["nmi"]
    reset = asm.labels["reset"]
    for offset, vector in ((0x3FFA, nmi), (0x3FFC, reset), (0x3FFE, reset)):
        prg[offset:offset + 2] = bytes((vector & 0xFF, vector >> 8))

    chr_rom = bytearray(0x2000)
    # Tile 1: vertical bars. Tile 2: a two-plane checker pattern.
    chr_rom[16:24] = bytes((0xAA,) * 8)
    chr_rom[24:32] = bytes((0x00,) * 8)
    chr_rom[32:40] = bytes((0xAA, 0x55) * 4)
    chr_rom[40:48] = bytes((0x55, 0xAA) * 4)
    header = b"NES\x1a" + bytes((1, 1, 0, 0)) + bytes(8)
    return header + bytes(prg) + bytes(chr_rom)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=Path("demo.nes"))
    args = parser.parse_args()
    data = build_rom()
    args.output.write_bytes(data)
    print(f"Wrote original demo cartridge: {args.output} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
