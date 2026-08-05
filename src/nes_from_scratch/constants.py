"""Electrical and timing constants for an NTSC Nintendo Entertainment System."""

from __future__ import annotations

from enum import Enum, IntEnum, IntFlag


CPU_HZ = 1_789_773
PPU_HZ = CPU_HZ * 3
PPU_WIDTH = 256
PPU_HEIGHT = 240
PPU_SCANLINES = 262
PPU_DOTS = 341
DEFAULT_SAMPLE_RATE = 44_100
# Rendering skips one dot on every other NTSC frame.
NTSC_FRAME_RATE = PPU_HZ / (PPU_SCANLINES * PPU_DOTS - 0.5)


class Mirroring(str, Enum):
    HORIZONTAL = "horizontal"
    VERTICAL = "vertical"
    SINGLE_LOWER = "single-lower"
    SINGLE_UPPER = "single-upper"
    FOUR_SCREEN = "four-screen"


class Button(IntFlag):
    A = 1 << 0
    B = 1 << 1
    SELECT = 1 << 2
    START = 1 << 3
    UP = 1 << 4
    DOWN = 1 << 5
    LEFT = 1 << 6
    RIGHT = 1 << 7


class StatusFlag(IntFlag):
    CARRY = 1 << 0
    ZERO = 1 << 1
    INTERRUPT = 1 << 2
    DECIMAL = 1 << 3
    BREAK = 1 << 4
    UNUSED = 1 << 5
    OVERFLOW = 1 << 6
    NEGATIVE = 1 << 7


class AddressMode(IntEnum):
    IMPLIED = 0
    ACCUMULATOR = 1
    IMMEDIATE = 2
    ZERO_PAGE = 3
    ZERO_PAGE_X = 4
    ZERO_PAGE_Y = 5
    RELATIVE = 6
    ABSOLUTE = 7
    ABSOLUTE_X = 8
    ABSOLUTE_Y = 9
    INDIRECT = 10
    INDEXED_INDIRECT = 11
    INDIRECT_INDEXED = 12


# A commonly used measured NTSC 2C02 palette. The PPU emits six-bit color
# indices; composite-video interpretation is deliberately isolated here.
NES_PALETTE = (
    (84, 84, 84), (0, 30, 116), (8, 16, 144), (48, 0, 136),
    (68, 0, 100), (92, 0, 48), (84, 4, 0), (60, 24, 0),
    (32, 42, 0), (8, 58, 0), (0, 64, 0), (0, 60, 0),
    (0, 50, 60), (0, 0, 0), (0, 0, 0), (0, 0, 0),
    (152, 150, 152), (8, 76, 196), (48, 50, 236), (92, 30, 228),
    (136, 20, 176), (160, 20, 100), (152, 34, 32), (120, 60, 0),
    (84, 90, 0), (40, 114, 0), (8, 124, 0), (0, 118, 40),
    (0, 102, 120), (0, 0, 0), (0, 0, 0), (0, 0, 0),
    (236, 238, 236), (76, 154, 236), (120, 124, 236), (176, 98, 236),
    (228, 84, 236), (236, 88, 180), (236, 106, 100), (212, 136, 32),
    (160, 170, 0), (116, 196, 0), (76, 208, 32), (56, 204, 108),
    (56, 180, 204), (60, 60, 60), (0, 0, 0), (0, 0, 0),
    (236, 238, 236), (168, 204, 236), (188, 188, 236), (212, 178, 236),
    (236, 174, 236), (236, 174, 212), (236, 180, 176), (228, 196, 144),
    (204, 210, 120), (180, 222, 120), (168, 226, 144), (152, 226, 180),
    (160, 214, 228), (160, 162, 160), (0, 0, 0), (0, 0, 0),
)
