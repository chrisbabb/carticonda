#!/usr/bin/env python3
"""Benchmark deterministic optimized and reference emulator paths."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Callable

from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.console import Console
from nes_from_scratch.constants import NTSC_FRAME_RATE
from nes_from_scratch.rewind import RewindBuffer

if __package__:
    from .make_demo_rom import Assembler, build_rom
else:
    from make_demo_rom import Assembler, build_rom


@dataclass(frozen=True)
class Result:
    mode: str
    frames: int
    seconds: float
    fps: float
    realtime_factor: float
    frame_sha256: str


def measure(
    mode: str,
    *,
    fast_ppu: bool,
    frames: int,
    disable_idle_batch: bool = False,
    disable_device_batch: bool = False,
    rom_data: bytes | None = None,
    force_rendering: bool = False,
    setup_console: Callable[[Console], None] | None = None,
    before_frame: Callable[[Console, int], None] | None = None,
    rewind_buffer: RewindBuffer | None = None,
) -> Result:
    console = Console(
        Cartridge.from_bytes(
            build_rom() if rom_data is None else rom_data,
            title=f"benchmark-{mode}",
        ),
        fast_ppu=fast_ppu,
        idle_batching=not disable_idle_batch,
        device_batching=not disable_device_batch,
    )
    if force_rendering:
        console.ppu.mask = 0x18
    if setup_console is not None:
        setup_console(console)
    frame = b""
    for _ in range(5 if fast_ppu else 1):
        frame = console.run_frame()
        console.apu.drain_samples()
    if rewind_buffer is not None:
        rewind_buffer.clear()
        rewind_buffer.capture(console, force=True)

    started = perf_counter()
    for frame_number in range(frames):
        if before_frame is not None:
            before_frame(console, frame_number)
        frame = console.run_frame()
        console.apu.drain_samples()
        if rewind_buffer is not None:
            rewind_buffer.capture(console)
    seconds = perf_counter() - started
    fps = frames / seconds
    return Result(
        mode=mode,
        frames=frames,
        seconds=seconds,
        fps=fps,
        realtime_factor=fps / NTSC_FRAME_RATE,
        frame_sha256=hashlib.sha256(frame).hexdigest(),
    )


def build_looping_dmc_rom() -> bytes:
    """Build an original NROM workload with the fastest looping DMC rate."""
    program = bytes(
        (
            0x78,                    # SEI
            0xA9, 0x4F,              # LDA #$4F: loop, fastest DMC rate
            0x8D, 0x10, 0x40,        # STA $4010
            0x8D, 0x12, 0x40,        # STA $4012
            0x8D, 0x13, 0x40,        # STA $4013
            0xA9, 0x10,              # LDA #$10
            0x8D, 0x15, 0x40,        # STA $4015
            0xE8,                    # INX
            0xD0, 0xFD,              # BNE $8011
            0x4C, 0x11, 0x80,        # JMP $8011
        )
    )
    header = b"NES\x1a" + bytes((1, 1)) + bytes(10)
    prg = bytearray(0x4000)
    prg[:len(program)] = program
    for offset in (0x3FFA, 0x3FFC, 0x3FFE):
        prg[offset:offset + 2] = b"\x00\x80"
    return header + bytes(prg) + bytes(0x2000)


def build_gameplay_stress_rom() -> bytes:
    """Build an original side-scroller-shaped NROM performance workload.

    It combines continuous game-logic-style zero-page work with an NMI that
    polls the controller, updates scrolling and pulse pitch, and performs the
    conventional $0200 OAM DMA every frame. Sixty-four moving sprite records
    exercise the same broad CPU/PPU/APU paths as an action platformer without
    containing any commercial game data.
    """
    asm = Assembler()
    asm.label("reset")
    asm.emit("SEI")
    asm.emit("CLD")
    asm.emit("LDX", "imm", 0xFF)
    asm.emit("TXS")
    asm.emit("INX")
    asm.emit("STX", "abs", 0x2000)
    asm.emit("STX", "abs", 0x2001)

    # Enable and seed two pulses, triangle, and noise.
    asm.emit("LDA", "imm", 0x0F)
    asm.emit("STA", "abs", 0x4015)
    for address, value in (
        (0x4000, 0x9F), (0x4002, 0x80), (0x4003, 0x18),
        (0x4004, 0x5A), (0x4006, 0x40), (0x4007, 0x20),
        (0x4008, 0xBF), (0x400A, 0x20), (0x400B, 0x18),
        (0x400C, 0x1C), (0x400E, 0x04), (0x400F, 0x18),
    ):
        asm.emit("LDA", "imm", value)
        asm.emit("STA", "abs", address)

    # Build 64 OAM records in CPU page $02.
    asm.emit("LDX", "imm", 0)
    asm.label("init_oam")
    asm.emit("TXA")
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("LDA", "imm", 1)
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("TXA")
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("BNE", "rel", "init_oam")

    asm.emit("LDA", "imm", 0x80)
    asm.emit("STA", "abs", 0x2000)
    asm.emit("LDA", "imm", 0x1E)
    asm.emit("STA", "abs", 0x2001)

    # Keep the CPU busy with the load/ALU/store mix used by ordinary 6502 game
    # logic while the NMI owns frame-synchronous I/O.
    asm.label("main")
    asm.emit("LDX", "imm", 0x3F)
    asm.label("logic_loop")
    asm.emit("LDA", "zpx", 0x00)
    asm.emit("CLC")
    asm.emit("ADC", "imm", 1)
    asm.emit("EOR", "imm", 0x5A)
    asm.emit("AND", "imm", 0x7F)
    asm.emit("STA", "zpx", 0x00)
    asm.emit("DEX")
    asm.emit("BPL", "rel", "logic_loop")
    asm.emit("JSR", "abs", "logic_tail")
    asm.emit("JMP", "abs", "main")

    asm.label("logic_tail")
    asm.emit("LDA", "zp", 0x10)
    asm.emit("CMP", "imm", 0x40)
    asm.emit("ROR", "acc")
    asm.emit("STA", "zp", 0x10)
    asm.emit("RTS")

    asm.label("nmi")
    asm.emit("PHA")
    asm.emit("TXA")
    asm.emit("PHA")
    asm.emit("TYA")
    asm.emit("PHA")
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2003)
    asm.emit("LDA", "imm", 2)
    asm.emit("STA", "abs", 0x4014)
    asm.emit("INC", "zp", 0xF0)
    asm.emit("LDA", "zp", 0xF0)
    asm.emit("STA", "abs", 0x0203)
    asm.emit("STA", "abs", 0x2005)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2005)
    asm.emit("LDA", "zp", 0xF0)
    asm.emit("STA", "abs", 0x4002)
    asm.emit("LDA", "imm", 1)
    asm.emit("STA", "abs", 0x4016)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x4016)
    asm.emit("LDX", "imm", 8)
    asm.label("read_pad")
    asm.emit("LDA", "abs", 0x4016)
    asm.emit("LSR", "acc")
    asm.emit("ROL", "zp", 0xF1)
    asm.emit("DEX")
    asm.emit("BNE", "rel", "read_pad")
    asm.emit("PLA")
    asm.emit("TAY")
    asm.emit("PLA")
    asm.emit("TAX")
    asm.emit("PLA")
    asm.emit("RTI")

    code = asm.finish()
    if len(code) > 0x3FFA:
        raise ValueError("gameplay stress program does not fit in NROM-128")
    prg = bytearray((0xEA,) * 0x4000)
    prg[:len(code)] = code
    nmi = asm.labels["nmi"]
    reset = asm.labels["reset"]
    for offset, vector in ((0x3FFA, nmi), (0x3FFC, reset), (0x3FFE, reset)):
        prg[offset:offset + 2] = bytes((vector & 0xFF, vector >> 8))

    chr_rom = bytearray(0x2000)
    chr_rom[16:24] = bytes((0xAA, 0x55) * 4)
    chr_rom[24:32] = bytes((0x55, 0xAA) * 4)
    header = b"NES\x1a" + bytes((1, 1, 0, 0)) + bytes(8)
    return header + bytes(prg) + bytes(chr_rom)


def build_mmc2_gameplay_stress_rom() -> bytes:
    """Build an original latch-heavy Mapper 9 action workload.

    Code executes from MMC2's fixed final 8 KiB bank while the NMI changes the
    switchable PRG bank. The main loop mirrors ordinary game-logic traffic and
    the NMI performs OAM DMA, scrolling, controller I/O, audio modulation, and
    one mapper write per frame.
    """
    asm = Assembler(origin=0xE000)
    asm.label("reset")
    asm.emit("SEI")
    asm.emit("CLD")
    asm.emit("LDX", "imm", 0xFF)
    asm.emit("TXS")
    asm.emit("INX")
    asm.emit("STX", "abs", 0x2000)
    asm.emit("STX", "abs", 0x2001)

    for address, value in (
        (0xB000, 0x00),
        (0xC000, 0x01),
        (0xD000, 0x02),
        (0xE000, 0x03),
        (0xF000, 0x00),
    ):
        asm.emit("LDA", "imm", value)
        asm.emit("STA", "abs", address)

    asm.emit("LDA", "imm", 0x0F)
    asm.emit("STA", "abs", 0x4015)
    for address, value in (
        (0x4000, 0x9F), (0x4002, 0x80), (0x4003, 0x18),
        (0x4004, 0x5A), (0x4006, 0x40), (0x4007, 0x20),
        (0x4008, 0xBF), (0x400A, 0x20), (0x400B, 0x18),
        (0x400C, 0x1C), (0x400E, 0x04), (0x400F, 0x18),
    ):
        asm.emit("LDA", "imm", value)
        asm.emit("STA", "abs", address)

    asm.emit("LDX", "imm", 0)
    asm.label("init_oam")
    asm.emit("TXA")
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("LDA", "imm", 0xFD)
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("TXA")
    asm.emit("STA", "abx", 0x0200)
    asm.emit("INX")
    asm.emit("BNE", "rel", "init_oam")

    asm.emit("LDA", "imm", 0x80)
    asm.emit("STA", "abs", 0x2000)
    asm.emit("LDA", "imm", 0x1E)
    asm.emit("STA", "abs", 0x2001)

    asm.label("main")
    asm.emit("LDX", "imm", 0x3F)
    asm.label("logic_loop")
    asm.emit("LDA", "zpx", 0x00)
    asm.emit("CLC")
    asm.emit("ADC", "imm", 1)
    asm.emit("EOR", "imm", 0x5A)
    asm.emit("AND", "imm", 0x7F)
    asm.emit("STA", "zpx", 0x00)
    asm.emit("DEX")
    asm.emit("BPL", "rel", "logic_loop")
    asm.emit("JSR", "abs", "logic_tail")
    asm.emit("JMP", "abs", "main")

    asm.label("logic_tail")
    asm.emit("LDA", "zp", 0x10)
    asm.emit("CMP", "imm", 0x40)
    asm.emit("ROR", "acc")
    asm.emit("STA", "zp", 0x10)
    asm.emit("RTS")

    asm.label("nmi")
    asm.emit("PHA")
    asm.emit("TXA")
    asm.emit("PHA")
    asm.emit("TYA")
    asm.emit("PHA")
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2003)
    asm.emit("LDA", "imm", 2)
    asm.emit("STA", "abs", 0x4014)
    asm.emit("INC", "zp", 0xF0)
    asm.emit("LDA", "zp", 0xF0)
    asm.emit("STA", "abs", 0x0203)
    asm.emit("STA", "abs", 0x2005)
    asm.emit("AND", "imm", 0x0F)
    asm.emit("STA", "abs", 0xA000)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x2005)
    asm.emit("LDA", "zp", 0xF0)
    asm.emit("STA", "abs", 0x4002)
    asm.emit("LDA", "imm", 1)
    asm.emit("STA", "abs", 0x4016)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0x4016)
    asm.emit("LDX", "imm", 8)
    asm.label("read_pad")
    asm.emit("LDA", "abs", 0x4016)
    asm.emit("LSR", "acc")
    asm.emit("ROL", "zp", 0xF1)
    asm.emit("DEX")
    asm.emit("BNE", "rel", "read_pad")
    asm.emit("PLA")
    asm.emit("TAY")
    asm.emit("PLA")
    asm.emit("TAX")
    asm.emit("PLA")
    asm.emit("RTI")

    code = asm.finish()
    if len(code) > 0x1FFA:
        raise ValueError("MMC2 stress program does not fit the fixed PRG bank")
    prg = bytearray((0xEA,) * 0x20000)
    fixed_start = len(prg) - 0x2000
    prg[fixed_start:fixed_start + len(code)] = code
    for offset, vector in (
        (len(prg) - 6, asm.labels["nmi"]),
        (len(prg) - 4, asm.labels["reset"]),
        (len(prg) - 2, asm.labels["reset"]),
    ):
        prg[offset:offset + 2] = bytes((vector & 0xFF, vector >> 8))

    chr_rom = bytearray(0x20000)
    for bank in range(32):
        bank_start = bank * 0x1000
        for row in range(0x1000):
            chr_rom[bank_start + row] = (
                bank * 29 + row * 13 + (row >> 4)
            ) & 0xFF
    header = b"NES\x1a" + bytes((8, 16, 0x90, 0x00)) + bytes(8)
    return header + bytes(prg) + bytes(chr_rom)


def build_mmc3_gameplay_stress_rom() -> bytes:
    """Build an original Mapper 4 transition-heavy action workload.

    The fixed-bank program and data are shared with the Mapper 9 stress image,
    but the header selects MMC3. Its per-frame $A000 write alternates
    mirroring, deliberately exercising the costly CHR/CIRAM transition that
    occurs while an action game changes scenes.
    """
    image = bytearray(build_mmc2_gameplay_stress_rom())
    image[6] = 0x40
    image[7] = 0x00
    return bytes(image)


def build_mmc3_cross_slot_scroll_rom() -> bytes:
    """Build scrolling MMC3 gameplay that calls an unchanged $C000 slot.

    The fixed-bank main loop repeatedly enters RAM-heavy game logic in the
    fixed second-last 8 KiB bank. No mapper write accompanies that control-flow
    transition. It guards the field regression where a newly entered stable
    code slot received only the first of two classification observations and
    consequently ran literally for the rest of the level.
    """
    routine = Assembler(origin=0xC000)
    routine.label("update_objects")
    routine.emit("LDX", "imm", 0)
    routine.label("object_loop")
    routine.emit("LDA", "abx", 0x0200)
    routine.emit("EOR", "imm", 0x5A)
    routine.emit("CLC")
    routine.emit("ADC", "imm", 1)
    routine.emit("STA", "abx", 0x0300)
    routine.emit("INX")
    routine.emit("BNE", "rel", "object_loop")
    routine.emit("RTS")
    routine_code = routine.finish()

    fixed = Assembler(origin=0xE000)
    fixed.label("reset")
    fixed.emit("SEI")
    fixed.emit("CLD")
    fixed.emit("LDA", "imm", 0x40)
    fixed.emit("STA", "abs", 0x4017)
    fixed.emit("LDA", "imm", 0x80)
    fixed.emit("STA", "abs", 0x2000)
    fixed.emit("LDA", "imm", 0x1E)
    fixed.emit("STA", "abs", 0x2001)
    fixed.emit("CLI")
    fixed.label("main")
    fixed.emit("JSR", "abs", 0xC000)
    fixed.emit("JMP", "abs", "main")

    fixed.label("nmi")
    fixed.emit("PHA")
    fixed.emit("TXA")
    fixed.emit("PHA")
    fixed.emit("TYA")
    fixed.emit("PHA")
    fixed.emit("INC", "zp", 0xF0)
    fixed.emit("LDA", "zp", 0xF0)
    fixed.emit("STA", "abs", 0x2005)
    fixed.emit("LDA", "imm", 0)
    fixed.emit("STA", "abs", 0x2005)
    fixed.emit("LDA", "imm", 2)
    fixed.emit("STA", "abs", 0x4014)
    fixed.emit("PLA")
    fixed.emit("TAY")
    fixed.emit("PLA")
    fixed.emit("TAX")
    fixed.emit("PLA")
    fixed.emit("RTI")
    fixed_code = fixed.finish()

    prg = bytearray((0xEA,) * 0x20000)
    second_last_start = len(prg) - 0x4000
    fixed_start = len(prg) - 0x2000
    prg[second_last_start:second_last_start + len(routine_code)] = (
        routine_code
    )
    prg[fixed_start:fixed_start + len(fixed_code)] = fixed_code
    for offset, vector in (
        (len(prg) - 6, fixed.labels["nmi"]),
        (len(prg) - 4, fixed.labels["reset"]),
        (len(prg) - 2, fixed.labels["reset"]),
    ):
        prg[offset:offset + 2] = bytes((vector & 0xFF, vector >> 8))
    header = b"NES\x1a" + bytes((8, 1, 0x40, 0x00)) + bytes(8)
    return header + bytes(prg) + bytes(0x2000)


def build_mmc3_irq_stress_rom() -> bytes:
    """Build an original MMC3 scanline-IRQ and scheduler-loop workload.

    The program selects different background and sprite pattern tables so one
    filtered A12 rise clocks the counter per scanline. It acknowledges and
    reloads the IRQ from a fixed-bank handler while a four-record cooperative
    scheduler waits in its fixed bank. This reproduces the CPU/IRQ shape used
    by late action games without containing commercial code or data.
    """
    asm = Assembler(origin=0xE000)
    asm.label("reset")
    asm.emit("SEI")
    asm.emit("CLD")
    asm.emit("LDA", "imm", 0x40)
    asm.emit("STA", "abs", 0x4017)  # Inhibit the unrelated APU frame IRQ.
    asm.emit("LDA", "imm", 0x08)
    asm.emit("STA", "abs", 0x2000)  # Sprites $1000; background $0000.
    asm.emit("LDA", "imm", 0x18)
    asm.emit("STA", "abs", 0x2001)
    asm.emit("LDA", "imm", 100)
    asm.emit("STA", "abs", 0xC000)
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0xC001)
    asm.emit("STA", "abs", 0xE001)
    asm.emit("CLI")
    asm.label("scheduler")
    asm.emit("LDX", "imm", 0)
    asm.emit("STX", "zp", 0x70)
    asm.emit("LDY", "imm", 4)
    asm.label("scan")
    asm.emit("LDA", "zpx", 0x20)
    asm.emit("CMP", "imm", 4)
    asm.emit("BCS", "rel", "process")
    asm.emit("INX")
    asm.emit("INX")
    asm.emit("INX")
    asm.emit("INX")
    asm.emit("DEY")
    asm.emit("BNE", "rel", "scan")
    asm.emit("JMP", "abs", "scheduler")
    asm.label("process")
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "zpx", 0x20)
    asm.emit("JMP", "abs", "scheduler")

    asm.label("irq")
    asm.emit("PHA")
    asm.emit("LDA", "imm", 0)
    asm.emit("STA", "abs", 0xE000)
    asm.emit("STA", "abs", 0xC001)
    asm.emit("STA", "abs", 0xE001)
    asm.emit("PLA")
    asm.emit("RTI")

    code = asm.finish()
    if len(code) > 0x1FFA:
        raise ValueError("MMC3 IRQ stress program does not fit fixed bank")
    prg = bytearray((0xEA,) * 0x20000)
    fixed_start = len(prg) - 0x2000
    prg[fixed_start:fixed_start + len(code)] = code
    for offset, vector in (
        (len(prg) - 6, asm.labels["reset"]),
        (len(prg) - 4, asm.labels["reset"]),
        (len(prg) - 2, asm.labels["irq"]),
    ):
        prg[offset:offset + 2] = bytes((vector & 0xFF, vector >> 8))
    header = b"NES\x1a" + bytes((8, 1, 0x40, 0x00)) + bytes(8)
    return header + bytes(prg) + bytes(0x2000)


def build_mmc2_forced_blank_stream_rom() -> bytes:
    """Build a Mapper 9 black-screen PPU-streaming and bell workload.

    The fixed-bank program leaves PPUMASK disabled, starts one pulse channel,
    and continuously writes changing bytes through PPUDATA. This models the
    setup phase before an action scene becomes visible without containing
    commercial code, graphics, or audio data.
    """
    program = bytes(
        (
            0x78,                    # SEI
            0xA9, 0x9F,              # LDA #$9F
            0x8D, 0x00, 0x40,        # STA $4000: pulse envelope/duty
            0xA9, 0x40,              # LDA #$40
            0x8D, 0x02, 0x40,        # STA $4002: pulse timer low
            0xA9, 0x08,              # LDA #$08
            0x8D, 0x03, 0x40,        # STA $4003: pulse timer high/length
            0xA9, 0x01,              # LDA #$01
            0x8D, 0x15, 0x40,        # STA $4015: enable pulse 1
            0xA9, 0x20,              # LDA #$20
            0x8D, 0x06, 0x20,        # STA $2006: nametable high
            0xA9, 0x00,              # LDA #$00
            0x8D, 0x06, 0x20,        # STA $2006: nametable low
            0xA2, 0x00,              # LDX #$00
            0x8A,                    # loop: TXA
            0x8D, 0x07, 0x20,        # STA $2007
            0xE8,                    # INX
            0xD0, 0xF9,              # BNE loop
            0x4C, 0x21, 0xE0,        # JMP loop
        )
    )
    prg = bytearray((0xEA,) * 0x20000)
    fixed_start = len(prg) - 0x2000
    prg[fixed_start:fixed_start + len(program)] = program
    for offset in (len(prg) - 6, len(prg) - 4, len(prg) - 2):
        prg[offset:offset + 2] = b"\x00\xE0"
    header = b"NES\x1a" + bytes((8, 1, 0x90, 0x00)) + bytes(8)
    return header + bytes(prg) + bytes(0x2000)


def build_mmc3_register_churn_rom() -> bytes:
    """Build an MMC3 workload that changes a visible CHR bank continuously.

    This isolates a 2.0 regression where every mapper invalidation discarded
    an eagerly composed sprite-zero status line. The scheduler could render
    the same scanline hundreds of times before the beam reached dot 256.
    """
    program = bytes(
        (
            0x78,                    # SEI
            0xA9, 0x1E,              # LDA #$1E
            0x8D, 0x01, 0x20,        # STA $2001: rendering + left edges
            0xA9, 0x00,              # LDA #$00
            0x8D, 0x00, 0x80,        # STA $8000: select CHR register 0
            0xE6, 0x00,              # loop: INC $00
            0xA5, 0x00,              # LDA $00
            0x8D, 0x01, 0x80,        # STA $8001: change visible CHR map
            0x4C, 0x0B, 0xE0,        # JMP loop
        )
    )
    prg = bytearray((0xEA,) * 0x20000)
    fixed_start = len(prg) - 0x2000
    prg[fixed_start:fixed_start + len(program)] = program
    for offset in (len(prg) - 6, len(prg) - 4, len(prg) - 2):
        prg[offset:offset + 2] = b"\x00\xE0"
    header = b"NES\x1a" + bytes((8, 1, 0x40, 0x00)) + bytes(8)
    return header + bytes(prg) + bytes(0x2000)


def build_mmc3_prg_bank_churn_rom() -> bytes:
    """Build an MMC3 scene-transition workload with rapid PRG switching.

    The fixed $E000 code bank continuously selects register 6 and changes the
    $8000 PRG slot. The switched banks contain distinct original filler so an
    optimizer cannot collapse them by value. This reproduces the 2.0 startup
    pause without using any commercial ROM data.
    """
    program = bytes(
        (
            0x78,                    # SEI
            0xA9, 0x06,              # LDA #$06: select PRG register 6
            0x8D, 0x00, 0x80,        # STA $8000
            0xE6, 0x00,              # loop: INC $00
            0xA5, 0x00,              # LDA $00
            0x8D, 0x01, 0x80,        # STA $8001: change $8000 PRG bank
            0x4C, 0x06, 0xE0,        # JMP loop
        )
    )
    prg = bytearray((0xEA,) * 0x40000)
    for bank in range(32):
        prg[bank * 0x2000] = bank
    fixed_start = len(prg) - 0x2000
    prg[fixed_start:fixed_start + len(program)] = program
    for offset in (len(prg) - 6, len(prg) - 4, len(prg) - 2):
        prg[offset:offset + 2] = b"\x00\xE0"
    header = b"NES\x1a" + bytes((16, 1, 0x40, 0x00)) + bytes(8)
    return header + bytes(prg) + bytes(0x2000)


def configure_mmc2_gameplay_stress(console: Console) -> None:
    """Populate CIRAM/palette for repeated MMC2 latch transitions."""
    ppu = console.ppu
    for table in range(4):
        table_start = table * 0x400
        for tile_y in range(30):
            row_start = table_start + tile_y * 32
            for tile_x in range(32):
                selector = (tile_x + tile_y * 3 + table) & 7
                if selector == 0:
                    tile = 0xFD
                elif selector == 4:
                    tile = 0xFE
                else:
                    tile = (tile_x * 11 + tile_y * 17 + table * 23) & 0xFF
                ppu.nametable_ram[row_start + tile_x] = tile
        attribute_start = table_start + 0x3C0
        for index in range(64):
            ppu.nametable_ram[attribute_start + index] = (
                index * 0x55 + table * 0x11
            ) & 0xFF
    ppu.palette_ram[:] = bytes(
        (
            0x0F, 0x01, 0x21, 0x31,
            0x0F, 0x06, 0x16, 0x26,
            0x0F, 0x09, 0x19, 0x29,
            0x0F, 0x0C, 0x1C, 0x2C,
        )
        * 2
    )


def configure_mmc3_sprite_cadence(console: Console) -> None:
    """Populate a scrolling background and sparse visible sprite rows."""
    configure_mmc2_gameplay_stress(console)
    oam = console.ppu.oam
    oam[:] = bytes((0xFF, 0, 0, 0)) * 64
    for index in range(16):
        base = index * 4
        oam[base:base + 4] = bytes((
            24 + (index // 4) * 24,
            index,
            index & 3,
            32 + (index & 3) * 40,
        ))
    console.ppu.invalidate_fast_sprite_cache()


def switch_mmc3_sprite_bank(console: Console, frame_number: int) -> None:
    """Exercise moving sprites plus independent sprite-side mutations.

    This cadence mirrors the retained-line/cache ratios reported by late
    Mapper 4 action games: OAM changes every frame, one sprite palette entry
    changes periodically, and a sprite-side CHR slot changes less often while
    the scrolling background remains reusable.
    """
    ppu = console.ppu
    oam = ppu.oam
    for index in range(16):
        base = index * 4
        oam[base] = 24 + (index // 4) * 24
        oam[base + 3] = (
            32 + (index & 3) * 40 + frame_number
        ) & 0xFF
    ppu.invalidate_fast_sprite_cache()
    if not frame_number % 6:
        ppu.write_memory(
            0x3F11,
            0x10 + ((frame_number // 6) & 0x0F),
        )
    if not frame_number % 13:
        console.bus.write(0x8000, 0x02)
        console.bus.write(0x8001, (frame_number // 13 + 1) & 7)


def configure_mmc3_field_tail(console: Console) -> None:
    """Populate the combined scrolling/sprite field-tail regression."""
    configure_mmc2_gameplay_stress(console)
    oam = console.ppu.oam
    oam[:] = bytes((0xFF, 0, 0, 0)) * 64
    for index in range(16):
        base = index * 4
        oam[base:base + 4] = bytes((
            24 + (index // 4) * 24,
            index,
            index & 3,
            24 + (index & 3) * 48,
        ))
    console.ppu.invalidate_fast_sprite_cache()


def update_mmc3_field_tail(console: Console, frame_number: int) -> None:
    """Model the cache/CPU cadence from the 2.2.7 Mega Man 5 field log."""
    ppu = console.ppu
    # Keep sprites in the independent upper pattern table, then move the
    # populated OAM set while the cartridge's NMI advances horizontal scroll.
    ppu.ctrl |= 0x08
    for index in range(16):
        ppu.oam[index * 4 + 3] = (
            24 + (index & 3) * 48 + frame_number
        ) & 0xFF
    ppu.invalidate_fast_sprite_cache()
    # Match the approximate sparse-content, sprite-palette, and sprite-bank
    # cadences from the supplied field counters without using commercial data.
    if not frame_number % 6:
        address = 0x2000 + ((frame_number // 6) * 17 % 0x03C0)
        ppu.write_memory(address, (frame_number // 6 + 1) & 0xFF)
    if not frame_number % 16:
        ppu.write_memory(
            0x3F11,
            0x10 + ((frame_number // 16) & 0x0F),
        )
    if not frame_number % 14:
        console.bus.write(0x8000, 0x02)
        console.bus.write(0x8001, (frame_number // 14 + 1) & 7)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--frames",
        type=int,
        default=120,
        help="frames for the optimized fast path (default: 120)",
    )
    parser.add_argument(
        "--control-frames",
        type=int,
        default=30,
        help="frames with idle batching disabled (default: 30)",
    )
    parser.add_argument(
        "--accurate-frames",
        type=int,
        default=3,
        help="frames for the dot reference path; zero skips it (default: 3)",
    )
    parser.add_argument(
        "--synchronized-frames",
        type=int,
        default=30,
        help=(
            "frames for the instruction-synchronized fast control; "
            "zero skips it (default: 30)"
        ),
    )
    parser.add_argument(
        "--dmc-frames",
        type=int,
        default=30,
        help=(
            "frames for the rendered fastest-rate looping-DMC stress "
            "workload; zero skips it (default: 30)"
        ),
    )
    parser.add_argument(
        "--gameplay-frames",
        type=int,
        default=120,
        help=(
            "frames for the NMI/OAM-DMA/scroll/tonal-audio gameplay "
            "workload; zero skips it (default: 120)"
        ),
    )
    parser.add_argument(
        "--mmc2-frames",
        type=int,
        default=120,
        help=(
            "frames for the latch-heavy Mapper 9 gameplay workload; "
            "zero skips it (default: 120)"
        ),
    )
    parser.add_argument(
        "--mmc2-blank-frames",
        type=int,
        default=0,
        help=(
            "frames for the forced-blank Mapper 9 PPUDATA/audio regression; "
            "zero skips it (default: 0)"
        ),
    )
    parser.add_argument(
        "--mmc3-frames",
        type=int,
        default=120,
        help=(
            "frames for the transition-heavy Mapper 4 gameplay workload; "
            "zero skips it (default: 120)"
        ),
    )
    parser.add_argument(
        "--mmc3-irq-frames",
        type=int,
        default=0,
        help=(
            "frames for the active Mapper 4 scanline-IRQ regression; "
            "zero skips it (default: 0)"
        ),
    )
    parser.add_argument(
        "--mmc3-churn-frames",
        type=int,
        default=0,
        help=(
            "frames for the continuous Mapper 4 CHR-register regression; "
            "zero skips it (default: 0)"
        ),
    )
    parser.add_argument(
        "--mmc3-sprite-frames",
        type=int,
        default=0,
        help=(
            "frames for periodic Mapper 4 sprite-bank changes over an "
            "unchanged scrolling background; zero skips it (default: 0)"
        ),
    )
    parser.add_argument(
        "--mmc3-slot-frames",
        type=int,
        default=0,
        help=(
            "frames for scrolling Mapper 4 gameplay that repeatedly enters "
            "an unchanged secondary code slot; zero skips it (default: 0)"
        ),
    )
    parser.add_argument(
        "--mmc3-field-frames",
        type=int,
        default=0,
        help=(
            "frames for combined scrolling, sprite, sparse-nametable, and "
            "bank-cadence tail validation; zero skips it"
        ),
    )
    parser.add_argument(
        "--mmc3-prg-churn-frames",
        type=int,
        default=0,
        help=(
            "frames for the continuous Mapper 4 PRG-bank regression; "
            "zero skips it (default: 0)"
        ),
    )
    parser.add_argument(
        "--rewind-frames",
        type=int,
        default=120,
        help=(
            "frames for gameplay with the default compressed rewind history; "
            "zero skips it (default: 120)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    args = parser.parse_args()

    requested = [
        ("fast", True, max(1, args.frames), False, False),
        ("fast-no-idle-batch", True, max(1, args.control_frames), True, False),
    ]
    if args.synchronized_frames > 0:
        requested.append(
            (
                "fast-synchronized",
                True,
                args.synchronized_frames,
                True,
                True,
            )
        )
    results = [
        measure(
            mode,
            fast_ppu=fast_ppu,
            frames=frames,
            disable_idle_batch=disable_idle_batch,
            disable_device_batch=disable_device_batch,
        )
        for (
            mode,
            fast_ppu,
            frames,
            disable_idle_batch,
            disable_device_batch,
        ) in requested
    ]
    if args.accurate_frames > 0:
        results.append(
            measure(
                "dot-reference",
                fast_ppu=False,
                frames=args.accurate_frames,
            )
        )
    if args.dmc_frames > 0:
        results.append(
            measure(
                "fast-looping-dmc",
                fast_ppu=True,
                frames=args.dmc_frames,
                rom_data=build_looping_dmc_rom(),
                force_rendering=True,
            )
        )
    if args.gameplay_frames > 0:
        results.append(
            measure(
                "fast-gameplay",
                fast_ppu=True,
                frames=args.gameplay_frames,
                rom_data=build_gameplay_stress_rom(),
            )
        )
    if args.rewind_frames > 0:
        results.append(
            measure(
                "fast-gameplay-rewind",
                fast_ppu=True,
                frames=args.rewind_frames,
                rom_data=build_gameplay_stress_rom(),
                rewind_buffer=RewindBuffer(),
            )
        )
    if args.mmc2_frames > 0:
        results.append(
            measure(
                "fast-mmc2-gameplay",
                fast_ppu=True,
                frames=args.mmc2_frames,
                rom_data=build_mmc2_gameplay_stress_rom(),
                setup_console=configure_mmc2_gameplay_stress,
            )
        )
    if args.mmc2_blank_frames > 0:
        results.append(
            measure(
                "fast-mmc2-forced-blank",
                fast_ppu=True,
                frames=args.mmc2_blank_frames,
                rom_data=build_mmc2_forced_blank_stream_rom(),
            )
        )
    if args.mmc3_frames > 0:
        results.append(
            measure(
                "fast-mmc3-gameplay",
                fast_ppu=True,
                frames=args.mmc3_frames,
                rom_data=build_mmc3_gameplay_stress_rom(),
                setup_console=configure_mmc2_gameplay_stress,
            )
        )
    if args.mmc3_irq_frames > 0:
        results.append(
            measure(
                "fast-mmc3-irq",
                fast_ppu=True,
                frames=args.mmc3_irq_frames,
                rom_data=build_mmc3_irq_stress_rom(),
            )
        )
    if args.mmc3_sprite_frames > 0:
        results.append(
            measure(
                "fast-mmc3-sprite-cadence",
                fast_ppu=True,
                frames=args.mmc3_sprite_frames,
                rom_data=build_mmc3_irq_stress_rom(),
                setup_console=configure_mmc3_sprite_cadence,
                before_frame=switch_mmc3_sprite_bank,
            )
        )
    if args.mmc3_slot_frames > 0:
        results.append(
            measure(
                "fast-mmc3-cross-slot-scroll",
                fast_ppu=True,
                frames=args.mmc3_slot_frames,
                rom_data=build_mmc3_cross_slot_scroll_rom(),
                setup_console=configure_mmc2_gameplay_stress,
            )
        )
    if args.mmc3_field_frames > 0:
        results.append(
            measure(
                "fast-mmc3-field-tail",
                fast_ppu=True,
                frames=args.mmc3_field_frames,
                rom_data=build_mmc3_cross_slot_scroll_rom(),
                setup_console=configure_mmc3_field_tail,
                before_frame=update_mmc3_field_tail,
            )
        )
    if args.mmc3_churn_frames > 0:
        results.append(
            measure(
                "fast-mmc3-churn",
                fast_ppu=True,
                frames=args.mmc3_churn_frames,
                rom_data=build_mmc3_register_churn_rom(),
            )
        )
    if args.mmc3_prg_churn_frames > 0:
        results.append(
            measure(
                "fast-mmc3-prg-churn",
                fast_ppu=True,
                frames=args.mmc3_prg_churn_frames,
                rom_data=build_mmc3_prg_bank_churn_rom(),
            )
        )

    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            print(
                f"{result.mode:20} "
                f"{result.fps:9.2f} FPS  "
                f"{result.realtime_factor:6.2f}x real time  "
                f"({result.frames} frames / {result.seconds:.4f}s)"
            )
        hashes = {result.frame_sha256 for result in results}
        print(f"deterministic frame hash: {results[0].frame_sha256}")
        if len(hashes) != 1:
            print(
                "note: presentation modes and workloads may end on different "
                "PPU boundaries; their frame hashes need not match"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
