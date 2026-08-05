"""Ricoh 2A03 CPU: an NMOS 6502 core with decimal arithmetic disabled."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from .constants import AddressMode as M
from .constants import StatusFlag as F


class CPUBus(Protocol):
    @property
    def irq_pending(self) -> bool: ...
    def read(self, address: int) -> int: ...
    def write(self, address: int, value: int) -> None: ...
    def peek_code(self, address: int) -> int | None: ...


@dataclass(frozen=True, slots=True)
class Instruction:
    name: str
    mode: M
    cycles: int
    page_cycle: bool = False
    unofficial: bool = False


_MODE_NAMES = {
    "imp": M.IMPLIED,
    "acc": M.ACCUMULATOR,
    "imm": M.IMMEDIATE,
    "zp": M.ZERO_PAGE,
    "zpx": M.ZERO_PAGE_X,
    "zpy": M.ZERO_PAGE_Y,
    "rel": M.RELATIVE,
    "abs": M.ABSOLUTE,
    "abx": M.ABSOLUTE_X,
    "aby": M.ABSOLUTE_Y,
    "ind": M.INDIRECT,
    "izx": M.INDEXED_INDIRECT,
    "izy": M.INDIRECT_INDEXED,
}


def _instruction(spec: str) -> Instruction:
    name, mode, timing = spec.split(":")
    page_cycle = timing.endswith("+")
    cycles = int(timing.rstrip("+"))
    official = name in {
        "ADC", "AND", "ASL", "BCC", "BCS", "BEQ", "BIT", "BMI", "BNE",
        "BPL", "BRK", "BVC", "BVS", "CLC", "CLD", "CLI", "CLV", "CMP",
        "CPX", "CPY", "DEC", "DEX", "DEY", "EOR", "INC", "INX", "INY",
        "JMP", "JSR", "LDA", "LDX", "LDY", "LSR", "NOP", "ORA", "PHA",
        "PHP", "PLA", "PLP", "ROL", "ROR", "RTI", "RTS", "SBC", "SEC",
        "SED", "SEI", "STA", "STX", "STY", "TAX", "TAY", "TSX", "TXA",
        "TXS", "TYA",
    }
    # Only opcode $EA is the official NOP; the caller corrects the variants.
    return Instruction(name, _MODE_NAMES[mode], cycles, page_cycle, not official)


_ROWS = (
    ("BRK:imp:7", "ORA:izx:6", "KIL:imp:2", "SLO:izx:8", "NOP:zp:3", "ORA:zp:3", "ASL:zp:5", "SLO:zp:5", "PHP:imp:3", "ORA:imm:2", "ASL:acc:2", "ANC:imm:2", "NOP:abs:4", "ORA:abs:4", "ASL:abs:6", "SLO:abs:6"),
    ("BPL:rel:2", "ORA:izy:5+", "KIL:imp:2", "SLO:izy:8", "NOP:zpx:4", "ORA:zpx:4", "ASL:zpx:6", "SLO:zpx:6", "CLC:imp:2", "ORA:aby:4+", "NOP:imp:2", "SLO:aby:7", "NOP:abx:4+", "ORA:abx:4+", "ASL:abx:7", "SLO:abx:7"),
    ("JSR:abs:6", "AND:izx:6", "KIL:imp:2", "RLA:izx:8", "BIT:zp:3", "AND:zp:3", "ROL:zp:5", "RLA:zp:5", "PLP:imp:4", "AND:imm:2", "ROL:acc:2", "ANC:imm:2", "BIT:abs:4", "AND:abs:4", "ROL:abs:6", "RLA:abs:6"),
    ("BMI:rel:2", "AND:izy:5+", "KIL:imp:2", "RLA:izy:8", "NOP:zpx:4", "AND:zpx:4", "ROL:zpx:6", "RLA:zpx:6", "SEC:imp:2", "AND:aby:4+", "NOP:imp:2", "RLA:aby:7", "NOP:abx:4+", "AND:abx:4+", "ROL:abx:7", "RLA:abx:7"),
    ("RTI:imp:6", "EOR:izx:6", "KIL:imp:2", "SRE:izx:8", "NOP:zp:3", "EOR:zp:3", "LSR:zp:5", "SRE:zp:5", "PHA:imp:3", "EOR:imm:2", "LSR:acc:2", "ALR:imm:2", "JMP:abs:3", "EOR:abs:4", "LSR:abs:6", "SRE:abs:6"),
    ("BVC:rel:2", "EOR:izy:5+", "KIL:imp:2", "SRE:izy:8", "NOP:zpx:4", "EOR:zpx:4", "LSR:zpx:6", "SRE:zpx:6", "CLI:imp:2", "EOR:aby:4+", "NOP:imp:2", "SRE:aby:7", "NOP:abx:4+", "EOR:abx:4+", "LSR:abx:7", "SRE:abx:7"),
    ("RTS:imp:6", "ADC:izx:6", "KIL:imp:2", "RRA:izx:8", "NOP:zp:3", "ADC:zp:3", "ROR:zp:5", "RRA:zp:5", "PLA:imp:4", "ADC:imm:2", "ROR:acc:2", "ARR:imm:2", "JMP:ind:5", "ADC:abs:4", "ROR:abs:6", "RRA:abs:6"),
    ("BVS:rel:2", "ADC:izy:5+", "KIL:imp:2", "RRA:izy:8", "NOP:zpx:4", "ADC:zpx:4", "ROR:zpx:6", "RRA:zpx:6", "SEI:imp:2", "ADC:aby:4+", "NOP:imp:2", "RRA:aby:7", "NOP:abx:4+", "ADC:abx:4+", "ROR:abx:7", "RRA:abx:7"),
    ("NOP:imm:2", "STA:izx:6", "NOP:imm:2", "SAX:izx:6", "STY:zp:3", "STA:zp:3", "STX:zp:3", "SAX:zp:3", "DEY:imp:2", "NOP:imm:2", "TXA:imp:2", "XAA:imm:2", "STY:abs:4", "STA:abs:4", "STX:abs:4", "SAX:abs:4"),
    ("BCC:rel:2", "STA:izy:6", "KIL:imp:2", "AHX:izy:6", "STY:zpx:4", "STA:zpx:4", "STX:zpy:4", "SAX:zpy:4", "TYA:imp:2", "STA:aby:5", "TXS:imp:2", "TAS:aby:5", "SHY:abx:5", "STA:abx:5", "SHX:aby:5", "AHX:aby:5"),
    ("LDY:imm:2", "LDA:izx:6", "LDX:imm:2", "LAX:izx:6", "LDY:zp:3", "LDA:zp:3", "LDX:zp:3", "LAX:zp:3", "TAY:imp:2", "LDA:imm:2", "TAX:imp:2", "LAX:imm:2", "LDY:abs:4", "LDA:abs:4", "LDX:abs:4", "LAX:abs:4"),
    ("BCS:rel:2", "LDA:izy:5+", "KIL:imp:2", "LAX:izy:5+", "LDY:zpx:4", "LDA:zpx:4", "LDX:zpy:4", "LAX:zpy:4", "CLV:imp:2", "LDA:aby:4+", "TSX:imp:2", "LAS:aby:4+", "LDY:abx:4+", "LDA:abx:4+", "LDX:aby:4+", "LAX:aby:4+"),
    ("CPY:imm:2", "CMP:izx:6", "NOP:imm:2", "DCP:izx:8", "CPY:zp:3", "CMP:zp:3", "DEC:zp:5", "DCP:zp:5", "INY:imp:2", "CMP:imm:2", "DEX:imp:2", "AXS:imm:2", "CPY:abs:4", "CMP:abs:4", "DEC:abs:6", "DCP:abs:6"),
    ("BNE:rel:2", "CMP:izy:5+", "KIL:imp:2", "DCP:izy:8", "NOP:zpx:4", "CMP:zpx:4", "DEC:zpx:6", "DCP:zpx:6", "CLD:imp:2", "CMP:aby:4+", "NOP:imp:2", "DCP:aby:7", "NOP:abx:4+", "CMP:abx:4+", "DEC:abx:7", "DCP:abx:7"),
    ("CPX:imm:2", "SBC:izx:6", "NOP:imm:2", "ISC:izx:8", "CPX:zp:3", "SBC:zp:3", "INC:zp:5", "ISC:zp:5", "INX:imp:2", "SBC:imm:2", "NOP:imp:2", "SBC:imm:2", "CPX:abs:4", "SBC:abs:4", "INC:abs:6", "ISC:abs:6"),
    ("BEQ:rel:2", "SBC:izy:5+", "KIL:imp:2", "ISC:izy:8", "NOP:zpx:4", "SBC:zpx:4", "INC:zpx:6", "ISC:zpx:6", "SED:imp:2", "SBC:aby:4+", "NOP:imp:2", "ISC:aby:7", "NOP:abx:4+", "SBC:abx:4+", "INC:abx:7", "ISC:abx:7"),
)


def _build_opcodes() -> tuple[Instruction, ...]:
    result: list[Instruction] = []
    for row in _ROWS:
        result.extend(_instruction(spec) for spec in row)
    official_nops = {0xEA}
    unofficial_nops = {
        0x04, 0x0C, 0x14, 0x1A, 0x1C, 0x34, 0x3A, 0x3C, 0x44, 0x54,
        0x5A, 0x5C, 0x64, 0x74, 0x7A, 0x7C, 0x80, 0x82, 0x89, 0xC2,
        0xD4, 0xDA, 0xDC, 0xE2, 0xF4, 0xFA, 0xFC,
    }
    for opcode in unofficial_nops:
        item = result[opcode]
        result[opcode] = Instruction(
            item.name, item.mode, item.cycles, item.page_cycle, True
        )
    for opcode in official_nops:
        item = result[opcode]
        result[opcode] = Instruction(
            item.name, item.mode, item.cycles, item.page_cycle, False
        )
    # $EB performs SBC #immediate but is not one of the 151 documented
    # instruction encodings.
    item = result[0xEB]
    result[0xEB] = Instruction(
        item.name, item.mode, item.cycles, item.page_cycle, True
    )
    return tuple(result)


OPCODES = _build_opcodes()


# The bounded CPU-only executor sees the same immutable opcode stream many
# millions of times. Classify its hot instruction families once so the inner
# loop does not repeatedly fetch an Instruction dataclass and scan several
# literal tuples before reaching the operation it can execute locally.
_BATCH_JMP_ABSOLUTE = 1
_BATCH_NOP = 2
_BATCH_IMMEDIATE = 3
_BATCH_CARRY = 4
_BATCH_INDEX = 5
_BATCH_ZERO_PAGE = 6
_BATCH_BRANCH = 7
_BATCH_MISC = 8
_BATCH_MEMORY_MISC = 9

_BATCH_MEM_LDA = 1
_BATCH_MEM_LDX = 2
_BATCH_MEM_LDY = 3
_BATCH_MEM_STA = 4
_BATCH_MEM_STX = 5
_BATCH_MEM_STY = 6
_BATCH_MEM_AND = 7
_BATCH_MEM_EOR = 8
_BATCH_MEM_ORA = 9
_BATCH_MEM_ADC = 10
_BATCH_MEM_SBC = 11
_BATCH_MEM_CMP = 12
_BATCH_MEM_CPX = 13
_BATCH_MEM_CPY = 14
_BATCH_MEM_BIT = 15
_BATCH_MEM_ASL = 16
_BATCH_MEM_LSR = 17
_BATCH_MEM_ROL = 18
_BATCH_MEM_ROR = 19
_BATCH_MEM_INC = 20
_BATCH_MEM_DEC = 21


def _build_batch_opcode_kinds() -> bytes:
    kinds = bytearray(0x100)
    kinds[0x4C] = _BATCH_JMP_ABSOLUTE
    kinds[0xEA] = _BATCH_NOP
    for opcode in (
        0x09, 0x29, 0x49, 0x69, 0xA0, 0xA2, 0xA9, 0xC0, 0xC9,
        0xE0, 0xE9,
    ):
        kinds[opcode] = _BATCH_IMMEDIATE
    for opcode in (0x18, 0x38):
        kinds[opcode] = _BATCH_CARRY
    for opcode in (0x88, 0xC8, 0xCA, 0xE8):
        kinds[opcode] = _BATCH_INDEX
    for opcode in (0x85, 0x95, 0xA5, 0xB5):
        kinds[opcode] = _BATCH_ZERO_PAGE
    for opcode in (0x10, 0x30, 0x50, 0x70, 0x90, 0xB0, 0xD0, 0xF0):
        kinds[opcode] = _BATCH_BRANCH
    for opcode in (
        0x08, 0x0A, 0x20, 0x2A, 0x48, 0x4A, 0x60, 0x68, 0x6A,
        0x8A, 0x98, 0x9A, 0xA8, 0xAA, 0xBA,
    ):
        kinds[opcode] = _BATCH_MISC
    memory_names = {
        "LDA", "LDX", "LDY", "STA", "STX", "STY", "AND", "EOR",
        "ORA", "ADC", "SBC", "CMP", "CPX", "CPY", "BIT", "ASL",
        "LSR", "ROL", "ROR", "INC", "DEC",
    }
    memory_modes = {
        M.ZERO_PAGE,
        M.ZERO_PAGE_X,
        M.ZERO_PAGE_Y,
        M.ABSOLUTE,
        M.ABSOLUTE_X,
        M.ABSOLUTE_Y,
        M.INDEXED_INDIRECT,
        M.INDIRECT_INDEXED,
    }
    for opcode, instruction in enumerate(OPCODES):
        if (
            not kinds[opcode]
            and not instruction.unofficial
            and instruction.name in memory_names
            and instruction.mode in memory_modes
        ):
            kinds[opcode] = _BATCH_MEMORY_MISC
    return bytes(kinds)


def _build_batch_memory_operations() -> bytes:
    operation_ids = {
        "LDA": _BATCH_MEM_LDA,
        "LDX": _BATCH_MEM_LDX,
        "LDY": _BATCH_MEM_LDY,
        "STA": _BATCH_MEM_STA,
        "STX": _BATCH_MEM_STX,
        "STY": _BATCH_MEM_STY,
        "AND": _BATCH_MEM_AND,
        "EOR": _BATCH_MEM_EOR,
        "ORA": _BATCH_MEM_ORA,
        "ADC": _BATCH_MEM_ADC,
        "SBC": _BATCH_MEM_SBC,
        "CMP": _BATCH_MEM_CMP,
        "CPX": _BATCH_MEM_CPX,
        "CPY": _BATCH_MEM_CPY,
        "BIT": _BATCH_MEM_BIT,
        "ASL": _BATCH_MEM_ASL,
        "LSR": _BATCH_MEM_LSR,
        "ROL": _BATCH_MEM_ROL,
        "ROR": _BATCH_MEM_ROR,
        "INC": _BATCH_MEM_INC,
        "DEC": _BATCH_MEM_DEC,
    }
    return bytes(operation_ids.get(item.name, 0) for item in OPCODES)


_BATCH_OPCODE_KINDS = _build_batch_opcode_kinds()
_BATCH_MEMORY_OPERATIONS = _build_batch_memory_operations()
_BATCH_ADDRESS_MODES = bytes(int(item.mode) for item in OPCODES)
_BATCH_BASE_CYCLES = bytes(item.cycles for item in OPCODES)
_BATCH_PAGE_CYCLES = bytes(int(item.page_cycle) for item in OPCODES)
_BATCH_DUMMY_FETCHES = bytes(
    int(item.mode in (M.IMPLIED, M.ACCUMULATOR))
    for item in OPCODES
)
_DYNAMIC_INDIRECT_BATCH_OPCODES = bytes(
    int(
        item.mode in (M.INDEXED_INDIRECT, M.INDIRECT_INDEXED)
        and not item.unofficial
    )
    for item in OPCODES
)


def _run_batch_misc_instruction(
    opcode: int,
    a: int,
    x: int,
    y: int,
    p: int,
    sp: int,
    pc: int,
    open_bus: int,
    ram: bytearray,
    prg: bytes,
    prg_base: int,
    mask: int,
) -> tuple[int, int, int, int, int, int, int, int]:
    """Execute a less-common RAM/stack opcode without publishing CPU state.

    Keeping this helper outside the primary batch loop is deliberate: the
    compact LDA/STA/ALU path stays friendly to CPython's instruction cache,
    while call-heavy game engines avoid eight object writes and reads around
    every transfer, shift, stack operation, JSR, and RTS.
    """
    if opcode == 0x20:  # JSR absolute
        low = prg[(pc - prg_base) & mask]
        high = prg[(pc + 1 - prg_base) & mask]
        return_address = (pc + 1) & 0xFFFF
        ram[0x100 | sp] = (return_address >> 8) & 0xFF
        sp = (sp - 1) & 0xFF
        open_bus = return_address & 0xFF
        ram[0x100 | sp] = open_bus
        sp = (sp - 1) & 0xFF
        pc = low | (high << 8)
        return a, x, y, p, sp, pc, open_bus, 6
    if opcode == 0x60:  # RTS
        sp = (sp + 1) & 0xFF
        low = ram[0x100 | sp]
        sp = (sp + 1) & 0xFF
        high = ram[0x100 | sp]
        open_bus = high
        pc = ((low | (high << 8)) + 1) & 0xFFFF
        return a, x, y, p, sp, pc, open_bus, 6
    if opcode == 0x48:  # PHA
        ram[0x100 | sp] = a
        sp = (sp - 1) & 0xFF
        return a, x, y, p, sp, pc, a, 3
    if opcode == 0x08:  # PHP
        open_bus = p | 0x30
        ram[0x100 | sp] = open_bus
        sp = (sp - 1) & 0xFF
        return a, x, y, p, sp, pc, open_bus, 3
    if opcode == 0x68:  # PLA
        sp = (sp + 1) & 0xFF
        a = ram[0x100 | sp]
        p = (
            (p & 0x7D)
            | 0x20
            | (0x02 if a == 0 else 0)
            | (a & 0x80)
        )
        return a, x, y, p, sp, pc, a, 4

    if opcode == 0x8A:  # TXA
        a = x
        result = a
    elif opcode == 0x98:  # TYA
        a = y
        result = a
    elif opcode == 0x9A:  # TXS
        sp = x
        return a, x, y, p, sp, pc, open_bus, 2
    elif opcode == 0xA8:  # TAY
        y = a
        result = y
    elif opcode == 0xAA:  # TAX
        x = a
        result = x
    elif opcode == 0xBA:  # TSX
        x = sp
        result = x
    else:
        original = a
        if opcode == 0x0A:  # ASL A
            result = (original << 1) & 0xFF
            carry = original >> 7
        elif opcode == 0x2A:  # ROL A
            result = ((original << 1) | (p & 1)) & 0xFF
            carry = original >> 7
        elif opcode == 0x4A:  # LSR A
            result = original >> 1
            carry = original & 1
        else:  # ROR A
            result = ((p & 1) << 7) | (original >> 1)
            carry = original & 1
        a = result
        p = (
            (p & 0x7C)
            | 0x20
            | carry
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
        return a, x, y, p, sp, pc, open_bus, 2

    p = (
        (p & 0x7D)
        | 0x20
        | (0x02 if result == 0 else 0)
        | (result & 0x80)
    )
    return a, x, y, p, sp, pc, open_bus, 2


def _run_batch_memory_instruction(
    opcode: int,
    a: int,
    x: int,
    y: int,
    p: int,
    pc: int,
    ram: bytearray,
    prg: bytes,
    prg_base: int,
    mask: int,
    nrom_prg: bytes | None,
    nrom_mask: int,
    mapped_banks: tuple[bytes, ...] | None,
) -> tuple[int, int, int, int, int, int, int]:
    """Execute one preclassified RAM/immutable-PRG memory instruction.

    The admission table has already proved that the operand, indexed dummy
    read, and final access cannot touch a timed device or mapper register. The
    helper can therefore use mirrored RAM and immutable PRG directly while
    retaining cycle, flag, page-cross, and final open-bus state.
    """
    operation = _BATCH_MEMORY_OPERATIONS[opcode]
    mode = _BATCH_ADDRESS_MODES[opcode]
    operand_pc = pc
    operand = prg[(operand_pc - prg_base) & mask]
    crossed = False
    if mode == M.ZERO_PAGE:
        address = operand
        pc = (operand_pc + 1) & 0xFFFF
    elif mode == M.ZERO_PAGE_X:
        address = (operand + x) & 0xFF
        pc = (operand_pc + 1) & 0xFFFF
    elif mode == M.ZERO_PAGE_Y:
        address = (operand + y) & 0xFF
        pc = (operand_pc + 1) & 0xFFFF
    elif mode == M.INDEXED_INDIRECT:
        pointer = (operand + x) & 0xFF
        low = ram[pointer]
        high = ram[(pointer + 1) & 0xFF]
        address = low | (high << 8)
        pc = (operand_pc + 1) & 0xFFFF
    elif mode == M.INDIRECT_INDEXED:
        low = ram[operand]
        high = ram[(operand + 1) & 0xFF]
        base_address = low | (high << 8)
        address = (base_address + y) & 0xFFFF
        crossed = (base_address & 0xFF00) != (address & 0xFF00)
        pc = (operand_pc + 1) & 0xFFFF
    else:
        high = prg[(operand_pc + 1 - prg_base) & mask]
        base_address = operand | (high << 8)
        pc = (operand_pc + 2) & 0xFFFF
        if mode == M.ABSOLUTE_X:
            address = (base_address + x) & 0xFFFF
            crossed = (base_address & 0xFF00) != (address & 0xFF00)
        elif mode == M.ABSOLUTE_Y:
            address = (base_address + y) & 0xFFFF
            crossed = (base_address & 0xFF00) != (address & 0xFF00)
        else:
            address = base_address

    instruction_cycles = _BATCH_BASE_CYCLES[opcode] + int(
        crossed and _BATCH_PAGE_CYCLES[opcode]
    )
    if operation in (_BATCH_MEM_STA, _BATCH_MEM_STX, _BATCH_MEM_STY):
        if operation == _BATCH_MEM_STA:
            value = a
        elif operation == _BATCH_MEM_STX:
            value = x
        else:
            value = y
        ram[address & 0x07FF] = value
        return a, x, y, p, pc, value, instruction_cycles

    if address < 0x2000:
        value = ram[address & 0x07FF]
    elif nrom_prg is not None:
        value = nrom_prg[(address - 0x8000) & nrom_mask]
    elif mapped_banks is not None:
        mapped_offset = address - 0x8000
        value = mapped_banks[
            mapped_offset >> 13
        ][mapped_offset & 0x1FFF]
    else:
        value = prg[address - 0x8000]
    open_bus = value

    if operation == _BATCH_MEM_LDA:
        a = value
        p = (
            (p & 0x7D)
            | 0x20
            | (0x02 if value == 0 else 0)
            | (value & 0x80)
        )
    elif operation == _BATCH_MEM_LDX:
        x = value
        p = (
            (p & 0x7D)
            | 0x20
            | (0x02 if value == 0 else 0)
            | (value & 0x80)
        )
    elif operation == _BATCH_MEM_LDY:
        y = value
        p = (
            (p & 0x7D)
            | 0x20
            | (0x02 if value == 0 else 0)
            | (value & 0x80)
        )
    elif operation in (_BATCH_MEM_AND, _BATCH_MEM_EOR, _BATCH_MEM_ORA):
        if operation == _BATCH_MEM_AND:
            result = a & value
        elif operation == _BATCH_MEM_EOR:
            result = a ^ value
        else:
            result = a | value
        a = result
        p = (
            (p & 0x7D)
            | 0x20
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
    elif operation in (_BATCH_MEM_ADC, _BATCH_MEM_SBC):
        if operation == _BATCH_MEM_SBC:
            value ^= 0xFF
        accumulator = a
        total = accumulator + value + (p & 1)
        result = total & 0xFF
        p = (
            (p & 0x3C)
            | 0x20
            | int(total > 0xFF)
            | (0x02 if result == 0 else 0)
            | (
                0x40
                if (
                    ~(accumulator ^ value) & (accumulator ^ result)
                ) & 0x80
                else 0
            )
            | (result & 0x80)
        )
        a = result
    elif operation in (_BATCH_MEM_CMP, _BATCH_MEM_CPX, _BATCH_MEM_CPY):
        if operation == _BATCH_MEM_CMP:
            register = a
        elif operation == _BATCH_MEM_CPX:
            register = x
        else:
            register = y
        result = (register - value) & 0xFF
        p = (
            (p & 0x7C)
            | 0x20
            | int(register >= value)
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
    elif operation == _BATCH_MEM_BIT:
        p = (
            (p & 0x3D)
            | 0x20
            | (0x02 if (a & value) == 0 else 0)
            | (value & 0xC0)
        )
    else:
        original = value
        if operation == _BATCH_MEM_ASL:
            result = (original << 1) & 0xFF
            carry = original >> 7
        elif operation == _BATCH_MEM_LSR:
            result = original >> 1
            carry = original & 1
        elif operation == _BATCH_MEM_ROL:
            result = ((original << 1) | (p & 1)) & 0xFF
            carry = original >> 7
        elif operation == _BATCH_MEM_ROR:
            result = ((p & 1) << 7) | (original >> 1)
            carry = original & 1
        elif operation == _BATCH_MEM_INC:
            result = (original + 1) & 0xFF
            carry = p & 1
        else:
            result = (original - 1) & 0xFF
            carry = p & 1
        if operation >= _BATCH_MEM_INC:
            p = (
                (p & 0x7D)
                | 0x20
                | (0x02 if result == 0 else 0)
                | (result & 0x80)
            )
        else:
            p = (
                (p & 0x7C)
                | 0x20
                | carry
                | (0x02 if result == 0 else 0)
                | (result & 0x80)
            )
        ram[address & 0x07FF] = result
        open_bus = result

    return a, x, y, p, pc, open_bus, instruction_cycles


BATCH_BLOCK_HOT_THRESHOLD = 8
# Hot code from a larger banked title can legitimately span well over one
# thousand basic-block entries during a long session. Keep enough compiled
# blocks for a large banked title while retaining a deterministic host-only
# bound. Once full, preserve the established hot working set rather than
# clearing every function and creating a visible compile/re-warm hitch.
BATCH_BLOCK_CACHE_LIMIT = 4096
BATCH_BLOCK_MAX_INSTRUCTIONS = 32
# Runtime Python compilation is optional host work and has a much larger tail
# than executing one already-proven generic batch. Admit at most one new block
# per emulated frame so a newly entered level cannot compile a burst of helper
# fragments in the same 16.64 ms presentation interval.
BATCH_BLOCK_COMPILE_BUDGET_PER_FRAME = 1
# Audio and presentation have not started while the two-frame worker reservoir
# is being filled. Use exactly those hidden frames to establish part of the
# cartridge's reset working set. Extending the relaxed budget into frame three
# can empty that reservoir before presentation has accumulated any headroom.
BATCH_BLOCK_INITIAL_COMPILE_BUDGET = 4
BATCH_BLOCK_INITIAL_COMPILE_FRAMES = 2
# Once both sides are already hot, a fixed-bank helper call can remain inside
# the same device deadline. Cold/generic targets deliberately return through
# Console so entering new level code never combines translation and recursive
# slot setup into one long frame.
BATCH_HOT_SLOT_CHAIN_LIMIT = 8


def _compile_batch_block(
    prg: bytes,
    prg_base: int,
    mask: int,
    start_pc: int,
    starts: bytes,
    safe_index_base: int,
    batch_base: int,
    batch_end: int,
    *,
    segmented: bool,
) -> Callable[..., tuple[int, int, int, int, int, int, int, int, int, int]] | None:
    """Translate one hot immutable straight-line block to Python bytecode.

    This is deliberately a basic-block translator, not a native-code JIT.
    Source bytes select only fixed templates below and are embedded solely as
    integer constants.  Timed-device accesses, mapper writes, interrupts,
    indirect addresses, unofficial opcodes, and code outside the currently
    immutable slot never enter a translated block.

    A translated block keeps CPU registers in the caller's locals, removes
    the opcode/addressing dispatcher from each instruction, and returns at a
    branch, call, jump, stack return, unsafe boundary, or cycle deadline.
    The ordinary batch executor remains the fallback and the correctness
    oracle for every unhandled shape.
    """
    lines = [
        "def run(a, x, y, p, sp, open_bus, max_cycles, ram, banks):",
        "    cycles = 0",
        f"    pc = {start_pc}",
        "    last_opcode = 0",
        "    instruction_cycles = 0",
    ]
    current_pc = start_pc
    instruction_count = 0
    terminated = False
    indent = "    "

    def emit(statement: str) -> None:
        lines.append(f"{indent}{statement}")

    def emit_finish(*, control: bool = False) -> None:
        emit("cycles += instruction_cycles")
        if control:
            emit(
                "return (a, x, y, p, sp, pc, open_bus, cycles, "
                "last_opcode, instruction_cycles)"
            )
        else:
            emit("if cycles >= max_cycles:")
            lines.append(
                f"{indent}    return (a, x, y, p, sp, pc, open_bus, "
                "cycles, last_opcode, instruction_cycles)"
            )

    def emit_zn(register: str) -> None:
        emit(
            f"p = ((p & 0x7D) | 0x20 | "
            f"(0x02 if {register} == 0 else 0) | ({register} & 0x80))"
        )

    def emit_memory_read(address_region: str) -> None:
        if address_region == "ram":
            emit("value = ram[address & 0x07FF]")
        elif address_region == "dynamic":
            emit("if address < 0x2000:")
            lines.append(f"{indent}    value = ram[address & 0x07FF]")
            emit("else:")
            if segmented:
                lines.append(
                    f"{indent}    mapped_offset = address - 0x8000"
                )
                lines.append(
                    f"{indent}    value = banks[mapped_offset >> 13]"
                    "[mapped_offset & 0x1FFF]"
                )
            else:
                lines.append(
                    f"{indent}    value = ROM[(address - 0x8000) & {mask}]"
                )
        elif segmented:
            emit("mapped_offset = address - 0x8000")
            emit(
                "value = banks[mapped_offset >> 13]"
                "[mapped_offset & 0x1FFF]"
            )
        else:
            emit(f"value = ROM[(address - 0x8000) & {mask}]")

    def emit_memory_operation(operation: int, address_region: str) -> None:
        if operation in (_BATCH_MEM_STA, _BATCH_MEM_STX, _BATCH_MEM_STY):
            if operation == _BATCH_MEM_STA:
                emit("value = a")
            elif operation == _BATCH_MEM_STX:
                emit("value = x")
            else:
                emit("value = y")
            emit("ram[address & 0x07FF] = value")
            emit("open_bus = value")
            return

        emit_memory_read(address_region)
        emit("open_bus = value")
        if operation == _BATCH_MEM_LDA:
            emit("a = value")
            emit_zn("a")
        elif operation == _BATCH_MEM_LDX:
            emit("x = value")
            emit_zn("x")
        elif operation == _BATCH_MEM_LDY:
            emit("y = value")
            emit_zn("y")
        elif operation in (_BATCH_MEM_AND, _BATCH_MEM_EOR, _BATCH_MEM_ORA):
            if operation == _BATCH_MEM_AND:
                emit("a &= value")
            elif operation == _BATCH_MEM_EOR:
                emit("a ^= value")
            else:
                emit("a |= value")
            emit_zn("a")
        elif operation in (_BATCH_MEM_ADC, _BATCH_MEM_SBC):
            if operation == _BATCH_MEM_SBC:
                emit("value ^= 0xFF")
            emit("accumulator = a")
            emit("total = accumulator + value + (p & 1)")
            emit("a = total & 0xFF")
            emit(
                "p = ((p & 0x3C) | 0x20 | int(total > 0xFF) | "
                "(0x02 if a == 0 else 0) | "
                "(0x40 if ((~(accumulator ^ value) & "
                "(accumulator ^ a)) & 0x80) else 0) | (a & 0x80))"
            )
        elif operation in (_BATCH_MEM_CMP, _BATCH_MEM_CPX, _BATCH_MEM_CPY):
            register = (
                "a"
                if operation == _BATCH_MEM_CMP
                else "x" if operation == _BATCH_MEM_CPX else "y"
            )
            emit(f"result = ({register} - value) & 0xFF")
            emit(
                f"p = ((p & 0x7C) | 0x20 | int({register} >= value) | "
                "(0x02 if result == 0 else 0) | (result & 0x80))"
            )
        elif operation == _BATCH_MEM_BIT:
            emit(
                "p = ((p & 0x3D) | 0x20 | "
                "(0x02 if (a & value) == 0 else 0) | (value & 0xC0))"
            )
        else:
            emit("original = value")
            if operation == _BATCH_MEM_ASL:
                emit("result = (original << 1) & 0xFF")
                emit("carry = original >> 7")
            elif operation == _BATCH_MEM_LSR:
                emit("result = original >> 1")
                emit("carry = original & 1")
            elif operation == _BATCH_MEM_ROL:
                emit("result = ((original << 1) | (p & 1)) & 0xFF")
                emit("carry = original >> 7")
            elif operation == _BATCH_MEM_ROR:
                emit("result = ((p & 1) << 7) | (original >> 1)")
                emit("carry = original & 1")
            elif operation == _BATCH_MEM_INC:
                emit("result = (original + 1) & 0xFF")
            else:
                emit("result = (original - 1) & 0xFF")
            if operation in (_BATCH_MEM_INC, _BATCH_MEM_DEC):
                emit_zn("result")
            else:
                emit(
                    "p = ((p & 0x7C) | 0x20 | carry | "
                    "(0x02 if result == 0 else 0) | (result & 0x80))"
                )
            emit("ram[address & 0x07FF] = result")
            emit("open_bus = result")

    for _ in range(BATCH_BLOCK_MAX_INSTRUCTIONS):
        if not batch_base <= current_pc < batch_end:
            break
        safe_offset = current_pc - safe_index_base
        if not 0 <= safe_offset < len(starts):
            break
        opcode = prg[(current_pc - prg_base) & mask]
        instruction = OPCODES[opcode]
        mode = instruction.mode
        dynamic_indirect = bool(_DYNAMIC_INDIRECT_BATCH_OPCODES[opcode])
        if not starts[safe_offset] and not dynamic_indirect:
            break
        kind = _BATCH_OPCODE_KINDS[opcode]
        if instruction.unofficial or mode == M.INDIRECT:
            break

        if mode in (M.IMPLIED, M.ACCUMULATOR):
            length = 1
        elif mode in (
            M.IMMEDIATE,
            M.ZERO_PAGE,
            M.ZERO_PAGE_X,
            M.ZERO_PAGE_Y,
            M.RELATIVE,
            M.INDEXED_INDIRECT,
            M.INDIRECT_INDEXED,
        ):
            length = 2
        else:
            length = 3
        next_pc = (current_pc + length) & 0xFFFF
        operand = (
            prg[(current_pc + 1 - prg_base) & mask]
            if length >= 2
            else 0
        )
        high = (
            prg[(current_pc + 2 - prg_base) & mask]
            if length == 3
            else 0
        )

        supported = bool(kind) or opcode in (0xB8, 0xD8, 0xF8)
        if not supported:
            break
        if dynamic_indirect:
            # Resolve and guard the pointer before publishing the opcode or
            # final bus value. A preceding instruction in this same generated
            # block may have changed the zero-page pointer from RAM/ROM into
            # timed device space; in that case return at the prior completed
            # instruction exactly as the generic safe-batch loop does.
            operation = _BATCH_MEMORY_OPERATIONS[opcode]
            if mode == M.INDEXED_INDIRECT:
                emit(f"pointer = ({operand} + x) & 0xFF")
                emit("low = ram[pointer]")
                emit("high = ram[(pointer + 1) & 0xFF]")
                emit("address = low | (high << 8)")
                if operation in (
                    _BATCH_MEM_STA,
                    _BATCH_MEM_STX,
                    _BATCH_MEM_STY,
                ):
                    safe_condition = "address < 0x2000"
                else:
                    safe_condition = (
                        "address < 0x2000 or address >= 0x8000"
                    )
            else:
                emit(f"low = ram[{operand}]")
                emit(f"high = ram[{(operand + 1) & 0xFF}]")
                emit("base_address = low | (high << 8)")
                emit("address = (base_address + y) & 0xFFFF")
                emit(
                    "dummy_address = (base_address & 0xFF00) "
                    "| (address & 0x00FF)"
                )
                emit(
                    "crossed = (base_address & 0xFF00) "
                    "!= (address & 0xFF00)"
                )
                if operation in (
                    _BATCH_MEM_STA,
                    _BATCH_MEM_STX,
                    _BATCH_MEM_STY,
                ):
                    safe_condition = (
                        "address < 0x2000 and dummy_address < 0x2000"
                    )
                else:
                    safe_condition = (
                        "(address < 0x2000 and dummy_address < 0x2000) "
                        "or (address >= 0x8000 and dummy_address >= 0x8000)"
                    )
            emit(f"if not ({safe_condition}):")
            lines.append(
                f"{indent}    return (a, x, y, p, sp, pc, open_bus, "
                "cycles, last_opcode, instruction_cycles)"
            )
        instruction_count += 1
        emit(f"open_bus = {opcode}")
        if mode in (M.IMPLIED, M.ACCUMULATOR):
            dummy = prg[(current_pc + 1 - prg_base) & mask]
            emit(f"open_bus = {dummy}")
        emit(f"last_opcode = {opcode}")

        if kind == _BATCH_JMP_ABSOLUTE:
            target = operand | (high << 8)
            emit(f"open_bus = {high}")
            emit(f"pc = {target}")
            emit("instruction_cycles = 3")
            emit_finish(control=True)
            terminated = True
        elif kind == _BATCH_NOP:
            emit(f"pc = {next_pc}")
            emit("instruction_cycles = 2")
            emit_finish()
        elif kind == _BATCH_IMMEDIATE:
            emit(f"value = {operand}")
            emit("open_bus = value")
            if opcode == 0xA9:
                emit("a = value")
                emit_zn("a")
            elif opcode == 0xA2:
                emit("x = value")
                emit_zn("x")
            elif opcode == 0xA0:
                emit("y = value")
                emit_zn("y")
            elif opcode in (0xC0, 0xC9, 0xE0):
                register = "y" if opcode == 0xC0 else "x" if opcode == 0xE0 else "a"
                emit(f"result = ({register} - value) & 0xFF")
                emit(
                    f"p = ((p & 0x7C) | 0x20 | int({register} >= value) | "
                    "(0x02 if result == 0 else 0) | (result & 0x80))"
                )
            elif opcode in (0x69, 0xE9):
                if opcode == 0xE9:
                    emit("value ^= 0xFF")
                emit("accumulator = a")
                emit("total = accumulator + value + (p & 1)")
                emit("a = total & 0xFF")
                emit(
                    "p = ((p & 0x3C) | 0x20 | int(total > 0xFF) | "
                    "(0x02 if a == 0 else 0) | "
                    "(0x40 if ((~(accumulator ^ value) & "
                    "(accumulator ^ a)) & 0x80) else 0) | (a & 0x80))"
                )
            else:
                if opcode == 0x29:
                    emit("a &= value")
                elif opcode == 0x49:
                    emit("a ^= value")
                else:
                    emit("a |= value")
                emit_zn("a")
            emit(f"pc = {next_pc}")
            emit("instruction_cycles = 2")
            emit_finish()
        elif kind == _BATCH_CARRY:
            if opcode == 0x18:
                emit("p = (p & 0xFE) | 0x20")
            else:
                emit("p |= 0x21")
            emit(f"pc = {next_pc}")
            emit("instruction_cycles = 2")
            emit_finish()
        elif kind == _BATCH_INDEX:
            if opcode == 0x88:
                emit("y = (y - 1) & 0xFF")
                register = "y"
            elif opcode == 0xC8:
                emit("y = (y + 1) & 0xFF")
                register = "y"
            elif opcode == 0xCA:
                emit("x = (x - 1) & 0xFF")
                register = "x"
            else:
                emit("x = (x + 1) & 0xFF")
                register = "x"
            emit_zn(register)
            emit(f"pc = {next_pc}")
            emit("instruction_cycles = 2")
            emit_finish()
        elif kind == _BATCH_ZERO_PAGE:
            emit(f"address = {operand}")
            if opcode in (0x95, 0xB5):
                emit("address = (address + x) & 0xFF")
            if opcode in (0x85, 0x95):
                emit("value = a")
                emit("ram[address] = value")
            else:
                emit("value = ram[address]")
                emit("a = value")
                emit_zn("a")
            emit("open_bus = value")
            emit(f"pc = {next_pc}")
            cycles = 3 if opcode in (0x85, 0xA5) else 4
            emit(f"instruction_cycles = {cycles}")
            emit_finish()
        elif kind == _BATCH_BRANCH:
            condition = {
                0x10: "not (p & 0x80)",
                0x30: "bool(p & 0x80)",
                0x50: "not (p & 0x40)",
                0x70: "bool(p & 0x40)",
                0x90: "not (p & 0x01)",
                0xB0: "bool(p & 0x01)",
                0xD0: "not (p & 0x02)",
                0xF0: "bool(p & 0x02)",
            }[opcode]
            signed = operand - 0x100 if operand & 0x80 else operand
            target = (next_pc + signed) & 0xFFFF
            emit(f"open_bus = {operand}")
            taken_cycles = 3 + int(
                (next_pc & 0xFF00) != (target & 0xFF00)
            )
            if target == start_pc:
                # A hot counted/array loop can remain inside one generated
                # Python frame.  The deadline check still occurs after every
                # 6502 instruction, and the not-taken iteration returns at
                # the exact fall-through PC and cycle count.
                lines[2:] = [f"    {line}" for line in lines[2:]]
                lines.insert(2, "    while True:")
                indent = "        "
            emit(f"if {condition}:")
            lines.append(f"{indent}    pc = {target}")
            lines.append(
                f"{indent}    instruction_cycles = {taken_cycles}"
            )
            emit("else:")
            lines.append(f"{indent}    pc = {next_pc}")
            lines.append(f"{indent}    instruction_cycles = 2")
            if target == start_pc:
                emit("cycles += instruction_cycles")
                emit(f"if cycles >= max_cycles or pc != {start_pc}:")
                lines.append(
                    f"{indent}    return (a, x, y, p, sp, pc, "
                    "open_bus, cycles, last_opcode, instruction_cycles)"
                )
            else:
                emit_finish(control=True)
            terminated = True
        elif kind == _BATCH_MEMORY_MISC:
            operation = _BATCH_MEMORY_OPERATIONS[opcode]
            if mode == M.ZERO_PAGE:
                emit(f"address = {operand}")
                region = "ram"
            elif mode == M.ZERO_PAGE_X:
                emit(f"address = ({operand} + x) & 0xFF")
                region = "ram"
            elif mode == M.ZERO_PAGE_Y:
                emit(f"address = ({operand} + y) & 0xFF")
                region = "ram"
            elif mode in (M.INDEXED_INDIRECT, M.INDIRECT_INDEXED):
                region = "dynamic"
            else:
                base_address = operand | (high << 8)
                if mode == M.ABSOLUTE_X:
                    emit(f"address = ({base_address} + x) & 0xFFFF")
                    emit(
                        f"crossed = ({base_address} & 0xFF00) != "
                        "(address & 0xFF00)"
                    )
                elif mode == M.ABSOLUTE_Y:
                    emit(f"address = ({base_address} + y) & 0xFFFF")
                    emit(
                        f"crossed = ({base_address} & 0xFF00) != "
                        "(address & 0xFF00)"
                    )
                else:
                    emit(f"address = {base_address}")
                region = "ram" if base_address < 0x2000 else "rom"
            emit_memory_operation(operation, region)
            emit(f"pc = {next_pc}")
            if (
                mode in (M.ABSOLUTE_X, M.ABSOLUTE_Y, M.INDIRECT_INDEXED)
                and instruction.page_cycle
            ):
                emit(
                    f"instruction_cycles = {instruction.cycles} + int(crossed)"
                )
            else:
                emit(f"instruction_cycles = {instruction.cycles}")
            emit_finish()
        elif kind == _BATCH_MISC:
            if opcode == 0x20:  # JSR absolute
                target = operand | (high << 8)
                return_address = (current_pc + 2) & 0xFFFF
                emit(f"ram[0x100 | sp] = {(return_address >> 8) & 0xFF}")
                emit("sp = (sp - 1) & 0xFF")
                emit(f"open_bus = {return_address & 0xFF}")
                emit("ram[0x100 | sp] = open_bus")
                emit("sp = (sp - 1) & 0xFF")
                emit(f"pc = {target}")
                emit("instruction_cycles = 6")
                emit_finish(control=True)
                terminated = True
            elif opcode == 0x60:  # RTS
                emit("sp = (sp + 1) & 0xFF")
                emit("low = ram[0x100 | sp]")
                emit("sp = (sp + 1) & 0xFF")
                emit("high = ram[0x100 | sp]")
                emit("open_bus = high")
                emit("pc = ((low | (high << 8)) + 1) & 0xFFFF")
                emit("instruction_cycles = 6")
                emit_finish(control=True)
                terminated = True
            elif opcode == 0x48:  # PHA
                emit("ram[0x100 | sp] = a")
                emit("sp = (sp - 1) & 0xFF")
                emit("open_bus = a")
                emit(f"pc = {next_pc}")
                emit("instruction_cycles = 3")
                emit_finish()
            elif opcode == 0x08:  # PHP
                emit("open_bus = p | 0x30")
                emit("ram[0x100 | sp] = open_bus")
                emit("sp = (sp - 1) & 0xFF")
                emit(f"pc = {next_pc}")
                emit("instruction_cycles = 3")
                emit_finish()
            elif opcode == 0x68:  # PLA
                emit("sp = (sp + 1) & 0xFF")
                emit("a = ram[0x100 | sp]")
                emit_zn("a")
                emit("open_bus = a")
                emit(f"pc = {next_pc}")
                emit("instruction_cycles = 4")
                emit_finish()
            elif opcode in (0x8A, 0x98, 0x9A, 0xA8, 0xAA, 0xBA):
                if opcode == 0x8A:
                    emit("a = x")
                    register = "a"
                elif opcode == 0x98:
                    emit("a = y")
                    register = "a"
                elif opcode == 0x9A:
                    emit("sp = x")
                    register = ""
                elif opcode == 0xA8:
                    emit("y = a")
                    register = "y"
                elif opcode == 0xAA:
                    emit("x = a")
                    register = "x"
                else:
                    emit("x = sp")
                    register = "x"
                if register:
                    emit_zn(register)
                emit(f"pc = {next_pc}")
                emit("instruction_cycles = 2")
                emit_finish()
            else:
                emit("original = a")
                if opcode == 0x0A:
                    emit("a = (original << 1) & 0xFF")
                    emit("carry = original >> 7")
                elif opcode == 0x2A:
                    emit("a = ((original << 1) | (p & 1)) & 0xFF")
                    emit("carry = original >> 7")
                elif opcode == 0x4A:
                    emit("a = original >> 1")
                    emit("carry = original & 1")
                else:
                    emit("a = ((p & 1) << 7) | (original >> 1)")
                    emit("carry = original & 1")
                emit(
                    "p = ((p & 0x7C) | 0x20 | carry | "
                    "(0x02 if a == 0 else 0) | (a & 0x80))"
                )
                emit(f"pc = {next_pc}")
                emit("instruction_cycles = 2")
                emit_finish()
        else:
            # CLV, CLD, and SED are safe implied flag operations that are
            # intentionally outside the compact common-kind table.
            if opcode == 0xB8:
                emit("p = (p & 0xBF) | 0x20")
            elif opcode == 0xD8:
                emit("p = (p & 0xF7) | 0x20")
            else:
                emit("p |= 0x28")
            emit(f"pc = {next_pc}")
            emit("instruction_cycles = 2")
            emit_finish()

        current_pc = next_pc
        if terminated:
            break

    # Retain short straight-line fragments as bridges too. Their direct call
    # is not materially faster in isolation, but the caller immediately
    # reconnects their fall-through PC to the next translated block. A device
    # deadline that lands one instruction before a hot loop therefore no
    # longer forces the remainder of the span through opcode dispatch.
    if instruction_count < 1:
        return None
    if not terminated:
        emit(
            "return (a, x, y, p, sp, pc, open_bus, cycles, "
            "last_opcode, instruction_cycles)"
        )
    namespace: dict[str, object] = {"ROM": prg}
    source = "\n".join(lines)
    try:
        exec(compile(source, "<cartaconda-batch-block>", "exec"), namespace)
    except (SyntaxError, ValueError):
        return None
    return namespace["run"]  # type: ignore[return-value]

# Punch-Out's sampled bell/speech player keeps rendering disabled and drives
# the $4011 DAC directly from a cycle-counted 6502 loop.  These three short
# signatures identify the loop by code, not by ROM title/hash, so unrelated
# code that happens to execute at the same address can never enter its
# specialized executor.
_CPU_DRIVEN_PCM_HEAD = (
    b"\xAD\xC2\x07\x29\x03\xAC\xC4\x07\xC0\x40\xB0\x02\x90\x02"
    b"\x09\x04\x0D\xC6\x07\xA8\xB9\x1A\x91\x10\x39\x18\x6D\xC4"
    b"\x07\x90\x02\xB0\x02\xA9\x00\x8D\xC4\x07\x4A\x8D\x11\x40"
)
_CPU_DRIVEN_PCM_DELAY3 = (
    b"\xEE\xFF\x07\xEE\xFF\x07\xEE\xFF\x07\x4C\x57\x90"
)
_CPU_DRIVEN_PCM_REFILL = (
    b"\xE6\xFE\xD0\xF0\xE6\xFF\xA5\xFF\xC9\x9D\xD0\x08\xA9\x92"
    b"\x85\xFF\xA9\x00\x85\xFE\xEE\xE0\x07\xAD\xE0\x07\xAC\xE1"
    b"\x07\xD9\xC2\x90\xD0\x03\xEE\xE1\x07\xAC\xE2\x07\xD9\xF6"
    b"\x90\xD0\x06\xEE\xE2\x07\x20\xFF\x90\xA0\x00\xB1\xFE\x8D"
    b"\xC2\x07\xA9\x04\x8D\xC0\x07"
)
_CPU_DRIVEN_PCM_TAIL = (
    b"\x4E\xC2\x07\x4E\xC2\x07\xCE\xC0\x07\xF0\x8C\xAC\xE1\x07"
    b"\xB9\xDB\x90\xF0\x21\x8D\xC6\x07\xEE\xFF\x07\xEE\xFF\x07"
    b"\xEE\xFF\x07\xEE\xFF\x07\xEE\xFF\x07\x4C\x63\x90"
)
_CPU_DRIVEN_PCM_POSITIVE = (
    b"\x18\x6D\xC4\x07\xB0\x02\x90\xC9\xA9\xFF\xD0\xC5\x60"
)


def _mapped_prg_has_cpu_driven_pcm(prg: bytes) -> bool:
    """Return whether a 32 KiB CPU map contains the recognized DAC loop."""
    return bool(
        len(prg) == 0x8000
        and prg[0x1063:0x108D] == _CPU_DRIVEN_PCM_HEAD
        and prg[0x1018:0x1024] == _CPU_DRIVEN_PCM_DELAY3
        and prg[0x1024:0x1063] == _CPU_DRIVEN_PCM_REFILL
        and prg[0x108D:0x10B5] == _CPU_DRIVEN_PCM_TAIL
        and prg[0x10B5:0x10C2] == _CPU_DRIVEN_PCM_POSITIVE
    )


_MEMORY_WRITE_OPERATIONS = frozenset(
    {
        "AHX",
        "ASL",
        "DCP",
        "DEC",
        "INC",
        "ISC",
        "LSR",
        "RLA",
        "ROL",
        "ROR",
        "RRA",
        "SAX",
        "SHX",
        "SHY",
        "SLO",
        "SRE",
        "STA",
        "STX",
        "STY",
        "TAS",
    }
)
SEGMENTED_BATCH_MAP_LIMIT = 64
# MMC3 tables are keyed by one immutable 8 KiB bank at the CPU slot where it
# actually executes, rather than by the complete transient 32 KiB mapping.
# Consequently a data-bank write cannot request compilation and an executed
# bank costs only about three milliseconds to classify on the development
# baseline. Compile it on first use: waiting for dozens of scheduler passes
# left short-lived level and audio engines on the literal CPU path forever.
SEGMENTED_BATCH_STABILITY_OBSERVATIONS = 1
_COOPERATIVE_POLL_KIND = 2


def _mark_cooperative_poll_loops(
    prg: bytes,
    base: int,
    poll: bytearray,
    safe: bytearray | None = None,
) -> None:
    """Recognize a four-slot cooperative scheduler's idle scan.

    A number of later games multiplex small software subsystems by scanning
    four zero-page status records.  When every status is below a constant,
    the immutable loop can only repeat until the next hardware interrupt::

        LDX #0 / STX reset / LDY #4
        LDA status,X / CMP #limit / BCS work
        INX / INX / INX / INX / DEY / BNE scan
        JMP setup

    The classifier keys the optimization to the complete control-flow shape,
    including both backward targets.  It therefore remains independent of a
    title, ROM hash, address, and the particular zero-page fields involved.
    """
    # Six setup bytes precede the scan, and the scan itself is sixteen bytes.
    for offset in range(6, len(prg) - 15):
        setup = offset - 6
        if (
            prg[setup] != 0xA2       # LDX #$00
            or prg[setup + 1] != 0x00
            or prg[setup + 2] != 0x86  # STX zp
            or prg[setup + 4] != 0xA0  # LDY #$04
            or prg[setup + 5] != 0x04
            or prg[offset] != 0xB5     # LDA zp,X
            or prg[offset + 2] != 0xC9  # CMP #limit
            or prg[offset + 4] != 0xB0  # BCS work
            or prg[offset + 6:offset + 10] != b"\xE8" * 4
            or prg[offset + 10] != 0x88  # DEY
            or prg[offset + 11] != 0xD0  # BNE scan
            or prg[offset + 13] != 0x4C  # JMP setup
        ):
            continue
        status_base = prg[offset + 1]
        reset_address = prg[setup + 3]
        if reset_address in {
            status_base,
            (status_base + 4) & 0xFF,
            (status_base + 8) & 0xFF,
            (status_base + 12) & 0xFF,
        }:
            # STX in the setup would change a value observed by a later scan,
            # so repeated scans would not be invariant from this CPU state.
            continue
        branch_relative = prg[offset + 12]
        if branch_relative & 0x80:
            branch_relative -= 0x100
        scan_address = base + offset
        if scan_address + 13 + branch_relative != scan_address:
            continue
        jump_target = prg[offset + 14] | (prg[offset + 15] << 8)
        if jump_target != base + setup:
            continue
        poll[offset if len(poll) == len(prg) else scan_address] = (
            _COOPERATIVE_POLL_KIND
        )
        if safe is not None:
            safe[offset if len(safe) == len(prg) else scan_address] = 0


@lru_cache(maxsize=64)
def _build_nrom_safe_batch_starts(prg: bytes) -> bytes:
    """Preclassify mapped PRG instructions that cannot touch a timed device.

    This is intentionally conservative.  The table permits only code fetches,
    stack/CPU-RAM accesses, and immutable mapped PRG reads. Therefore several
    CPU instructions can execute before the Console scheduler is re-entered,
    provided its existing PPU/APU/DMC deadline bounds the total cycle span.
    """
    if len(prg) not in (0x4000, 0x8000):
        return bytes(0x10000)
    mask = len(prg) - 1
    result = bytearray(0x10000)

    def rom_byte(address: int) -> int:
        return prg[(address - 0x8000) & mask]

    def static_access_is_safe(address: int, writes: bool) -> bool:
        if address < 0x2000:
            return True
        return address >= 0x8000 and not writes

    def indexed_access_is_safe(base: int, writes: bool) -> bool:
        # The full 8-bit index range stays inside mirrored CPU RAM.
        if base <= 0x1F00:
            return True
        # Immutable NROM reads are safe only when the index cannot enter the
        # expansion/device window or wrap through $0000.
        return not writes and 0x8000 <= base <= 0xFF00

    single_byte_modes = {M.IMPLIED, M.ACCUMULATOR}
    for pc in range(0x8000, 0x10000):
        opcode = rom_byte(pc)
        instruction = OPCODES[opcode]
        mode = instruction.mode
        if pc == 0xFFFF:
            # A one-byte opcode still fetches its following byte, which wraps
            # into CPU RAM and therefore cannot be part of a ROM-only batch.
            continue
        if mode in single_byte_modes:
            result[pc] = int(
                instruction.name
                not in {"BRK", "KIL", "CLI", "PLP", "RTI", "SEI"}
            )
            continue
        if mode in {
            M.IMMEDIATE,
            M.ZERO_PAGE,
            M.ZERO_PAGE_X,
            M.ZERO_PAGE_Y,
            M.RELATIVE,
        }:
            result[pc] = 1
            continue
        if mode in {M.INDEXED_INDIRECT, M.INDIRECT_INDEXED}:
            if not instruction.unofficial:
                # Runtime pointer resolution is required, but the opcode can
                # be rejected cheaply before entering that slower check.
                result[pc] = 2
            continue
        if pc == 0xFFFE:
            continue

        low = rom_byte(pc + 1)
        high = rom_byte(pc + 2)
        address = low | (high << 8)
        writes = instruction.name in _MEMORY_WRITE_OPERATIONS
        if mode == M.ABSOLUTE:
            if instruction.name in {"JMP", "JSR"}:
                # Keep the specialized O(1) self-jump batch reachable.
                result[pc] = int(
                    instruction.name == "JSR" or address != pc
                )
            else:
                result[pc] = int(
                    static_access_is_safe(address, writes)
                )
        elif mode in {M.ABSOLUTE_X, M.ABSOLUTE_Y}:
            result[pc] = int(indexed_access_is_safe(address, writes))
        elif mode == M.INDIRECT:
            # Only JMP uses this mode. Its NMOS page-wrap read remains wholly
            # in RAM or wholly in NROM for these two ranges.
            result[pc] = int(address < 0x2000 or address >= 0x8000)

    return bytes(result)


@lru_cache(maxsize=64)
def _build_mapped_loop_tables(prg: bytes) -> tuple[bytes, bytes]:
    """Recognize safe counter and RAM-poll loops in one 32 KiB PRG map."""
    if len(prg) != 0x8000:
        return bytes(0x10000), bytes(0x10000)

    counter_loop_opcodes = bytearray(0x10000)
    for opcode in (0x88, 0xC8, 0xCA, 0xE8):
        pattern = bytes((opcode, 0xD0, 0xFD))
        offset = prg.find(pattern)
        while offset >= 0:
            counter_loop_opcodes[0x8000 + offset] = opcode
            offset = prg.find(pattern, offset + 1)

    poll_loop_starts = bytearray(0x10000)
    read_lengths = {
        0xA4: 2, 0xA5: 2, 0xA6: 2, 0x24: 2,
        0xC4: 2, 0xC5: 2, 0xE4: 2,
        0xAC: 3, 0xAD: 3, 0xAE: 3, 0x2C: 3,
        0xCC: 3, 0xCD: 3, 0xEC: 3,
    }
    branch_opcodes = {
        0x10, 0x30, 0x50, 0x70,
        0x90, 0xB0, 0xD0, 0xF0,
    }

    def rom_byte(address: int) -> int:
        return prg[address - 0x8000]

    for address in range(0x8000, 0xFFFC):
        instruction_length = read_lengths.get(rom_byte(address))
        if instruction_length is None:
            continue
        if instruction_length == 3:
            read_address = (
                rom_byte(address + 1)
                | (rom_byte(address + 2) << 8)
            )
            if read_address >= 0x2000:
                continue
        branch_address = address + instruction_length
        if rom_byte(branch_address) not in branch_opcodes:
            continue
        relative = rom_byte(branch_address + 1)
        if relative & 0x80:
            relative -= 0x100
        branch_base = (branch_address + 2) & 0xFFFF
        if (branch_base + relative) & 0xFFFF == address:
            poll_loop_starts[address] = 1

    _mark_cooperative_poll_loops(prg, 0x8000, poll_loop_starts)

    return bytes(counter_loop_opcodes), bytes(poll_loop_starts)


@lru_cache(maxsize=512)
def _build_segment_fast_tables(
    prg: bytes,
    base: int,
) -> tuple[bytes, bytes, bytes]:
    """Preclassify one immutable 8 KiB bank at one MMC3 CPU slot.

    Instructions whose operands cross a slot boundary stay on the literal
    path. That tiny conservative edge lets a newly selected PRG bank reuse
    work for every other slot and avoids rescanning all 32 KiB during a scene
    transition.
    """
    if len(prg) != 0x2000 or base not in (0x8000, 0xA000, 0xC000, 0xE000):
        empty = bytes(0x2000)
        return empty, empty, empty
    safe = bytearray(0x2000)

    def static_access_is_safe(address: int, writes: bool) -> bool:
        return address < 0x2000 or (address >= 0x8000 and not writes)

    def indexed_access_is_safe(address: int, writes: bool) -> bool:
        if address <= 0x1F00:
            return True
        return not writes and 0x8000 <= address <= 0xFF00

    single_byte_modes = {M.IMPLIED, M.ACCUMULATOR}
    for offset, opcode in enumerate(prg):
        pc = base + offset
        instruction = OPCODES[opcode]
        mode = instruction.mode
        if mode in single_byte_modes:
            # The discarded second-cycle fetch belongs to the following MMC3
            # slot when an implied opcode sits at $9FFF/$BFFF/$DFFF. Keep that
            # one byte literal instead of wrapping the current 8 KiB bank.
            if offset < 0x1FFF:
                safe[offset] = int(
                    instruction.name
                    not in {"BRK", "KIL", "CLI", "PLP", "RTI", "SEI"}
                )
            continue
        if mode in {
            M.IMMEDIATE,
            M.ZERO_PAGE,
            M.ZERO_PAGE_X,
            M.ZERO_PAGE_Y,
            M.RELATIVE,
        }:
            if offset < 0x1FFF:
                safe[offset] = 1
            continue
        if mode in {M.INDEXED_INDIRECT, M.INDIRECT_INDEXED}:
            if not instruction.unofficial:
                safe[offset] = 2
            continue
        if offset >= 0x1FFE:
            continue
        address = prg[offset + 1] | (prg[offset + 2] << 8)
        writes = instruction.name in _MEMORY_WRITE_OPERATIONS
        if mode == M.ABSOLUTE:
            if instruction.name in {"JMP", "JSR"}:
                safe[offset] = int(
                    instruction.name == "JSR" or address != pc
                )
            else:
                safe[offset] = int(
                    static_access_is_safe(address, writes)
                )
        elif mode in {M.ABSOLUTE_X, M.ABSOLUTE_Y}:
            safe[offset] = int(indexed_access_is_safe(address, writes))
        elif mode == M.INDIRECT:
            safe[offset] = int(address < 0x2000 or address >= 0x8000)

    counter = bytearray(0x2000)
    for opcode in (0x88, 0xC8, 0xCA, 0xE8):
        pattern = bytes((opcode, 0xD0, 0xFD))
        offset = prg.find(pattern)
        while offset >= 0:
            counter[offset] = opcode
            safe[offset] = 0
            offset = prg.find(pattern, offset + 1)

    poll = bytearray(0x2000)
    read_lengths = {
        0xA4: 2, 0xA5: 2, 0xA6: 2, 0x24: 2,
        0xC4: 2, 0xC5: 2, 0xE4: 2,
        0xAC: 3, 0xAD: 3, 0xAE: 3, 0x2C: 3,
        0xCC: 3, 0xCD: 3, 0xEC: 3,
    }
    branch_opcodes = {
        0x10, 0x30, 0x50, 0x70,
        0x90, 0xB0, 0xD0, 0xF0,
    }
    for offset in range(0x1FFB):
        instruction_length = read_lengths.get(prg[offset])
        if instruction_length is None:
            continue
        if instruction_length == 3:
            read_address = prg[offset + 1] | (prg[offset + 2] << 8)
            if read_address >= 0x2000:
                continue
        branch_offset = offset + instruction_length
        if prg[branch_offset] not in branch_opcodes:
            continue
        relative = prg[branch_offset + 1]
        if relative & 0x80:
            relative -= 0x100
        branch_base = base + branch_offset + 2
        if (branch_base + relative) & 0xFFFF == base + offset:
            poll[offset] = 1
            safe[offset] = 0

    _mark_cooperative_poll_loops(prg, base, poll, safe)

    return bytes(safe), bytes(counter), bytes(poll)


class _SegmentedTableView:
    """Address a four-slot MMC3 classification without joining its banks."""

    __slots__ = ("slots",)

    def __init__(self) -> None:
        self.slots: list[bytes | None] = [None, None, None, None]

    def __getitem__(self, address: int) -> int:
        if address < 0x8000:
            return 0
        offset = address - 0x8000
        table = self.slots[offset >> 13]
        return 0 if table is None else table[offset & 0x1FFF]


class CPU:
    def __init__(self, bus: CPUBus) -> None:
        self.bus = bus
        # The CPU bus object is stable for the life of the console. Caching its
        # bound methods removes two attribute lookups from every memory access.
        self._bus_read = bus.read
        self._bus_write = bus.write
        self._code_read = getattr(bus, "read_code", bus.read)
        self._code_peek = getattr(bus, "peek_code", None)
        self._nrom_prg = getattr(bus, "_nrom_prg", None)
        self._nrom_prg_mask = getattr(bus, "_nrom_prg_mask", 0)
        mapped_prg_windows = getattr(bus, "_mapped_prg_windows", None)
        mapped_prg_banks = getattr(bus, "_mapped_prg_banks", None)
        self._mapped_prg_bus = (
            bus if mapped_prg_windows or mapped_prg_banks else None
        )
        self._active_mapped_prg: bytes | None = None
        self._active_mapped_prg_banks: tuple[bytes, ...] | None = None
        self._segmented_prg_window: bytes | None = None
        self._segmented_stable_observations = 0
        self.segmented_map_compilations = 0
        self.segmented_map_deferrals = 0
        self._banked_batch_tables: dict[
            int, tuple[bytes, bytes, bytes]
        ] | None = None
        self._segmented_batch_tables: dict[
            tuple[bytes, int], tuple[bytes, bytes, bytes]
        ] | None = None
        self._segmented_safe_view: _SegmentedTableView | None = None
        self._segmented_counter_view: _SegmentedTableView | None = None
        self._segmented_poll_view: _SegmentedTableView | None = None
        self._segmented_candidate: tuple[bytes, int] | None = None
        self._batch_block_cache: dict[
            tuple[bytes, int], dict[int, Callable[..., object] | bool]
        ] = {}
        self._batch_block_hot: dict[tuple[bytes, int], dict[int, int]] = {}
        self._translated_block_count = 0
        self.translated_block_compilations = 0
        self.translated_block_deferrals = 0
        self._translated_block_warmup_frames_remaining = (
            BATCH_BLOCK_INITIAL_COMPILE_FRAMES
        )
        self._translated_block_compilation_budget = (
            BATCH_BLOCK_INITIAL_COMPILE_BUDGET
        )
        self._translated_block_deferred_entries: list[
            tuple[dict[int, Callable[..., object] | bool], int]
        ] = []
        self._translated_block_compilations_this_frame = 0
        self.translated_block_peak_frame_compilations = 0
        self._cpu_driven_pcm_checked_prg: bytes | None = None
        self._cpu_driven_pcm_matches = False
        self._counter_loop_opcodes = getattr(
            bus,
            "counter_loop_opcodes",
            None,
        )
        self._poll_loop_starts = getattr(bus, "poll_loop_starts", None)
        if self._nrom_prg is not None:
            safe_batch_starts = bytearray(
                _build_nrom_safe_batch_starts(self._nrom_prg)
            )
            # Preserve the dedicated O(1) recognized-loop paths. A general
            # CPU-only batch can branch back into one of these addresses, so
            # make it yield to the Console scheduler at that exact point.
            counter_starts = self._counter_loop_opcodes
            poll_starts = self._poll_loop_starts
            if counter_starts is not None or poll_starts is not None:
                for address in range(0x8000, 0x10000):
                    if (
                        (
                            counter_starts is not None
                            and counter_starts[address]
                        )
                        or (
                            poll_starts is not None
                            and poll_starts[address]
                        )
                    ):
                        safe_batch_starts[address] = 0
            self._safe_batch_starts = bytes(safe_batch_starts)
        elif mapped_prg_windows:
            banked_tables: dict[int, tuple[bytes, bytes, bytes]] = {}
            for window in mapped_prg_windows:
                safe_batch_starts = bytearray(
                    _build_nrom_safe_batch_starts(window)
                )
                counter_starts, poll_starts = _build_mapped_loop_tables(window)
                for address in range(0x8000, 0x10000):
                    if counter_starts[address] or poll_starts[address]:
                        safe_batch_starts[address] = 0
                # A general CPU-only span may otherwise execute across the
                # exact entry to the specialized software-DAC executor. Make
                # that entry a scheduler yield point only in immutable maps
                # whose complete loop signature matches.
                if _mapped_prg_has_cpu_driven_pcm(window):
                    safe_batch_starts[0x908D] = 0
                banked_tables[id(window)] = (
                    bytes(safe_batch_starts),
                    counter_starts,
                    poll_starts,
                )
            self._banked_batch_tables = banked_tables
            self._safe_batch_starts = None
            self._refresh_banked_fast_tables()
        elif mapped_prg_banks:
            # MMC3 exposes four independently switched 8 KiB slots. Classify
            # only the bank/slot containing code the CPU actually executes.
            # A data bank may churn thousands of times while the CPU stays in
            # the fixed $E000 bank; it must not trigger optimizer work.
            self._segmented_batch_tables = {}
            self._segmented_safe_view = _SegmentedTableView()
            self._segmented_counter_view = _SegmentedTableView()
            self._segmented_poll_view = _SegmentedTableView()
            self._safe_batch_starts = self._segmented_safe_view
            self._counter_loop_opcodes = self._segmented_counter_view
            self._poll_loop_starts = self._segmented_poll_view
            self._refresh_segmented_fast_tables()
        else:
            self._safe_batch_starts = None
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFD
        self.pc = 0
        self.p = int(F.UNUSED | F.INTERRUPT)
        self._irq_inhibit = True
        self.total_cycles = 0
        self.nmi_pending = False
        self._nmi_delay = 0
        self.external_irq_sampling = False
        self._irq_sampled = False
        # Optimized CPU-only batches publish the cycle length of their final
        # instruction so the Console can poll IRQ at that exact instruction
        # boundary if the batch reaches a device event.
        self._last_batch_instruction_cycles = 0
        self.deferred_ppu_stream_batches = 0
        self.halted = False
        self.last_opcode = 0
        self._address_resolvers = (
            self._address_none,
            self._address_none,
            self._address_immediate,
            self._address_zero_page,
            self._address_zero_page_x,
            self._address_zero_page_y,
            self._address_relative,
            self._address_absolute,
            self._address_absolute_x,
            self._address_absolute_y,
            self._address_indirect,
            self._address_indexed_indirect,
            self._address_indirect_indexed,
        )
        fast_handlers = [None] * 256
        for opcode, handler in (
            (0x09, self._op_ora_immediate),
            (0x10, self._op_bpl),
            (0x18, self._op_clc),
            (0x29, self._op_and_immediate),
            (0x30, self._op_bmi),
            (0x38, self._op_sec),
            (0x49, self._op_eor_immediate),
            (0x50, self._op_bvc),
            (0x58, self._op_cli),
            (0x69, self._op_adc_immediate),
            (0x70, self._op_bvs),
            (0x78, self._op_sei),
            (0x85, self._op_sta_zero_page),
            (0x88, self._op_dey),
            (0x8D, self._op_sta_absolute),
            (0x90, self._op_bcc),
            (0x95, self._op_sta_zero_page_x),
            (0x99, self._op_sta_absolute_y),
            (0x9D, self._op_sta_absolute_x),
            (0xA0, self._op_ldy_immediate),
            (0xA2, self._op_ldx_immediate),
            (0xA5, self._op_lda_zero_page),
            (0xA9, self._op_lda_immediate),
            (0xAD, self._op_lda_absolute),
            (0xB0, self._op_bcs),
            (0xB5, self._op_lda_zero_page_x),
            (0xB9, self._op_lda_absolute_y),
            (0xBD, self._op_lda_absolute_x),
            (0xC8, self._op_iny),
            (0xC9, self._op_cmp_immediate),
            (0xCA, self._op_dex),
            (0xD0, self._op_bne),
            (0xE8, self._op_inx),
            (0xE9, self._op_sbc_immediate),
            (0xF0, self._op_beq),
        ):
            fast_handlers[opcode] = handler
        for opcode, instruction in enumerate(OPCODES):
            if fast_handlers[opcode] is None and not instruction.unofficial:
                fast_handlers[opcode] = self._make_generated_fast_handler(
                    instruction
                )
        self._fast_opcode_handlers = tuple(fast_handlers)

    def _refresh_banked_fast_tables(self) -> None:
        """Select preclassified tables for the current immutable PRG map."""
        tables = self._banked_batch_tables
        if tables is None:
            return
        window = getattr(self.bus, "_mapped_prg_window", None)
        if window is self._active_mapped_prg:
            return
        self._active_mapped_prg = window
        if window is None:
            self._safe_batch_starts = None
            self._counter_loop_opcodes = None
            self._poll_loop_starts = None
            return
        (
            self._safe_batch_starts,
            self._counter_loop_opcodes,
            self._poll_loop_starts,
        ) = tables[id(window)]

    def _refresh_segmented_fast_tables(self) -> None:
        """Classify only the MMC3 bank in the CPU's current code slot."""
        tables = self._segmented_batch_tables
        if tables is None:
            return
        banks = getattr(self.bus, "_mapped_prg_banks", None)
        previous_banks = self._active_mapped_prg_banks
        if banks is not previous_banks:
            self._active_mapped_prg_banks = banks
            self._segmented_prg_window = None
            if banks is None:
                for view in (
                    self._segmented_safe_view,
                    self._segmented_counter_view,
                    self._segmented_poll_view,
                ):
                    assert view is not None
                    view.slots[:] = [None, None, None, None]
                return
            safe_view = self._segmented_safe_view
            counter_view = self._segmented_counter_view
            poll_view = self._segmented_poll_view
            assert safe_view is not None
            assert counter_view is not None
            assert poll_view is not None
            for slot, bank in enumerate(banks):
                if (
                    previous_banks is not None
                    and bank is previous_banks[slot]
                ):
                    continue
                table = tables.get((bank, 0x8000 + slot * 0x2000))
                if table is None:
                    safe_view.slots[slot] = None
                    counter_view.slots[slot] = None
                    poll_view.slots[slot] = None
                else:
                    safe_view.slots[slot] = table[0]
                    counter_view.slots[slot] = table[1]
                    poll_view.slots[slot] = table[2]

        pc = getattr(self, "pc", 0)
        if banks is None or pc < 0x8000:
            return
        slot = (pc - 0x8000) >> 13
        safe_view = self._segmented_safe_view
        assert safe_view is not None
        if safe_view.slots[slot] is not None:
            self._segmented_candidate = None
            self._segmented_stable_observations = 0
            return
        base = 0x8000 + slot * 0x2000
        key = (banks[slot], base)
        if key == self._segmented_candidate:
            self._segmented_stable_observations += 1
        else:
            self._segmented_candidate = key
            self._segmented_stable_observations = 1
            self.segmented_map_deferrals += 1
        if (
            self._segmented_stable_observations
            < SEGMENTED_BATCH_STABILITY_OBSERVATIONS
        ):
            return

        table = _build_segment_fast_tables(*key)
        if len(tables) >= SEGMENTED_BATCH_MAP_LIMIT:
            tables.pop(next(iter(tables)))
        tables[key] = table
        counter_view = self._segmented_counter_view
        poll_view = self._segmented_poll_view
        assert counter_view is not None
        assert poll_view is not None
        safe_view.slots[slot] = table[0]
        counter_view.slots[slot] = table[1]
        poll_view.slots[slot] = table[2]
        self._segmented_candidate = None
        self._segmented_stable_observations = 0
        self.segmented_map_compilations += 1

    def _make_generated_fast_handler(
        self,
        instruction: Instruction,
    ) -> Callable[[], int] | None:
        """Build a direct handler for an official fixed operation/mode pair.

        The generic decoder remains the source of truth and handles every
        unofficial encoding. Official game code can skip its repeated string
        dispatch and value/address helper layers after this one-time setup.
        """
        name = instruction.name
        mode = instruction.mode
        cycles = instruction.cycles
        page_cycle = instruction.page_cycle
        resolve = self._address_resolvers[mode]
        read = self._bus_read
        write = self._bus_write
        indexed_dummy = mode in {
            M.ZERO_PAGE_X,
            M.ZERO_PAGE_Y,
            M.ABSOLUTE_X,
            M.ABSOLUTE_Y,
            M.INDIRECT_INDEXED,
        }

        if name in {
            "LDA", "LDX", "LDY", "AND", "EOR", "ORA", "ADC", "SBC",
            "CMP", "CPX", "CPY", "BIT",
        }:
            if name == "LDA":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self.a = self._set_zn(read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "LDX":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self.x = self._set_zn(read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "LDY":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self.y = self._set_zn(read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "AND":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self.a = self._set_zn(self.a & read(address))
                    return cycles + int(page_cycle and crossed)
            elif name == "EOR":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self.a = self._set_zn(self.a ^ read(address))
                    return cycles + int(page_cycle and crossed)
            elif name == "ORA":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self.a = self._set_zn(self.a | read(address))
                    return cycles + int(page_cycle and crossed)
            elif name == "ADC":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self._adc(read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "SBC":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self._adc((read(address) & 0xFF) ^ 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "CMP":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self._compare(self.a, read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "CPX":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self._compare(self.x, read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            elif name == "CPY":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    self._compare(self.y, read(address) & 0xFF)
                    return cycles + int(page_cycle and crossed)
            else:
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, False
                        )
                    value = read(address) & 0xFF
                    self.p = (
                        (self.p & 0x3D)
                        | 0x20
                        | (0x02 if (self.a & value) == 0 else 0)
                        | (value & 0xC0)
                    )
                    return cycles + int(page_cycle and crossed)
            return handler

        if name in {"STA", "STX", "STY"}:
            if name == "STA":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, True
                        )
                    write(address, self.a)
                    return cycles
            elif name == "STX":
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, True
                        )
                    write(address, self.x)
                    return cycles
            else:
                def handler() -> int:
                    address, crossed = resolve()
                    assert address is not None
                    if indexed_dummy:
                        self._indexed_dummy_read(
                            mode, address, crossed, True
                        )
                    write(address, self.y)
                    return cycles
            return handler

        if name in {"ASL", "LSR", "ROL", "ROR", "INC", "DEC"}:
            if mode == M.ACCUMULATOR:
                if name == "ASL":
                    def handler() -> int:
                        self.a = self._asl(self.a)
                        return cycles
                elif name == "LSR":
                    def handler() -> int:
                        self.a = self._lsr(self.a)
                        return cycles
                elif name == "ROL":
                    def handler() -> int:
                        self.a = self._rol(self.a)
                        return cycles
                else:
                    def handler() -> int:
                        self.a = self._ror(self.a)
                        return cycles
                return handler

            if name == "ASL":
                operation = self._asl
            elif name == "LSR":
                operation = self._lsr
            elif name == "ROL":
                operation = self._rol
            elif name == "ROR":
                operation = self._ror
            elif name == "INC":
                operation = lambda value: self._set_zn(value + 1)
            else:
                operation = lambda value: self._set_zn(value - 1)

            def handler() -> int:
                address, crossed = resolve()
                assert address is not None
                if indexed_dummy:
                    self._indexed_dummy_read(
                        mode, address, crossed, True
                    )
                original = read(address) & 0xFF
                # NMOS 6502 memory RMW instructions perform a dummy write of
                # the unmodified value immediately before the final write.
                # Cartridge registers and PPU/APU I/O can observe both.
                write(address, original)
                write(address, operation(original))
                return cycles

            return handler

        if name == "JSR":
            def handler() -> int:
                address, _crossed = resolve()
                assert address is not None
                self._push16((self.pc - 1) & 0xFFFF)
                self.pc = address
                return cycles
            return handler
        if name == "RTS":
            def handler() -> int:
                self.pc = (self._pull16() + 1) & 0xFFFF
                return cycles
            return handler
        if name == "RTI":
            def handler() -> int:
                self.p = (self._pull() & 0xEF) | 0x20
                self.pc = self._pull16()
                return cycles
            return handler

        if name == "TAX":
            def handler() -> int:
                self.x = self._set_zn(self.a)
                return cycles
            return handler
        if name == "TAY":
            def handler() -> int:
                self.y = self._set_zn(self.a)
                return cycles
            return handler
        if name == "TSX":
            def handler() -> int:
                self.x = self._set_zn(self.sp)
                return cycles
            return handler
        if name == "TXA":
            def handler() -> int:
                self.a = self._set_zn(self.x)
                return cycles
            return handler
        if name == "TXS":
            def handler() -> int:
                self.sp = self.x
                return cycles
            return handler
        if name == "TYA":
            def handler() -> int:
                self.a = self._set_zn(self.y)
                return cycles
            return handler
        if name == "PHA":
            def handler() -> int:
                self._push(self.a)
                return cycles
            return handler
        if name == "PHP":
            def handler() -> int:
                self._push(self.p | 0x30)
                return cycles
            return handler
        if name == "PLA":
            def handler() -> int:
                self.a = self._set_zn(self._pull())
                return cycles
            return handler
        if name == "PLP":
            def handler() -> int:
                self.p = (self._pull() & 0xEF) | 0x20
                return cycles
            return handler

        if name == "CLD":
            def handler() -> int:
                self.p = (self.p & 0xF7) | 0x20
                return cycles
            return handler
        if name == "CLV":
            def handler() -> int:
                self.p = (self.p & 0xBF) | 0x20
                return cycles
            return handler
        if name == "SED":
            def handler() -> int:
                self.p |= 0x28
                return cycles
            return handler
        return None

    def reset(self, *, cold: bool = True) -> None:
        if cold:
            self.a = self.x = self.y = 0
            self.sp = 0xFD
            self.p = int(F.UNUSED | F.INTERRUPT)
            self.total_cycles = 7
        else:
            # RESET internally performs three stack reads, decrementing S
            # without writing stack RAM. A, X, Y, and the other status flags
            # survive; only interrupt disable is forced on.
            self.sp = (self.sp - 3) & 0xFF
            self.p |= int(F.UNUSED | F.INTERRUPT)
            self.total_cycles += 7
        self._irq_inhibit = True
        self.pc = self._read16(0xFFFC)
        if self._segmented_batch_tables is not None:
            # Classify the reset-vector bank while the game is loading, not
            # during its first rendered frame. Newly executed switched banks
            # are still admitted lazily on their first scheduler boundary.
            self._refresh_segmented_fast_tables()
        self.nmi_pending = False
        self._nmi_delay = 0
        self._irq_sampled = False
        self._last_batch_instruction_cycles = 0
        self.halted = False

    def request_nmi(self, *, defer: bool = False) -> None:
        if not self.nmi_pending:
            self.nmi_pending = True
            self._nmi_delay = int(bool(defer))

    def cancel_nmi(self) -> None:
        self.nmi_pending = False
        self._nmi_delay = 0

    def latch_irq(self) -> None:
        self._irq_sampled = True

    def read(self, address: int) -> int:
        return self._bus_read(address & 0xFFFF) & 0xFF

    def write(self, address: int, value: int) -> None:
        self._bus_write(address & 0xFFFF, value & 0xFF)

    def _read16(self, address: int) -> int:
        read = self._bus_read
        low = read(address & 0xFFFF)
        high = read((address + 1) & 0xFFFF)
        return low | (high << 8)

    def _read16_zp(self, address: int) -> int:
        read = self._bus_read
        low = read(address & 0xFF)
        high = read((address + 1) & 0xFF)
        return low | (high << 8)

    def _fetch(self) -> int:
        value = self._code_read(self.pc) & 0xFF
        self.pc = (self.pc + 1) & 0xFFFF
        return value

    def _push(self, value: int) -> None:
        self._bus_write(0x100 | self.sp, value & 0xFF)
        self.sp = (self.sp - 1) & 0xFF

    def _pull(self) -> int:
        self.sp = (self.sp + 1) & 0xFF
        return self._bus_read(0x100 | self.sp) & 0xFF

    def _push16(self, value: int) -> None:
        self._push((value >> 8) & 0xFF)
        self._push(value & 0xFF)

    def _pull16(self) -> int:
        low = self._pull()
        return low | (self._pull() << 8)

    def _flag(self, flag: F) -> bool:
        return bool(self.p & int(flag))

    def _set_flag(self, flag: F, enabled: bool) -> None:
        if enabled:
            self.p |= int(flag)
        else:
            self.p &= ~int(flag)
        self.p = (self.p | int(F.UNUSED)) & 0xFF

    def _set_zn(self, value: int) -> int:
        value &= 0xFF
        # ZERO and NEGATIVE are replaced together. This is the hottest flag
        # path in ordinary game code and avoids two helper calls plus repeated
        # IntFlag conversions for every load/increment/logic instruction.
        self.p = (
            (self.p & 0x7D)
            | 0x20
            | (0x02 if value == 0 else 0)
            | (value & 0x80)
        )
        return value

    def _address_none(self) -> tuple[None, bool]:
        return None, False

    def _address_immediate(self) -> tuple[int, bool]:
        address = self.pc
        self.pc = (address + 1) & 0xFFFF
        return address, False

    def _address_zero_page(self) -> tuple[int, bool]:
        pc = self.pc
        self.pc = (pc + 1) & 0xFFFF
        return self._code_read(pc) & 0xFF, False

    def _address_zero_page_x(self) -> tuple[int, bool]:
        pc = self.pc
        self.pc = (pc + 1) & 0xFFFF
        return (self._code_read(pc) + self.x) & 0xFF, False

    def _address_zero_page_y(self) -> tuple[int, bool]:
        pc = self.pc
        self.pc = (pc + 1) & 0xFFFF
        return (self._code_read(pc) + self.y) & 0xFF, False

    def _address_relative(self) -> tuple[int, bool]:
        pc = self.pc
        value = self._code_read(pc) & 0xFF
        self.pc = (pc + 1) & 0xFFFF
        return value - 0x100 if value & 0x80 else value, False

    def _address_absolute(self) -> tuple[int, bool]:
        read = self._code_read
        pc = self.pc
        low = read(pc)
        high = read((pc + 1) & 0xFFFF)
        self.pc = (pc + 2) & 0xFFFF
        return (low | (high << 8)) & 0xFFFF, False

    def _address_absolute_indexed(self, index: int) -> tuple[int, bool]:
        read = self._code_read
        pc = self.pc
        low = read(pc)
        high = read((pc + 1) & 0xFFFF)
        self.pc = (pc + 2) & 0xFFFF
        base = (low | (high << 8)) & 0xFFFF
        address = (base + index) & 0xFFFF
        return address, (base & 0xFF00) != (address & 0xFF00)

    def _address_absolute_x(self) -> tuple[int, bool]:
        return self._address_absolute_indexed(self.x)

    def _address_absolute_y(self) -> tuple[int, bool]:
        return self._address_absolute_indexed(self.y)

    def _address_indirect(self) -> tuple[int, bool]:
        code_read = self._code_read
        read = self._bus_read
        pc = self.pc
        low = code_read(pc)
        high = code_read((pc + 1) & 0xFFFF)
        self.pc = (pc + 2) & 0xFFFF
        pointer = (low | (high << 8)) & 0xFFFF
        # NMOS 6502 wraps the high-byte fetch within the same page.
        target_low = read(pointer)
        target_high = read((pointer & 0xFF00) | ((pointer + 1) & 0xFF))
        return target_low | (target_high << 8), False

    def _address_indexed_indirect(self) -> tuple[int, bool]:
        read = self._bus_read
        pc = self.pc
        operand = self._code_read(pc) & 0xFF
        self.pc = (pc + 1) & 0xFFFF
        # Every (zero-page,X) instruction reads the unindexed zero-page
        # operand during its extra indexing cycle.
        read(operand)
        pointer = (operand + self.x) & 0xFF
        return (
            (read(pointer) | (read((pointer + 1) & 0xFF) << 8)) & 0xFFFF,
            False,
        )

    def _address_indirect_indexed(self) -> tuple[int, bool]:
        read = self._bus_read
        pc = self.pc
        pointer = self._code_read(pc) & 0xFF
        self.pc = (pc + 1) & 0xFFFF
        base = (
            read(pointer) | (read((pointer + 1) & 0xFF) << 8)
        ) & 0xFFFF
        address = (base + self.y) & 0xFFFF
        return address, (base & 0xFF00) != (address & 0xFF00)

    def _resolve(self, mode: M) -> tuple[int | None, bool]:
        return self._address_resolvers[mode]()

    def _indexed_dummy_read(
        self,
        mode: M,
        address: int,
        crossed: bool,
        always: bool,
    ) -> None:
        """Issue the NMOS 6502's observable intermediate indexed read."""
        if mode == M.ZERO_PAGE_X:
            self._bus_read((address - self.x) & 0xFF)
            return
        if mode == M.ZERO_PAGE_Y:
            self._bus_read((address - self.y) & 0xFF)
            return
        if not (always or crossed):
            return
        if mode == M.ABSOLUTE_X:
            index = self.x
        elif mode in (M.ABSOLUTE_Y, M.INDIRECT_INDEXED):
            index = self.y
        else:
            return
        base = (address - index) & 0xFFFF
        self._bus_read((base & 0xFF00) | (address & 0x00FF))

    def _interrupt(self, vector: int) -> int:
        self._push16(self.pc)
        self._push((self.p & ~int(F.BREAK)) | int(F.UNUSED))
        vector = self._select_interrupt_vector(vector)
        self._set_flag(F.INTERRUPT, True)
        self._irq_inhibit = True
        self.pc = self._read16(vector)
        return 7

    def _select_interrupt_vector(
        self,
        vector: int,
        access_cycle: int = 4,
    ) -> int:
        """Apply the NMOS 6502's NMI-over-IRQ/BRK vector priority."""
        if vector != 0xFFFE:
            return vector

        hijacked = False
        if self.nmi_pending:
            self.nmi_pending = False
            self._nmi_delay = 0
            hijacked = True
        sync_vector = getattr(self.bus, "sync_interrupt_vector", None)
        if sync_vector is not None and sync_vector(access_cycle):
            hijacked = True
        return 0xFFFA if hijacked else vector

    def step(self) -> int:
        # Bus-visible device accesses publish their position within this
        # instruction. The Console can then advance PPU/APU clocks to the
        # correct CPU cycle before applying the side effect.
        self.bus.cpu_access_cycle = 0  # type: ignore[attr-defined]
        if self.halted:
            self.total_cycles += 1
            return 1
        if self.nmi_pending:
            if self._nmi_delay:
                self._nmi_delay -= 1
            else:
                self.nmi_pending = False
                # NMI wins interrupt arbitration when both inputs were
                # recognized at the same boundary. Do not retain a stale IRQ
                # sample for immediate service after the NMI handler.
                self._irq_sampled = False
                cycles = self._interrupt(0xFFFA)
                self.total_cycles += cycles
                return cycles
        if self._irq_sampled:
            self._irq_sampled = False
            cycles = self._interrupt(0xFFFE)
            self.total_cycles += cycles
            return cycles
        if (
            not self.external_irq_sampling
            and not self._irq_inhibit
            and self.bus.irq_pending
        ):
            cycles = self._interrupt(0xFFFE)
            self.total_cycles += cycles
            return cycles

        old_interrupt_inhibit = bool(self.p & int(F.INTERRUPT))
        pc = self.pc
        nrom_prg = self._nrom_prg
        segmented_banks = (
            self._active_mapped_prg_banks
            if self._segmented_batch_tables is not None
            else None
        )
        mapped_prg = (
            getattr(self.bus, "_mapped_prg_window", None)
            if self._mapped_prg_bus is not None
            else None
        )
        if nrom_prg is not None and pc >= 0x8000:
            opcode = nrom_prg[
                (pc - 0x8000) & self._nrom_prg_mask
            ]
            self.bus.open_bus = opcode  # type: ignore[attr-defined]
        elif mapped_prg is not None and pc >= 0x8000:
            opcode = mapped_prg[pc - 0x8000]
            self.bus.open_bus = opcode  # type: ignore[attr-defined]
        elif segmented_banks is not None and pc >= 0x8000:
            offset = pc - 0x8000
            opcode = segmented_banks[offset >> 13][offset & 0x1FFF]
            self.bus.open_bus = opcode  # type: ignore[attr-defined]
        else:
            opcode = self._code_read(pc) & 0xFF
        self.pc = (pc + 1) & 0xFFFF
        self.last_opcode = opcode
        instruction = OPCODES[opcode]
        final_access_cycle = max(
            0,
            instruction.cycles - 1,
        )
        self.bus.cpu_access_cycle = final_access_cycle  # type: ignore[attr-defined]

        if instruction.mode in (M.IMPLIED, M.ACCUMULATOR):
            # Every one-byte opcode performs a second-cycle fetch from the
            # following address. It is normally discarded, but reads of PPU
            # or other I/O space observe its side effects.
            dummy_pc = self.pc
            self.bus.cpu_access_cycle = 1  # type: ignore[attr-defined]
            if nrom_prg is not None and dummy_pc >= 0x8000:
                self.bus.open_bus = nrom_prg[  # type: ignore[attr-defined]
                    (dummy_pc - 0x8000) & self._nrom_prg_mask
                ]
            elif mapped_prg is not None and dummy_pc >= 0x8000:
                self.bus.open_bus = mapped_prg[  # type: ignore[attr-defined]
                    dummy_pc - 0x8000
                ]
            elif segmented_banks is not None and dummy_pc >= 0x8000:
                offset = dummy_pc - 0x8000
                self.bus.open_bus = segmented_banks[  # type: ignore[attr-defined]
                    offset >> 13
                ][offset & 0x1FFF]
            else:
                self._bus_read(dummy_pc)
            self.bus.cpu_access_cycle = final_access_cycle  # type: ignore[attr-defined]

        # The absolute jump is both common and frequently used as an idle
        # loop. Its fixed operation and addressing mode can bypass the generic
        # decoder without changing any bus-visible reads.
        if opcode == 0x4C:
            pc = self.pc
            if nrom_prg is not None and 0x8000 <= pc < 0xFFFF:
                mask = self._nrom_prg_mask
                low = nrom_prg[(pc - 0x8000) & mask]
                high = nrom_prg[(pc + 1 - 0x8000) & mask]
                self.bus.open_bus = high  # type: ignore[attr-defined]
            elif mapped_prg is not None and 0x8000 <= pc < 0xFFFF:
                low = mapped_prg[pc - 0x8000]
                high = mapped_prg[pc + 1 - 0x8000]
                self.bus.open_bus = high  # type: ignore[attr-defined]
            elif segmented_banks is not None and 0x8000 <= pc < 0xFFFF:
                offset = pc - 0x8000
                low = segmented_banks[offset >> 13][offset & 0x1FFF]
                offset += 1
                high = segmented_banks[offset >> 13][offset & 0x1FFF]
                self.bus.open_bus = high  # type: ignore[attr-defined]
            else:
                read = self._code_read
                low = read(pc)
                high = read((pc + 1) & 0xFFFF)
            self.pc = (low | (high << 8)) & 0xFFFF
            self._irq_inhibit = bool(self.p & int(F.INTERRUPT))
            self.total_cycles += 3
            return 3
        if opcode == 0xEA:
            self._irq_inhibit = bool(self.p & int(F.INTERRUPT))
            self.total_cycles += 2
            return 2

        handler = self._fast_opcode_handlers[opcode]
        if handler is not None:
            cycles = handler()
            self._irq_inhibit = (
                old_interrupt_inhibit
                if opcode in (0x28, 0x58, 0x78)
                else bool(self.p & int(F.INTERRUPT))
            )
            self.total_cycles += cycles
            return cycles

        address, crossed = self._address_resolvers[instruction.mode]()
        if address is not None and instruction.mode in {
            M.ZERO_PAGE_X,
            M.ZERO_PAGE_Y,
            M.ABSOLUTE_X,
            M.ABSOLUTE_Y,
            M.INDIRECT_INDEXED,
        }:
            self._indexed_dummy_read(
                instruction.mode,
                address,
                crossed,
                instruction.name in _MEMORY_WRITE_OPERATIONS,
            )
        extra = self._execute(instruction.name, instruction.mode, address)
        cycles = instruction.cycles + extra
        if crossed and instruction.page_cycle:
            cycles += 1
        self._irq_inhibit = (
            old_interrupt_inhibit
            if opcode in (0x28, 0x58, 0x78)
            else bool(self.p & int(F.INTERRUPT))
        )
        self.total_cycles += cycles
        return cycles

    def begin_translated_block_frame(self) -> None:
        """Open the bounded optional-code-generation budget for one frame."""
        # True is a transient "deferred this frame" cache sentinel; False is
        # the existing permanent "not compilable" sentinel. Removing the few
        # transient entries here makes every repeated visit after a closed
        # budget a single dictionary lookup instead of allocating/checking a
        # tuple in a side set on each scheduler dispatch.
        for cache, pc in self._translated_block_deferred_entries:
            if cache.get(pc) is True:
                del cache[pc]
        self._translated_block_deferred_entries.clear()
        if self._translated_block_warmup_frames_remaining > 0:
            self._translated_block_warmup_frames_remaining -= 1
            self._translated_block_compilation_budget = (
                BATCH_BLOCK_INITIAL_COMPILE_BUDGET
            )
        else:
            self._translated_block_compilation_budget = (
                BATCH_BLOCK_COMPILE_BUDGET_PER_FRAME
            )
        self._translated_block_compilations_this_frame = 0

    def _reserve_translated_block_compilation(
        self,
        cache: dict[int, Callable[..., object] | bool],
        pc: int,
    ) -> bool:
        if self._translated_block_compilation_budget <= 0:
            # Count and mark one deferred hot start per frame. The cache
            # sentinel prevents this method from being reached again for the
            # same tight loop until begin_translated_block_frame() removes it.
            cache[pc] = True
            self._translated_block_deferred_entries.append((cache, pc))
            self.translated_block_deferrals += 1
            return False
        self._translated_block_compilation_budget -= 1
        return True

    def _record_translated_block_compilation(self) -> None:
        self.translated_block_compilations += 1
        self._translated_block_compilations_this_frame += 1
        self.translated_block_peak_frame_compilations = max(
            self.translated_block_peak_frame_compilations,
            self._translated_block_compilations_this_frame,
        )

    def batch_safe_instructions(
        self,
        max_cycles: int,
        _hot_slot_hops: int = 0,
    ) -> int:
        """Run RAM/immutable-PRG instructions inside one scheduler dispatch.

        The caller supplies the number of CPU cycles until the next timed
        PPU/APU/DMC event.  The final instruction may cross that boundary, just
        as it does in the ordinary instruction-at-a-time scheduler, but no
        following instruction is started before device clocks are flushed.
        """
        self._last_batch_instruction_cycles = 0
        starts = self._safe_batch_starts
        start_kind = 0 if starts is None else starts[self.pc]
        if (
            starts is None
            or max_cycles <= 0
            or self.halted
            or self.nmi_pending
            or self._irq_sampled
            or self._irq_inhibit
            != bool(self.p & int(F.INTERRUPT))
            or (
                not self._irq_inhibit
                and self.bus.irq_pending
            )
            or start_kind == 0
            or (
                start_kind == 2
                and not self._dynamic_batch_start_is_safe()
            )
        ):
            return 0

        # Inline the already-specialized PRG dispatch. Calling step() for
        # every instruction would retain millions of Python call/interrupt
        # checks even though this span cannot advance a device interrupt.
        nrom_prg = self._nrom_prg
        if nrom_prg is not None:
            prg = nrom_prg
            mask = self._nrom_prg_mask
            prg_base = 0x8000
            batch_base = 0x8000
            batch_end = 0x10000
            safe_index_base = 0
        elif self._segmented_batch_tables is not None:
            banks = self._active_mapped_prg_banks
            pc_slot = (self.pc - 0x8000) >> 13
            if banks is None or not 0 <= pc_slot < 4:
                return 0
            prg = banks[pc_slot]
            mask = 0x1FFF
            prg_base = 0x8000 + pc_slot * 0x2000
            batch_base = prg_base
            batch_end = prg_base + 0x2000
            safe_view = self._segmented_safe_view
            assert safe_view is not None
            segment_starts = safe_view.slots[pc_slot]
            if segment_starts is None:
                return 0
            # The batch cannot cross its 8 KiB slot. Index its raw immutable
            # classification bytes directly instead of calling
            # _SegmentedTableView.__getitem__ for every 6502 instruction.
            starts = segment_starts
            safe_index_base = prg_base
        else:
            prg = getattr(self.bus, "_mapped_prg_window", None)
            if prg is None:
                return 0
            mask = 0x7FFF
            prg_base = 0x8000
            batch_base = 0x8000
            batch_end = 0x10000
            safe_index_base = 0
        bus = self.bus
        ram = getattr(bus, "ram", None)
        fast_handlers = self._fast_opcode_handlers
        resolvers = self._address_resolvers
        opcodes = OPCODES
        opcode_kinds = _BATCH_OPCODE_KINDS
        dummy_fetches = _BATCH_DUMMY_FETCHES
        run_misc = _run_batch_misc_instruction
        run_memory = _run_batch_memory_instruction
        interrupt_flag = int(F.INTERRUPT)
        memory_write_operations = _MEMORY_WRITE_OPERATIONS
        dynamic_indirect_opcodes = _DYNAMIC_INDIRECT_BATCH_OPCODES
        apu = getattr(bus, "apu", None)
        dynamic_indirect_allowed = not (
            apu is not None
            and (
                apu.cpu_stall_cycles
                or apu.dmc.bytes_remaining
                or apu.dmc.sample_buffer is not None
            )
        )
        cycles = 0
        total_cycles = self.total_cycles
        pc = self.pc
        a = self.a
        x = self.x
        y = self.y
        p = self.p
        sp = self.sp
        open_bus = bus.open_bus  # type: ignore[attr-defined]
        last_opcode = self.last_opcode
        last_instruction_cycles = 0
        translated_block: Callable[..., object] | None = None
        translated_blocks: dict[int, Callable[..., object] | bool] | None = None
        if ram is not None:
            block_bank_key = (prg, prg_base)
            translated_blocks = self._batch_block_cache.setdefault(
                block_bank_key,
                {},
            )
            candidate_block = translated_blocks.get(pc)
            if callable(candidate_block):
                translated_block = candidate_block
            elif candidate_block is None:
                hot = self._batch_block_hot.setdefault(block_bank_key, {})
                observations = hot.get(pc, 0)
                if observations < BATCH_BLOCK_HOT_THRESHOLD:
                    observations += 1
                    hot[pc] = observations
                if observations >= BATCH_BLOCK_HOT_THRESHOLD:
                    if (
                        self._translated_block_count
                        >= BATCH_BLOCK_CACHE_LIMIT
                    ):
                        translated_blocks[pc] = False
                    elif not self._reserve_translated_block_compilation(
                        translated_blocks,
                        pc,
                    ):
                        pass
                    else:
                        compiled = _compile_batch_block(
                            prg,
                            prg_base,
                            mask,
                            pc,
                            starts,
                            safe_index_base,
                            batch_base,
                            batch_end,
                            segmented=(
                                self._segmented_batch_tables is not None
                            ),
                        )
                        if compiled is None:
                            translated_blocks[pc] = False
                        else:
                            translated_blocks[pc] = compiled
                            translated_block = compiled
                            self._translated_block_count += 1
                            self._record_translated_block_compilation()
        def dynamic_access_is_safe(
            address_pc: int,
            register_x: int,
            register_y: int,
        ) -> bool:
            """Resolve a ROM-stream indirect access without touching devices."""
            if ram is None or not dynamic_indirect_allowed:
                return False
            opcode_value = prg[(address_pc - prg_base) & mask]
            if not dynamic_indirect_opcodes[opcode_value]:
                return False
            instruction = opcodes[opcode_value]
            operand = prg[(address_pc + 1 - prg_base) & mask]
            if instruction.mode == M.INDEXED_INDIRECT:
                pointer = (operand + register_x) & 0xFF
                low = ram[pointer]
                high = ram[(pointer + 1) & 0xFF]
                address = low | (high << 8)
                dummy_address = address
            else:
                low = ram[operand]
                high = ram[(operand + 1) & 0xFF]
                base_address = low | (high << 8)
                address = (base_address + register_y) & 0xFFFF
                dummy_address = (
                    (base_address & 0xFF00)
                    | (address & 0x00FF)
                )

            writes = instruction.name in memory_write_operations
            if writes:
                return address < 0x2000 and dummy_address < 0x2000
            return (
                (
                    address < 0x2000
                    and dummy_address < 0x2000
                )
                or (
                    address >= 0x8000
                    and dummy_address >= 0x8000
                )
            )

        while (
            batch_base <= pc < batch_end
            and (
                starts[pc - safe_index_base] == 1
                or (
                    starts[pc - safe_index_base] == 2
                    and dynamic_access_is_safe(pc, x, y)
                )
            )
        ):
            if translated_block is not None:
                (
                    a,
                    x,
                    y,
                    p,
                    sp,
                    pc,
                    open_bus,
                    block_cycles,
                    last_opcode,
                    last_instruction_cycles,
                ) = translated_block(
                    a,
                    x,
                    y,
                    p,
                    sp,
                    open_bus,
                    max_cycles - cycles,
                    ram,
                    self._active_mapped_prg_banks,
                )
                total_cycles += block_cycles
                cycles += block_cycles
                if cycles >= max_cycles:
                    break
                assert translated_blocks is not None
                candidate_block = translated_blocks.get(pc)
                if candidate_block is None:
                    hot = self._batch_block_hot.setdefault(
                        (prg, prg_base),
                        {},
                    )
                    observations = hot.get(pc, 0)
                    if observations < BATCH_BLOCK_HOT_THRESHOLD:
                        observations += 1
                        hot[pc] = observations
                    if observations >= BATCH_BLOCK_HOT_THRESHOLD:
                        if (
                            self._translated_block_count
                            >= BATCH_BLOCK_CACHE_LIMIT
                        ):
                            translated_blocks[pc] = False
                            candidate_block = False
                        elif not self._reserve_translated_block_compilation(
                            translated_blocks,
                            pc,
                        ):
                            candidate_block = False
                        else:
                            compiled = _compile_batch_block(
                                prg,
                                prg_base,
                                mask,
                                pc,
                                starts,
                                safe_index_base,
                                batch_base,
                                batch_end,
                                segmented=(
                                    self._segmented_batch_tables is not None
                                ),
                            )
                            if compiled is None:
                                translated_blocks[pc] = False
                                candidate_block = False
                            else:
                                translated_blocks[pc] = compiled
                                candidate_block = compiled
                                self._translated_block_count += 1
                                self._record_translated_block_compilation()
                translated_block = (
                    candidate_block if callable(candidate_block) else None
                )
                continue
            opcode = prg[(pc - prg_base) & mask]
            open_bus = opcode
            pc = (pc + 1) & 0xFFFF
            last_opcode = opcode
            kind = opcode_kinds[opcode]
            if dummy_fetches[opcode]:
                # Safe batches are restricted to immutable PRG here, so the
                # otherwise-discarded second-cycle fetch is a direct lookup.
                open_bus = prg[(pc - prg_base) & mask]

            if kind == _BATCH_JMP_ABSOLUTE:
                operand_pc = pc
                low = prg[(operand_pc - prg_base) & mask]
                high = prg[(operand_pc + 1 - prg_base) & mask]
                open_bus = high
                pc = (low | (high << 8)) & 0xFFFF
                instruction_cycles = 3
            elif kind == _BATCH_NOP:
                instruction_cycles = 2
            elif kind == _BATCH_IMMEDIATE:
                operand_pc = pc
                value = prg[(operand_pc - prg_base) & mask]
                open_bus = value
                pc = (operand_pc + 1) & 0xFFFF
                if opcode == 0xA9:
                    a = value
                    p = (
                        (p & 0x7D)
                        | 0x20
                        | (0x02 if value == 0 else 0)
                        | (value & 0x80)
                    )
                elif opcode == 0xA2:
                    x = value
                    p = (
                        (p & 0x7D)
                        | 0x20
                        | (0x02 if value == 0 else 0)
                        | (value & 0x80)
                    )
                elif opcode == 0xA0:
                    y = value
                    p = (
                        (p & 0x7D)
                        | 0x20
                        | (0x02 if value == 0 else 0)
                        | (value & 0x80)
                    )
                elif opcode in (0xC0, 0xC9, 0xE0):
                    if opcode == 0xC0:
                        register = y
                    elif opcode == 0xE0:
                        register = x
                    else:
                        register = a
                    result = (register - value) & 0xFF
                    p = (
                        (p & 0x7C)
                        | 0x20
                        | int(register >= value)
                        | (0x02 if result == 0 else 0)
                        | (result & 0x80)
                    )
                elif opcode in (0x69, 0xE9):
                    if opcode == 0xE9:
                        value ^= 0xFF
                    accumulator = a
                    total = accumulator + value + (p & 0x01)
                    result = total & 0xFF
                    p = (
                        (p & 0x3C)
                        | 0x20
                        | int(total > 0xFF)
                        | (0x02 if result == 0 else 0)
                        | (
                            0x40
                            if (
                                ~(accumulator ^ value)
                                & (accumulator ^ result)
                            ) & 0x80
                            else 0
                        )
                        | (result & 0x80)
                    )
                    a = result
                else:
                    if opcode == 0x29:
                        result = a & value
                    elif opcode == 0x49:
                        result = a ^ value
                    else:
                        result = a | value
                    a = result
                    p = (
                        (p & 0x7D)
                        | 0x20
                        | (0x02 if result == 0 else 0)
                        | (result & 0x80)
                    )
                instruction_cycles = 2
            elif kind == _BATCH_CARRY:
                if opcode == 0x18:
                    p = (p & 0xFE) | 0x20
                else:
                    p |= 0x21
                instruction_cycles = 2
            elif kind == _BATCH_INDEX:
                if opcode == 0x88:
                    result = (y - 1) & 0xFF
                    y = result
                elif opcode == 0xC8:
                    result = (y + 1) & 0xFF
                    y = result
                elif opcode == 0xCA:
                    result = (x - 1) & 0xFF
                    x = result
                else:
                    result = (x + 1) & 0xFF
                    x = result
                p = (
                    (p & 0x7D)
                    | 0x20
                    | (0x02 if result == 0 else 0)
                    | (result & 0x80)
                )
                instruction_cycles = 2
            elif ram is not None and kind == _BATCH_ZERO_PAGE:
                operand_pc = pc
                address = prg[(operand_pc - prg_base) & mask]
                open_bus = address
                pc = (operand_pc + 1) & 0xFFFF
                if opcode in (0x95, 0xB5):
                    address = (address + x) & 0xFF
                if opcode in (0x85, 0x95):
                    value = a
                    ram[address] = value
                else:
                    value = ram[address]
                    a = value
                    p = (
                        (p & 0x7D)
                        | 0x20
                        | (0x02 if value == 0 else 0)
                        | (value & 0x80)
                    )
                open_bus = value
                instruction_cycles = 3 if opcode in (0x85, 0xA5) else 4
            elif kind == _BATCH_BRANCH:
                operand_pc = pc
                value = prg[(operand_pc - prg_base) & mask]
                open_bus = value
                old_pc = (operand_pc + 1) & 0xFFFF
                pc = old_pc
                if opcode == 0x10:
                    taken = not (p & 0x80)
                elif opcode == 0x30:
                    taken = bool(p & 0x80)
                elif opcode == 0x50:
                    taken = not (p & 0x40)
                elif opcode == 0x70:
                    taken = bool(p & 0x40)
                elif opcode == 0x90:
                    taken = not (p & 0x01)
                elif opcode == 0xB0:
                    taken = bool(p & 0x01)
                elif opcode == 0xD0:
                    taken = not (p & 0x02)
                else:
                    taken = bool(p & 0x02)
                if taken:
                    offset = value - 0x100 if value & 0x80 else value
                    pc = (old_pc + offset) & 0xFFFF
                    instruction_cycles = 3 + int(
                        (old_pc & 0xFF00) != (pc & 0xFF00)
                    )
                else:
                    instruction_cycles = 2
            elif ram is not None and opcode in (0xA1, 0xB1):
                # Scene/audio command decoders commonly stream immutable PRG
                # data through LDA (d),Y. The dynamic admission check above
                # proved that every access remains in RAM/ROM, so keep this
                # hot form in locals instead of publishing the whole CPU
                # object merely to call the generic addressing handler.
                operand = prg[(pc - prg_base) & mask]
                pc = (pc + 1) & 0xFFFF
                if opcode == 0xA1:
                    pointer = (operand + x) & 0xFF
                    low = ram[pointer]
                    high = ram[(pointer + 1) & 0xFF]
                    address = low | (high << 8)
                    crossed = False
                    instruction_cycles = 6
                else:
                    low = ram[operand]
                    high = ram[(operand + 1) & 0xFF]
                    base_address = low | (high << 8)
                    address = (base_address + y) & 0xFFFF
                    crossed = (
                        (base_address & 0xFF00)
                        != (address & 0xFF00)
                    )
                    instruction_cycles = 5 + int(crossed)
                if address < 0x2000:
                    value = ram[address & 0x07FF]
                elif nrom_prg is not None:
                    value = nrom_prg[
                        (address - 0x8000) & self._nrom_prg_mask
                    ]
                elif self._segmented_batch_tables is not None:
                    mapped_banks = self._active_mapped_prg_banks
                    assert mapped_banks is not None
                    offset = address - 0x8000
                    value = mapped_banks[offset >> 13][offset & 0x1FFF]
                else:
                    value = prg[address - 0x8000]
                a = value
                p = (
                    (p & 0x7D)
                    | 0x20
                    | (0x02 if value == 0 else 0)
                    | (value & 0x80)
                )
                open_bus = value
            elif ram is not None and kind == _BATCH_MEMORY_MISC:
                (
                    a,
                    x,
                    y,
                    p,
                    pc,
                    open_bus,
                    instruction_cycles,
                ) = run_memory(
                    opcode,
                    a,
                    x,
                    y,
                    p,
                    pc,
                    ram,
                    prg,
                    prg_base,
                    mask,
                    nrom_prg,
                    self._nrom_prg_mask,
                    self._active_mapped_prg_banks,
                )
            elif ram is not None and kind == _BATCH_MISC:
                (
                    a,
                    x,
                    y,
                    p,
                    sp,
                    pc,
                    open_bus,
                    instruction_cycles,
                ) = run_misc(
                    opcode,
                    a,
                    x,
                    y,
                    p,
                    sp,
                    pc,
                    open_bus,
                    ram,
                    prg,
                    prg_base,
                    mask,
                )
            else:
                # Less common instructions retain the generated/general
                # handlers. Publish the local core state only at that boundary;
                # the overwhelmingly common ALU/RAM/branch path above stays
                # entirely in Python locals.
                self.a = a
                self.x = x
                self.y = y
                self.p = p
                self.sp = sp
                self.pc = pc
                self.last_opcode = last_opcode
                self.total_cycles = total_cycles
                bus.open_bus = open_bus  # type: ignore[attr-defined]
                instruction = opcodes[opcode]
                handler = fast_handlers[opcode]
                if handler is not None:
                    instruction_cycles = handler()
                else:
                    address, crossed = resolvers[instruction.mode]()
                    if address is not None and instruction.mode in {
                        M.ZERO_PAGE_X,
                        M.ZERO_PAGE_Y,
                        M.ABSOLUTE_X,
                        M.ABSOLUTE_Y,
                        M.INDIRECT_INDEXED,
                    }:
                        self._indexed_dummy_read(
                            instruction.mode,
                            address,
                            crossed,
                            instruction.name in memory_write_operations,
                        )
                    extra = self._execute(
                        instruction.name,
                        instruction.mode,
                        address,
                    )
                    instruction_cycles = instruction.cycles + extra
                    if crossed and instruction.page_cycle:
                        instruction_cycles += 1
                a = self.a
                x = self.x
                y = self.y
                p = self.p
                sp = self.sp
                pc = self.pc
                open_bus = bus.open_bus  # type: ignore[attr-defined]

            total_cycles += instruction_cycles
            cycles += instruction_cycles
            last_instruction_cycles = instruction_cycles
            if cycles >= max_cycles:
                break
        self.a = a
        self.x = x
        self.y = y
        self.p = p
        self.sp = sp
        self.pc = pc
        self.last_opcode = last_opcode
        self.total_cycles = total_cycles
        self._last_batch_instruction_cycles = last_instruction_cycles
        bus.open_bus = open_bus  # type: ignore[attr-defined]
        self._irq_inhibit = bool(p & interrupt_flag)
        if (
            cycles < max_cycles
            and _hot_slot_hops < BATCH_HOT_SLOT_CHAIN_LIMIT
            and self._segmented_batch_tables is not None
            and 0x8000 <= pc < 0x10000
            and not batch_base <= pc < batch_end
        ):
            # Chain only to a block that was compiled in an earlier dispatch.
            # Version 2.2.7 recursed into every merely-safe target, which
            # reduced a counter but let cold compilation/setup bursts deepen
            # field frame tails. This hot-only gate retains the useful steady
            # JSR/RTS path without moving cold work into the current frame.
            mapped_banks = self._active_mapped_prg_banks
            target_slot = (pc - 0x8000) >> 13
            target_block = None
            if mapped_banks is not None and 0 <= target_slot < 4:
                target_bank = mapped_banks[target_slot]
                target_base = 0x8000 + target_slot * 0x2000
                target_cache = self._batch_block_cache.get(
                    (target_bank, target_base)
                )
                if target_cache is not None:
                    candidate = target_cache.get(pc)
                    if callable(candidate):
                        target_block = candidate
            if target_block is not None:
                previous_last_cycles = last_instruction_cycles
                chained_cycles = self.batch_safe_instructions(
                    max_cycles - cycles,
                    _hot_slot_hops + 1,
                )
                if chained_cycles:
                    return cycles + chained_cycles
                self._last_batch_instruction_cycles = previous_last_cycles
        return cycles

    def batch_cpu_driven_pcm_sample(
        self,
        max_cycles: int,
    ) -> tuple[int, tuple[tuple[int, int], ...]] | None:
        """Execute a bounded run of recognized software-DAC sample periods.

        Some NES cartridges play sampled audio by spending almost the entire
        CPU frame in a carefully timed loop and writing each output value to
        $4011.  Device batching alone cannot help much because every DAC write
        is an observable boundary.  This executor recognizes Punch-Out's
        immutable loop and folds its RAM/ROM-only instructions into arithmetic,
        returning every exact DAC-write cycle offset and value in the span.

        The common non-wrapping compressed-byte refill is included so one
        scheduler dispatch can cover many samples. Pointer wraps, command-table
        transitions, and routine exits remain on the literal core.
        """
        self._last_batch_instruction_cycles = 0
        if (
            self.pc != 0x908D
            or max_cycles < 130
            or self.halted
            or self.nmi_pending
            or self._irq_sampled
            or not self._irq_inhibit
            or not (self.p & int(F.INTERRUPT))
        ):
            return None

        bus = self.bus
        ram = getattr(bus, "ram", None)
        if ram is None:
            return None
        sample_pairs = ram[0x07C0]
        if not 1 <= sample_pairs <= 4:
            return None

        if self._nrom_prg is not None:
            prg = self._nrom_prg
            mask = self._nrom_prg_mask

            def rom_slice(start: int, end: int) -> bytes:
                return bytes(
                    prg[(address - 0x8000) & mask]
                    for address in range(start, end)
                )

            def rom_byte(address: int) -> int:
                return prg[(address - 0x8000) & mask]
        else:
            prg = getattr(bus, "_mapped_prg_window", None)
            if prg is None:
                return None

            def rom_slice(start: int, end: int) -> bytes:
                return prg[start - 0x8000:end - 0x8000]

            def rom_byte(address: int) -> int:
                return prg[address - 0x8000]

        if prg is not self._cpu_driven_pcm_checked_prg:
            self._cpu_driven_pcm_checked_prg = prg
            self._cpu_driven_pcm_matches = (
                _mapped_prg_has_cpu_driven_pcm(prg)
                if len(prg) == 0x8000
                else bool(
                    rom_slice(0x9063, 0x908D) == _CPU_DRIVEN_PCM_HEAD
                    and rom_slice(0x9018, 0x9024)
                    == _CPU_DRIVEN_PCM_DELAY3
                    and rom_slice(0x9024, 0x9063)
                    == _CPU_DRIVEN_PCM_REFILL
                    and rom_slice(0x908D, 0x90B5)
                    == _CPU_DRIVEN_PCM_TAIL
                    and rom_slice(0x90B5, 0x90C2)
                    == _CPU_DRIVEN_PCM_POSITIVE
                )
            )
        if not self._cpu_driven_pcm_matches:
            return None

        p = self.p
        source_byte = ram[0x07C2]
        previous_level = ram[0x07C4]
        delay_value = ram[0x07C6]
        delay_index = ram[0x07E1]
        delay_counter = ram[0x07FF]
        pointer_low = ram[0x00FE]
        pointer_high = ram[0x00FF]
        events: list[tuple[int, int]] = []
        span_cycles = 0
        final_a = self.a
        final_y = self.y

        def set_zn(value: int) -> None:
            nonlocal p
            p = (
                (p & 0x7D)
                | 0x20
                | (0x02 if value == 0 else 0)
                | (value & 0x80)
            )

        # APU frame events usually leave room for dozens of output periods.
        # Bound the Python list as a safety guard even if a future scheduler
        # supplies a much larger deadline.
        while len(events) < 64:
            # The longest admitted period is below 123 cycles. Reserve its
            # seven-cycle frame/NMI margin before changing any local state so
            # a rejected final period cannot leak into the committed batch.
            if span_cycles + 130 > max_cycles:
                break
            sample_cycles = 0
            if sample_pairs > 1:
                # $908D-$90B4: shift to the next two-bit source sample and
                # run the calibrated delay body.
                next_delay_value = rom_byte(
                    (0x90DB + delay_index) & 0xFFFF
                )
                if next_delay_value == 0:
                    break
                source = source_byte >> 1
                p = (
                    (p & 0x7C)
                    | 0x20
                    | (source_byte & 1)
                    | (0x02 if source == 0 else 0)
                )
                source_byte = source >> 1
                p = (
                    (p & 0x7C)
                    | 0x20
                    | (source & 1)
                    | (0x02 if source_byte == 0 else 0)
                )
                sample_pairs = (sample_pairs - 1) & 0xFF
                p = (
                    (p & 0x7D)
                    | 0x20
                    | (0x02 if sample_pairs == 0 else 0)
                    | (sample_pairs & 0x80)
                )
                set_zn(delay_index)
                delay_value = next_delay_value
                set_zn(delay_value)
                delay_counter = (delay_counter + 5) & 0xFF
                set_zn(delay_counter)
                sample_cycles = 67 + int(
                    (0x90DB & 0xFF00)
                    != ((0x90DB + delay_index) & 0xFF00)
                )
            else:
                # The fourth pair advances the compressed stream.  Admit only
                # the overwhelmingly common non-wrapping pointer path; $xxFF
                # and command-boundary work stays literal.
                if pointer_low == 0xFF:
                    break
                next_pointer_low = (pointer_low + 1) & 0xFF
                pointer = next_pointer_low | (pointer_high << 8)
                if pointer < 0x8000:
                    break
                source = source_byte >> 1
                p = (
                    (p & 0x7C)
                    | 0x20
                    | (source_byte & 1)
                    | (0x02 if source == 0 else 0)
                )
                source2 = source >> 1
                p = (
                    (p & 0x7C)
                    | 0x20
                    | (source & 1)
                    | (0x02 if source2 == 0 else 0)
                )
                # DEC $07C0 / BEQ then INC $FE / BNE $9018.
                p = (p & 0x7D) | 0x22
                pointer_low = next_pointer_low
                p = (
                    (p & 0x7D)
                    | 0x20
                    | (0x02 if pointer_low == 0 else 0)
                    | (pointer_low & 0x80)
                )
                delay_counter = (delay_counter + 3) & 0xFF
                set_zn(delay_counter)
                source_byte = rom_byte(pointer)
                sample_pairs = 4
                # Refill path and common tail both consume 67 cycles before
                # entering the sample-level integrator.
                sample_cycles = 67

            # $9063-$908C/$90B5-$90C0: select and integrate the next level.
            a = source_byte & 0x03
            set_zn(a)
            set_zn(previous_level)
            difference = (previous_level - 0x40) & 0xFF
            p = (
                (p & 0x7C)
                | 0x20
                | int(previous_level >= 0x40)
                | (0x02 if difference == 0 else 0)
                | (difference & 0x80)
            )
            if previous_level >= 0x40:
                a |= 0x04
                set_zn(a)
            a |= delay_value
            set_zn(a)
            sample_index = a
            set_zn(sample_index)
            table_address = (0x911A + sample_index) & 0xFFFF
            delta = rom_byte(table_address)
            set_zn(delta)
            sample_cycles += 27 + int(
                (0x911A & 0xFF00) != (table_address & 0xFF00)
            )

            p &= 0xFE
            accumulator = delta
            total = accumulator + previous_level
            result = total & 0xFF
            p = (
                (p & 0x3C)
                | 0x20
                | int(total > 0xFF)
                | (0x02 if result == 0 else 0)
                | (
                    0x40
                    if (
                        ~(accumulator ^ previous_level)
                        & (accumulator ^ result)
                    ) & 0x80
                    else 0
                )
                | (result & 0x80)
            )
            next_level = result
            if delta & 0x80:
                sample_cycles += 23
                if total <= 0xFF:
                    next_level = 0
                    set_zn(next_level)
            else:
                sample_cycles += 24
                if total > 0xFF:
                    sample_cycles += 3
                    next_level = 0xFF
                    set_zn(next_level)

            output_level = next_level >> 1
            p = (
                (p & 0x7C)
                | 0x20
                | (next_level & 1)
                | (0x02 if output_level == 0 else 0)
            )
            # Keep a full instruction of headroom at frame/NMI boundaries.
            if span_cycles + sample_cycles + 7 > max_cycles:
                raise AssertionError("PCM period exceeded reserved deadline")

            span_cycles += sample_cycles
            events.append((span_cycles - 1, output_level))
            previous_level = next_level
            final_a = output_level
            final_y = sample_index

        if not events:
            return None

        ram[0x00FE] = pointer_low
        ram[0x00FF] = pointer_high
        ram[0x07C2] = source_byte
        ram[0x07C0] = sample_pairs
        ram[0x07C6] = delay_value
        ram[0x07FF] = delay_counter
        ram[0x07C4] = previous_level
        self.a = final_a
        self.y = final_y
        self.p = p
        self.pc = 0x908D
        self.last_opcode = 0x8D
        self.total_cycles += span_cycles
        self._last_batch_instruction_cycles = 4
        return span_cycles, tuple(events)

    def _dynamic_batch_start_is_safe(self) -> bool:
        """Admit a resolved indirect access that cannot reach a device.

        Static classification cannot know the zero-page pointer used by
        ``(d),Y`` and ``(d,X)`` instructions. Scene and audio decoders use
        these modes heavily for immutable PRG command streams, so resolve the
        pointer once at the scheduler boundary and keep the instruction in
        the CPU-only batch when both its real and dummy accesses stay wholly
        inside CPU RAM or cartridge ROM.
        """
        pc = self.pc
        if pc < 0x8000:
            return False
        nrom_prg = self._nrom_prg
        if nrom_prg is not None:
            prg = nrom_prg
            mask = self._nrom_prg_mask
            base = 0x8000
        elif self._segmented_batch_tables is not None:
            banks = self._active_mapped_prg_banks
            slot = (pc - 0x8000) >> 13
            if banks is None or not 0 <= slot < 4:
                return False
            prg = banks[slot]
            mask = 0x1FFF
            base = 0x8000 + slot * 0x2000
        else:
            prg = getattr(self.bus, "_mapped_prg_window", None)
            if prg is None:
                return False
            mask = 0x7FFF
            base = 0x8000

        opcode = prg[(pc - base) & mask]
        if not _DYNAMIC_INDIRECT_BATCH_OPCODES[opcode]:
            return False
        # Only the sixteen official indirect data opcodes need the pointer
        # and DMC safety work below. Most failed scheduler admissions are a
        # timed-device instruction; reject those after one immutable byte
        # lookup instead of repeatedly inspecting APU and RAM objects.
        apu = getattr(self.bus, "apu", None)
        if (
            apu is not None
            and (
                apu.cpu_stall_cycles
                or apu.dmc.bytes_remaining
                or apu.dmc.sample_buffer is not None
            )
        ):
            # A DMC fetch can halt an indirect read at a cycle inside the
            # instruction. Keep that uncommon overlap on the literal timed
            # path; otherwise a broad CPU-only span could classify the fetch
            # against the following RAM write instead.
            return False
        ram = getattr(self.bus, "ram", None)
        if ram is None:
            return False
        instruction = OPCODES[opcode]
        operand = prg[(pc + 1 - base) & mask]
        if instruction.mode == M.INDEXED_INDIRECT:
            pointer = (operand + self.x) & 0xFF
            low = ram[pointer]
            high = ram[(pointer + 1) & 0xFF]
            address = low | (high << 8)
            dummy_address = address
        else:
            low = ram[operand]
            high = ram[(operand + 1) & 0xFF]
            base_address = low | (high << 8)
            address = (base_address + self.y) & 0xFFFF
            dummy_address = (
                (base_address & 0xFF00)
                | (address & 0x00FF)
            )

        if instruction.name in _MEMORY_WRITE_OPERATIONS:
            return address < 0x2000 and dummy_address < 0x2000
        return (
            (
                address < 0x2000
                and dummy_address < 0x2000
            )
            or (
                address >= 0x8000
                and dummy_address >= 0x8000
            )
        )

    def batch_deferred_ppu_stream(
        self,
        max_cycles: int,
    ) -> tuple[int, bytearray, int] | None:
        """Join forced-blank ``$2007`` stores with adjacent safe CPU work.

        The Console has already established that no device event occurs
        inside ``max_cycles`` and that MMC2 PPUDATA writes can be timestamped
        without entering the PPU/APU steppers. This wrapper reuses the normal
        conservative CPU-only batch between literal STA/STX/STY stores,
        avoiding one scheduler dispatch for every byte in an unrolled
        nametable decompressor. Values are returned as one compact bytearray
        so the PPU can apply the complete stream with one cache invalidation.
        """
        self._last_batch_instruction_cycles = 0
        if (
            max_cycles < 5
            or self.halted
            or self.nmi_pending
            or self._irq_sampled
            or self._irq_inhibit
            != bool(self.p & int(F.INTERRUPT))
            or (
                not self._irq_inhibit
                and self.bus.irq_pending
            )
        ):
            return None

        bus = self.bus
        prg = getattr(bus, "_mapped_prg_window", None)
        if prg is None:
            return None
        cycles = 0
        last_instruction_cycles = 0
        write_values = bytearray()
        last_write_offset = 0

        while cycles + 4 < max_cycles:
            pc = self.pc
            if not 0x8000 <= pc <= 0xFFFD:
                break
            prg_offset = pc - 0x8000
            opcode = prg[prg_offset]
            if opcode == 0x8C:
                register = "y"
            elif opcode == 0x8D:
                register = "a"
            elif opcode == 0x8E:
                register = "x"
            else:
                register = None
            if (
                register is not None
                and prg[prg_offset + 1] == 0x07
                and prg[prg_offset + 2] == 0x20
            ):
                value = getattr(self, register)
                write_values.append(value)
                last_write_offset = cycles + 3
                bus.open_bus = value  # type: ignore[attr-defined]
                self.pc = (pc + 3) & 0xFFFF
                self.last_opcode = opcode
                self.total_cycles += 4
                cycles += 4
                last_instruction_cycles = 4
                continue

            starts = self._safe_batch_starts
            if starts is None or not starts[pc]:
                break

            # Keep the decompressor's overwhelmingly common ALU/index/branch
            # path in this loop. Falling back through batch_safe_instructions
            # for each byte would still perform one Python scheduler-style
            # setup around every PPUDATA store.
            if opcode in (0x88, 0xC8, 0xCA, 0xE8):
                if opcode == 0x88:
                    register, delta = "y", -1
                elif opcode == 0xC8:
                    register, delta = "y", 1
                elif opcode == 0xCA:
                    register, delta = "x", -1
                else:
                    register, delta = "x", 1
                result = (getattr(self, register) + delta) & 0xFF
                setattr(self, register, result)
                self.p = (
                    (self.p & 0x7D)
                    | 0x20
                    | (0x02 if result == 0 else 0)
                    | (result & 0x80)
                )
                bus.open_bus = prg[prg_offset + 1]  # type: ignore[attr-defined]
                self.pc = (pc + 1) & 0xFFFF
                self.last_opcode = opcode
                self.total_cycles += 2
                cycles += 2
                last_instruction_cycles = 2
                continue

            if opcode in (0x8A, 0x98, 0xAA, 0xA8):
                if opcode == 0x8A:
                    source, target = "x", "a"
                elif opcode == 0x98:
                    source, target = "y", "a"
                elif opcode == 0xAA:
                    source, target = "a", "x"
                else:
                    source, target = "a", "y"
                result = getattr(self, source)
                setattr(self, target, result)
                self.p = (
                    (self.p & 0x7D)
                    | 0x20
                    | (0x02 if result == 0 else 0)
                    | (result & 0x80)
                )
                bus.open_bus = prg[prg_offset + 1]  # type: ignore[attr-defined]
                self.pc = (pc + 1) & 0xFFFF
                self.last_opcode = opcode
                self.total_cycles += 2
                cycles += 2
                last_instruction_cycles = 2
                continue

            if opcode in (
                0x10, 0x30, 0x50, 0x70,
                0x90, 0xB0, 0xD0, 0xF0,
            ):
                relative = prg[prg_offset + 1]
                bus.open_bus = relative  # type: ignore[attr-defined]
                old_pc = (pc + 2) & 0xFFFF
                new_pc = old_pc
                instruction_cycles = 2
                if opcode == 0x10:
                    taken = not (self.p & 0x80)
                elif opcode == 0x30:
                    taken = bool(self.p & 0x80)
                elif opcode == 0x50:
                    taken = not (self.p & 0x40)
                elif opcode == 0x70:
                    taken = bool(self.p & 0x40)
                elif opcode == 0x90:
                    taken = not (self.p & 0x01)
                elif opcode == 0xB0:
                    taken = bool(self.p & 0x01)
                elif opcode == 0xD0:
                    taken = not (self.p & 0x02)
                else:
                    taken = bool(self.p & 0x02)
                if taken:
                    offset = (
                        relative - 0x100
                        if relative & 0x80
                        else relative
                    )
                    new_pc = (old_pc + offset) & 0xFFFF
                    instruction_cycles = 3 + int(
                        (old_pc & 0xFF00) != (new_pc & 0xFF00)
                    )
                self.pc = new_pc
                self.last_opcode = opcode
                self.total_cycles += instruction_cycles
                cycles += instruction_cycles
                last_instruction_cycles = instruction_cycles
                continue

            if opcode in (0x18, 0x38, 0xEA):
                bus.open_bus = prg[prg_offset + 1]  # type: ignore[attr-defined]
                if opcode == 0x18:
                    self.p = (self.p & 0xFE) | 0x20
                elif opcode == 0x38:
                    self.p |= 0x21
                self.pc = (pc + 1) & 0xFFFF
                self.last_opcode = opcode
                self.total_cycles += 2
                cycles += 2
                last_instruction_cycles = 2
                continue

            if opcode in (0x69, 0xE9):
                value = prg[prg_offset + 1]
                bus.open_bus = value  # type: ignore[attr-defined]
                if opcode == 0xE9:
                    value ^= 0xFF
                accumulator = self.a
                total = accumulator + value + (self.p & 0x01)
                result = total & 0xFF
                self.p = (
                    (self.p & 0x3C)
                    | 0x20
                    | int(total > 0xFF)
                    | (0x02 if result == 0 else 0)
                    | (
                        0x40
                        if (
                            ~(accumulator ^ value)
                            & (accumulator ^ result)
                        ) & 0x80
                        else 0
                    )
                    | (result & 0x80)
                )
                self.a = result
                self.pc = (pc + 2) & 0xFFFF
                self.last_opcode = opcode
                self.total_cycles += 2
                cycles += 2
                last_instruction_cycles = 2
                continue

            safe_cycles = self.batch_safe_instructions(max_cycles - cycles)
            if safe_cycles <= 0:
                break
            cycles += safe_cycles
            last_instruction_cycles = self._last_batch_instruction_cycles
            if cycles >= max_cycles:
                break

        self._last_batch_instruction_cycles = last_instruction_cycles
        if cycles:
            self.deferred_ppu_stream_batches += 1
            return cycles, write_values, last_write_offset
        return None

    def batch_counter_loop(self, max_cycles: int) -> int:
        """Collapse safe ``INC/DEC register; BNE`` delay-loop iterations.

        Only taken iterations entirely inside immutable cartridge ROM are
        batched. The zero-producing final iteration remains on the literal
        instruction path, and the scheduler bounds ``max_cycles`` before every
        PPU/APU/DMC/IRQ-visible event. This preserves instruction-boundary
        interrupt sampling while avoiding hundreds of identical Python calls
        in a common 6502 delay-loop shape.
        """
        self._last_batch_instruction_cycles = 0
        if (
            max_cycles < 5
            or self.pc < 0x8000
            or self.halted
            or self.nmi_pending
            or self._irq_sampled
            or (
                not self._irq_inhibit
                and self.bus.irq_pending
            )
        ):
            return 0
        pc = self.pc
        known_loops = self._counter_loop_opcodes
        if known_loops is not None:
            opcode = known_loops[pc]
            if opcode == 0:
                return 0
        else:
            peek = self._code_peek
            if peek is None:
                return 0
            opcode = peek(pc)
            if (
                opcode not in (0x88, 0xC8, 0xCA, 0xE8)
                or peek((pc + 1) & 0xFFFF) != 0xD0
                or peek((pc + 2) & 0xFFFF) != 0xFD
            ):
                return 0

        if opcode == 0xE8:
            register, delta = "x", 1
        elif opcode == 0xCA:
            register, delta = "x", -1
        elif opcode == 0xC8:
            register, delta = "y", 1
        else:
            register, delta = "y", -1

        value = getattr(self, register)
        iterations_to_zero = (
            256 - value
            if delta > 0
            else value if value else 256
        )
        branch_base = (pc + 3) & 0xFFFF
        cycles_per_iteration = 5 + int(
            (branch_base & 0xFF00) != (pc & 0xFF00)
        )
        repeats = min(
            iterations_to_zero - 1,
            max_cycles // cycles_per_iteration,
        )
        if repeats <= 0:
            return 0

        # Establish the same final instruction-stream/open-bus value as the
        # literal BNE iteration. PRG reads are immutable and side-effect-free.
        read = self._code_read
        read(pc)
        read((pc + 1) & 0xFFFF)
        read((pc + 2) & 0xFFFF)
        setattr(
            self,
            register,
            self._set_zn((value + delta * repeats) & 0xFF),
        )
        self.last_opcode = 0xD0
        self._last_batch_instruction_cycles = (
            cycles_per_iteration - 2
        )
        cycles = repeats * cycles_per_iteration
        self.total_cycles += cycles
        return cycles

    def batch_poll_loop(self, max_cycles: int) -> int:
        """Collapse repeated side-effect-free RAM polling iterations.

        NMI-driven games frequently finish their frame and wait in a short
        ``load/compare/bit RAM; branch back`` loop. Internal RAM cannot change
        until an interrupt handler runs, and the console scheduler bounds this
        batch before every interrupt-visible PPU/APU event. The final CPU
        registers, flags, PC, open bus, cycles, and instruction boundary are
        therefore identical to literal execution.
        """
        self._last_batch_instruction_cycles = 0
        starts = self._poll_loop_starts
        pc = self.pc
        poll_kind = 0 if starts is None else starts[pc]
        if (
            max_cycles < 6
            or starts is None
            or pc < 0x8000
            or not poll_kind
            or self.halted
            or self.nmi_pending
            or self._irq_sampled
            or self._irq_inhibit
            != bool(self.p & int(F.INTERRUPT))
            or (
                not self._irq_inhibit
                and self.bus.irq_pending
            )
        ):
            return 0

        nrom_prg = self._nrom_prg
        ram = getattr(self.bus, "ram", None)
        if nrom_prg is not None:
            prg = nrom_prg
            mask = self._nrom_prg_mask
            def rom_byte(address: int) -> int:
                return prg[(address - 0x8000) & mask]
        elif self._segmented_batch_tables is not None:
            banks = self._active_mapped_prg_banks
            if banks is None:
                return 0

            def rom_byte(address: int) -> int:
                offset = address - 0x8000
                return banks[offset >> 13][offset & 0x1FFF]
        else:
            prg = getattr(self.bus, "_mapped_prg_window", None)
            if prg is None:
                return 0
            mask = 0x7FFF

            def rom_byte(address: int) -> int:
                return prg[(address - 0x8000) & mask]

        if ram is None:
            return 0

        if poll_kind == _COOPERATIVE_POLL_KIND:
            # The immutable classifier has already validated both branches
            # and the surrounding setup block. Enter only at that setup's
            # canonical scan state; a mid-loop state remains literal.
            if self.x != 0 or self.y != 4:
                return 0
            status_base = rom_byte(pc + 1)
            threshold = rom_byte(pc + 3)
            status_values = (
                ram[status_base],
                ram[(status_base + 4) & 0xFF],
                ram[(status_base + 8) & 0xFF],
                ram[(status_base + 12) & 0xFF],
            )
            # A ready subsystem takes the BCS work path and may mutate any
            # part of the machine, so only the invariant idle case collapses.
            if any(value >= threshold for value in status_values):
                return 0

            # Four scans consume 83 cycles; JMP setup, LDX, STX, and LDY add
            # ten. Keep a partial final scan on the ordinary CPU path.
            repeats = max_cycles // 93
            if repeats <= 0:
                return 0
            reset_address = rom_byte(pc - 3)
            ram[reset_address] = 0
            self.a = status_values[-1]
            self.x = 0
            self.y = 4
            # The final LDY #4 clears N/Z. The last unsuccessful CMP left C
            # clear; every other status bit survives the literal sequence.
            self.p = (self.p & 0x7C) | 0x20
            self.last_opcode = 0xA0
            self._last_batch_instruction_cycles = 2
            self.bus.open_bus = 0x04  # type: ignore[attr-defined]
            cycles = repeats * 93
            self.total_cycles += cycles
            return cycles

        opcode = rom_byte(pc)
        # The opcode values do not sort perfectly by addressing mode.
        if opcode in (0xA4, 0xA5, 0xA6, 0x24, 0xC4, 0xC5, 0xE4):
            instruction_length = 2
            read_address = rom_byte(pc + 1)
            instruction_cycles = 3
        else:
            instruction_length = 3
            read_address = rom_byte(pc + 1) | (rom_byte(pc + 2) << 8)
            instruction_cycles = 4
        value = ram[read_address & 0x07FF]

        old_p = self.p
        if opcode in (0xA5, 0xAD):  # LDA
            new_p = (
                (old_p & 0x7D)
                | 0x20
                | (0x02 if value == 0 else 0)
                | (value & 0x80)
            )
            register = "a"
        elif opcode in (0xA6, 0xAE):  # LDX
            new_p = (
                (old_p & 0x7D)
                | 0x20
                | (0x02 if value == 0 else 0)
                | (value & 0x80)
            )
            register = "x"
        elif opcode in (0xA4, 0xAC):  # LDY
            new_p = (
                (old_p & 0x7D)
                | 0x20
                | (0x02 if value == 0 else 0)
                | (value & 0x80)
            )
            register = "y"
        elif opcode in (0x24, 0x2C):  # BIT
            new_p = (
                (old_p & 0x3D)
                | 0x20
                | (0x02 if (self.a & value) == 0 else 0)
                | (value & 0xC0)
            )
            register = ""
        else:
            if opcode in (0xC5, 0xCD):
                compared = self.a
            elif opcode in (0xE4, 0xEC):
                compared = self.x
            else:
                compared = self.y
            difference = (compared - value) & 0xFF
            new_p = (
                (old_p & 0x7C)
                | 0x20
                | int(compared >= value)
                | (0x02 if difference == 0 else 0)
                | (difference & 0x80)
            )
            register = ""

        branch_address = pc + instruction_length
        branch_opcode = rom_byte(branch_address)
        branch_taken = {
            0x10: not (new_p & 0x80),
            0x30: bool(new_p & 0x80),
            0x50: not (new_p & 0x40),
            0x70: bool(new_p & 0x40),
            0x90: not (new_p & 0x01),
            0xB0: bool(new_p & 0x01),
            0xD0: not (new_p & 0x02),
            0xF0: bool(new_p & 0x02),
        }[branch_opcode]
        if not branch_taken:
            return 0

        branch_base = (branch_address + 2) & 0xFFFF
        branch_cycles = 3 + int(
            (branch_base & 0xFF00) != (pc & 0xFF00)
        )
        cycles_per_iteration = instruction_cycles + branch_cycles
        repeats = max_cycles // cycles_per_iteration
        if repeats <= 0:
            return 0

        if register:
            setattr(self, register, value)
        self.p = new_p
        self.last_opcode = branch_opcode
        self._last_batch_instruction_cycles = branch_cycles
        # The branch displacement is the last literal bus value of every
        # iteration. Keep the same CPU open-bus state without replaying reads.
        self.bus.open_bus = rom_byte(branch_address + 1)  # type: ignore[attr-defined]
        cycles = repeats * cycles_per_iteration
        self.total_cycles += cycles
        return cycles

    def _fast_immediate(self) -> int:
        pc = self.pc
        self.pc = (pc + 1) & 0xFFFF
        nrom_prg = self._nrom_prg
        if nrom_prg is not None and pc >= 0x8000:
            value = nrom_prg[
                (pc - 0x8000) & self._nrom_prg_mask
            ]
            self.bus.open_bus = value  # type: ignore[attr-defined]
            return value
        mapped_prg = (
            getattr(self.bus, "_mapped_prg_window", None)
            if self._mapped_prg_bus is not None
            else None
        )
        if mapped_prg is not None and pc >= 0x8000:
            value = mapped_prg[pc - 0x8000]
            self.bus.open_bus = value  # type: ignore[attr-defined]
            return value
        segmented_banks = self._active_mapped_prg_banks
        if (
            self._segmented_batch_tables is not None
            and segmented_banks is not None
            and pc >= 0x8000
        ):
            offset = pc - 0x8000
            value = segmented_banks[offset >> 13][offset & 0x1FFF]
            self.bus.open_bus = value  # type: ignore[attr-defined]
            return value
        return self._code_read(pc) & 0xFF

    def _fast_absolute_address(self) -> int:
        pc = self.pc
        nrom_prg = self._nrom_prg
        if nrom_prg is not None and 0x8000 <= pc < 0xFFFF:
            mask = self._nrom_prg_mask
            low = nrom_prg[(pc - 0x8000) & mask]
            high = nrom_prg[(pc + 1 - 0x8000) & mask]
            self.bus.open_bus = high  # type: ignore[attr-defined]
        else:
            mapped_prg = (
                getattr(self.bus, "_mapped_prg_window", None)
                if self._mapped_prg_bus is not None
                else None
            )
            if mapped_prg is not None and 0x8000 <= pc < 0xFFFF:
                low = mapped_prg[pc - 0x8000]
                high = mapped_prg[pc + 1 - 0x8000]
                self.bus.open_bus = high  # type: ignore[attr-defined]
            elif (
                self._segmented_batch_tables is not None
                and self._active_mapped_prg_banks is not None
                and 0x8000 <= pc < 0xFFFF
            ):
                banks = self._active_mapped_prg_banks
                offset = pc - 0x8000
                low = banks[offset >> 13][offset & 0x1FFF]
                offset += 1
                high = banks[offset >> 13][offset & 0x1FFF]
                self.bus.open_bus = high  # type: ignore[attr-defined]
            else:
                read = self._code_read
                low = read(pc)
                high = read((pc + 1) & 0xFFFF)
        self.pc = (pc + 2) & 0xFFFF
        return (low | (high << 8)) & 0xFFFF

    def _fast_branch(self, condition: bool) -> int:
        value = self._fast_immediate()
        if not condition:
            return 2
        old_pc = self.pc
        offset = value - 0x100 if value & 0x80 else value
        self.pc = (old_pc + offset) & 0xFFFF
        return 3 + int((old_pc & 0xFF00) != (self.pc & 0xFF00))

    def _op_lda_immediate(self) -> int:
        self.a = self._set_zn(self._fast_immediate())
        return 2

    def _op_ldx_immediate(self) -> int:
        self.x = self._set_zn(self._fast_immediate())
        return 2

    def _op_ldy_immediate(self) -> int:
        self.y = self._set_zn(self._fast_immediate())
        return 2

    def _op_lda_zero_page(self) -> int:
        self.a = self._set_zn(self._bus_read(self._fast_immediate()) & 0xFF)
        return 3

    def _op_lda_zero_page_x(self) -> int:
        base = self._fast_immediate()
        self._bus_read(base)
        address = (base + self.x) & 0xFF
        self.a = self._set_zn(self._bus_read(address) & 0xFF)
        return 4

    def _op_lda_absolute(self) -> int:
        self.a = self._set_zn(
            self._bus_read(self._fast_absolute_address()) & 0xFF
        )
        return 4

    def _op_lda_absolute_x(self) -> int:
        base = self._fast_absolute_address()
        address = (base + self.x) & 0xFFFF
        crossed = (base & 0xFF00) != (address & 0xFF00)
        if crossed:
            self._bus_read((base & 0xFF00) | (address & 0x00FF))
        self.a = self._set_zn(self._bus_read(address) & 0xFF)
        return 4 + int(crossed)

    def _op_lda_absolute_y(self) -> int:
        base = self._fast_absolute_address()
        address = (base + self.y) & 0xFFFF
        crossed = (base & 0xFF00) != (address & 0xFF00)
        if crossed:
            self._bus_read((base & 0xFF00) | (address & 0x00FF))
        self.a = self._set_zn(self._bus_read(address) & 0xFF)
        return 4 + int(crossed)

    def _op_sta_zero_page(self) -> int:
        self._bus_write(self._fast_immediate(), self.a)
        return 3

    def _op_sta_zero_page_x(self) -> int:
        base = self._fast_immediate()
        self._bus_read(base)
        self._bus_write((base + self.x) & 0xFF, self.a)
        return 4

    def _op_sta_absolute(self) -> int:
        self._bus_write(self._fast_absolute_address(), self.a)
        return 4

    def _op_sta_absolute_x(self) -> int:
        base = self._fast_absolute_address()
        address = (base + self.x) & 0xFFFF
        self._bus_read((base & 0xFF00) | (address & 0x00FF))
        self._bus_write(address, self.a)
        return 5

    def _op_sta_absolute_y(self) -> int:
        base = self._fast_absolute_address()
        address = (base + self.y) & 0xFFFF
        self._bus_read((base & 0xFF00) | (address & 0x00FF))
        self._bus_write(address, self.a)
        return 5

    def _op_inx(self) -> int:
        self.x = self._set_zn(self.x + 1)
        return 2

    def _op_iny(self) -> int:
        self.y = self._set_zn(self.y + 1)
        return 2

    def _op_dex(self) -> int:
        self.x = self._set_zn(self.x - 1)
        return 2

    def _op_dey(self) -> int:
        self.y = self._set_zn(self.y - 1)
        return 2

    def _op_bcc(self) -> int:
        return self._fast_branch(not (self.p & 0x01))

    def _op_bcs(self) -> int:
        return self._fast_branch(bool(self.p & 0x01))

    def _op_beq(self) -> int:
        return self._fast_branch(bool(self.p & 0x02))

    def _op_bmi(self) -> int:
        return self._fast_branch(bool(self.p & 0x80))

    def _op_bne(self) -> int:
        return self._fast_branch(not (self.p & 0x02))

    def _op_bpl(self) -> int:
        return self._fast_branch(not (self.p & 0x80))

    def _op_bvc(self) -> int:
        return self._fast_branch(not (self.p & 0x40))

    def _op_bvs(self) -> int:
        return self._fast_branch(bool(self.p & 0x40))

    def _op_and_immediate(self) -> int:
        self.a = self._set_zn(self.a & self._fast_immediate())
        return 2

    def _op_eor_immediate(self) -> int:
        self.a = self._set_zn(self.a ^ self._fast_immediate())
        return 2

    def _op_ora_immediate(self) -> int:
        self.a = self._set_zn(self.a | self._fast_immediate())
        return 2

    def _op_cmp_immediate(self) -> int:
        value = self._fast_immediate()
        result = (self.a - value) & 0xFF
        self.p = (
            (self.p & 0x7C)
            | 0x20
            | int(self.a >= value)
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
        return 2

    def _op_adc_immediate(self) -> int:
        self._adc(self._fast_immediate())
        return 2

    def _op_sbc_immediate(self) -> int:
        self._adc(self._fast_immediate() ^ 0xFF)
        return 2

    def _op_clc(self) -> int:
        self.p = (self.p & 0xFE) | 0x20
        return 2

    def _op_sec(self) -> int:
        self.p |= 0x21
        return 2

    def _op_cli(self) -> int:
        self.p = (self.p & 0xFB) | 0x20
        return 2

    def _op_sei(self) -> int:
        self.p |= 0x24
        return 2

    def _value(self, address: int | None) -> int:
        if address is None:
            raise AssertionError("instruction requires an operand")
        return self._bus_read(address & 0xFFFF) & 0xFF

    def _write_result(self, mode: M, address: int | None, value: int) -> None:
        value &= 0xFF
        if mode == M.ACCUMULATOR:
            self.a = value
        elif address is not None:
            self._bus_write(address & 0xFFFF, value)
        else:
            raise AssertionError("instruction requires a destination")

    def _adc(self, value: int) -> None:
        accumulator = self.a
        total = accumulator + value + (self.p & 0x01)
        result = total & 0xFF
        self.p = (
            (self.p & 0x3C)
            | 0x20
            | int(total > 0xFF)
            | (0x02 if result == 0 else 0)
            | (
                0x40
                if (~(accumulator ^ value) & (accumulator ^ result)) & 0x80
                else 0
            )
            | (result & 0x80)
        )
        self.a = result

    def _compare(self, register: int, value: int) -> None:
        result = (register - value) & 0xFF
        self.p = (
            (self.p & 0x7C)
            | 0x20
            | int(register >= value)
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )

    def _asl(self, value: int) -> int:
        result = (value << 1) & 0xFF
        self.p = (
            (self.p & 0x7C)
            | 0x20
            | int(bool(value & 0x80))
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
        return result

    def _lsr(self, value: int) -> int:
        result = value >> 1
        self.p = (
            (self.p & 0x7C)
            | 0x20
            | (value & 1)
            | (0x02 if result == 0 else 0)
        )
        return result

    def _rol(self, value: int) -> int:
        result = ((value << 1) | (self.p & 1)) & 0xFF
        self.p = (
            (self.p & 0x7C)
            | 0x20
            | int(bool(value & 0x80))
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
        return result

    def _ror(self, value: int) -> int:
        result = (value >> 1) | ((self.p & 1) << 7)
        self.p = (
            (self.p & 0x7C)
            | 0x20
            | (value & 1)
            | (0x02 if result == 0 else 0)
            | (result & 0x80)
        )
        return result

    def _branch(self, condition: bool, offset: int | None) -> int:
        if not condition:
            return 0
        assert offset is not None
        old_pc = self.pc
        self.pc = (self.pc + offset) & 0xFFFF
        return 1 + int((old_pc & 0xFF00) != (self.pc & 0xFF00))

    def _execute(self, name: str, mode: M, address: int | None) -> int:
        if name == "LDA":
            self.a = self._set_zn(self._value(address))
        elif name == "LDX":
            self.x = self._set_zn(self._value(address))
        elif name == "LDY":
            self.y = self._set_zn(self._value(address))
        elif name == "STA":
            self.write(address, self.a)  # type: ignore[arg-type]
        elif name == "STX":
            self.write(address, self.x)  # type: ignore[arg-type]
        elif name == "STY":
            self.write(address, self.y)  # type: ignore[arg-type]
        elif name == "TAX":
            self.x = self._set_zn(self.a)
        elif name == "TAY":
            self.y = self._set_zn(self.a)
        elif name == "TSX":
            self.x = self._set_zn(self.sp)
        elif name == "TXA":
            self.a = self._set_zn(self.x)
        elif name == "TXS":
            self.sp = self.x
        elif name == "TYA":
            self.a = self._set_zn(self.y)
        elif name == "PHA":
            self._push(self.a)
        elif name == "PHP":
            self._push(self.p | int(F.BREAK | F.UNUSED))
        elif name == "PLA":
            self.a = self._set_zn(self._pull())
        elif name == "PLP":
            self.p = (self._pull() & ~int(F.BREAK)) | int(F.UNUSED)
        elif name == "AND":
            self.a = self._set_zn(self.a & self._value(address))
        elif name == "EOR":
            self.a = self._set_zn(self.a ^ self._value(address))
        elif name == "ORA":
            self.a = self._set_zn(self.a | self._value(address))
        elif name == "BIT":
            value = self._value(address)
            self._set_flag(F.ZERO, (self.a & value) == 0)
            self._set_flag(F.OVERFLOW, bool(value & 0x40))
            self._set_flag(F.NEGATIVE, bool(value & 0x80))
        elif name == "ADC":
            self._adc(self._value(address))
        elif name == "SBC":
            self._adc(self._value(address) ^ 0xFF)
        elif name == "CMP":
            self._compare(self.a, self._value(address))
        elif name == "CPX":
            self._compare(self.x, self._value(address))
        elif name == "CPY":
            self._compare(self.y, self._value(address))
        elif name == "ASL":
            value = self.a if mode == M.ACCUMULATOR else self._value(address)
            if mode != M.ACCUMULATOR:
                self.write(address, value)  # type: ignore[arg-type]
            self._write_result(mode, address, self._asl(value))
        elif name == "LSR":
            value = self.a if mode == M.ACCUMULATOR else self._value(address)
            if mode != M.ACCUMULATOR:
                self.write(address, value)  # type: ignore[arg-type]
            self._write_result(mode, address, self._lsr(value))
        elif name == "ROL":
            value = self.a if mode == M.ACCUMULATOR else self._value(address)
            if mode != M.ACCUMULATOR:
                self.write(address, value)  # type: ignore[arg-type]
            self._write_result(mode, address, self._rol(value))
        elif name == "ROR":
            value = self.a if mode == M.ACCUMULATOR else self._value(address)
            if mode != M.ACCUMULATOR:
                self.write(address, value)  # type: ignore[arg-type]
            self._write_result(mode, address, self._ror(value))
        elif name == "INC":
            value = self._value(address)
            self.write(address, value)  # type: ignore[arg-type]
            self.write(address, self._set_zn(value + 1))  # type: ignore[arg-type]
        elif name == "DEC":
            value = self._value(address)
            self.write(address, value)  # type: ignore[arg-type]
            self.write(address, self._set_zn(value - 1))  # type: ignore[arg-type]
        elif name == "INX":
            self.x = self._set_zn(self.x + 1)
        elif name == "INY":
            self.y = self._set_zn(self.y + 1)
        elif name == "DEX":
            self.x = self._set_zn(self.x - 1)
        elif name == "DEY":
            self.y = self._set_zn(self.y - 1)
        elif name == "JMP":
            self.pc = int(address)
        elif name == "JSR":
            self._push16((self.pc - 1) & 0xFFFF)
            self.pc = int(address)
        elif name == "RTS":
            self.pc = (self._pull16() + 1) & 0xFFFF
        elif name == "RTI":
            self.p = (self._pull() & ~int(F.BREAK)) | int(F.UNUSED)
            self.pc = self._pull16()
        elif name == "BRK":
            self.pc = (self.pc + 1) & 0xFFFF
            self._push16(self.pc)
            self._push(self.p | int(F.BREAK | F.UNUSED))
            # BRK's explicit opcode fetch is already cycle one of its
            # seven-cycle sequence; vector priority is fixed one scheduler
            # offset earlier than hardware IRQ entry.
            vector = self._select_interrupt_vector(0xFFFE, 3)
            self._set_flag(F.INTERRUPT, True)
            self.pc = self._read16(vector)
        elif name == "BCC":
            return self._branch(not self._flag(F.CARRY), address)
        elif name == "BCS":
            return self._branch(self._flag(F.CARRY), address)
        elif name == "BEQ":
            return self._branch(self._flag(F.ZERO), address)
        elif name == "BMI":
            return self._branch(self._flag(F.NEGATIVE), address)
        elif name == "BNE":
            return self._branch(not self._flag(F.ZERO), address)
        elif name == "BPL":
            return self._branch(not self._flag(F.NEGATIVE), address)
        elif name == "BVC":
            return self._branch(not self._flag(F.OVERFLOW), address)
        elif name == "BVS":
            return self._branch(self._flag(F.OVERFLOW), address)
        elif name == "CLC":
            self._set_flag(F.CARRY, False)
        elif name == "CLD":
            self._set_flag(F.DECIMAL, False)
        elif name == "CLI":
            self._set_flag(F.INTERRUPT, False)
        elif name == "CLV":
            self._set_flag(F.OVERFLOW, False)
        elif name == "SEC":
            self._set_flag(F.CARRY, True)
        elif name == "SED":
            self._set_flag(F.DECIMAL, True)
        elif name == "SEI":
            self._set_flag(F.INTERRUPT, True)
        elif name == "NOP":
            if address is not None and mode not in (M.RELATIVE,):
                self.read(address)
        elif name == "KIL":
            self.halted = True
        elif name == "LAX":
            value = self._set_zn(self._value(address))
            self.a = self.x = value
        elif name == "SAX":
            self.write(address, self.a & self.x)  # type: ignore[arg-type]
        elif name == "SLO":
            original = self._value(address)
            self.write(address, original)  # type: ignore[arg-type]
            value = self._asl(original)
            self.write(address, value)  # type: ignore[arg-type]
            self.a = self._set_zn(self.a | value)
        elif name == "RLA":
            original = self._value(address)
            self.write(address, original)  # type: ignore[arg-type]
            value = self._rol(original)
            self.write(address, value)  # type: ignore[arg-type]
            self.a = self._set_zn(self.a & value)
        elif name == "SRE":
            original = self._value(address)
            self.write(address, original)  # type: ignore[arg-type]
            value = self._lsr(original)
            self.write(address, value)  # type: ignore[arg-type]
            self.a = self._set_zn(self.a ^ value)
        elif name == "RRA":
            original = self._value(address)
            self.write(address, original)  # type: ignore[arg-type]
            value = self._ror(original)
            self.write(address, value)  # type: ignore[arg-type]
            self._adc(value)
        elif name == "DCP":
            original = self._value(address)
            self.write(address, original)  # type: ignore[arg-type]
            value = self._set_zn(original - 1)
            self.write(address, value)  # type: ignore[arg-type]
            self._compare(self.a, value)
        elif name == "ISC":
            original = self._value(address)
            self.write(address, original)  # type: ignore[arg-type]
            value = self._set_zn(original + 1)
            self.write(address, value)  # type: ignore[arg-type]
            self._adc(value ^ 0xFF)
        elif name == "ANC":
            self.a = self._set_zn(self.a & self._value(address))
            self._set_flag(F.CARRY, bool(self.a & 0x80))
        elif name == "ALR":
            self.a &= self._value(address)
            self.a = self._lsr(self.a)
        elif name == "ARR":
            self.a &= self._value(address)
            carry = 0x80 if self._flag(F.CARRY) else 0
            self.a = self._set_zn((self.a >> 1) | carry)
            self._set_flag(F.CARRY, bool(self.a & 0x40))
            self._set_flag(F.OVERFLOW, bool(((self.a >> 6) ^ (self.a >> 5)) & 1))
        elif name == "AXS":
            operand = self._value(address)
            source = self.a & self.x
            self._set_flag(F.CARRY, source >= operand)
            self.x = self._set_zn(source - operand)
        elif name == "LAS":
            value = self._set_zn(self._value(address) & self.sp)
            self.a = self.x = self.sp = value
        elif name == "XAA":
            self.a = self._set_zn(self.x & self._value(address))
        elif name in ("AHX", "SHX", "SHY", "TAS"):
            assert address is not None
            # These unstable NMOS stores mask against the *unindexed* high
            # address byte plus one. On a page crossing, that masked value
            # also leaks onto the address bus and replaces the destination's
            # high byte.
            index = self.x if mode == M.ABSOLUTE_X else self.y
            base = (address - index) & 0xFFFF
            high_plus_one = ((base >> 8) + 1) & 0xFF
            if name == "AHX":
                value = self.a & self.x & high_plus_one
            elif name == "SHX":
                value = self.x & high_plus_one
            elif name == "SHY":
                value = self.y & high_plus_one
            else:
                self.sp = self.a & self.x
                value = self.sp & high_plus_one
            if (base & 0xFF00) != (address & 0xFF00):
                address = (value << 8) | (address & 0xFF)
            self.write(address, value)
        else:
            raise AssertionError(f"unhandled opcode operation {name}")
        return 0

    def disassemble_at(self, address: int) -> tuple[str, int]:
        opcode = self.read(address)
        instruction = OPCODES[opcode]
        pc = (address + 1) & 0xFFFF
        lengths = {
            M.IMPLIED: 1, M.ACCUMULATOR: 1,
            M.IMMEDIATE: 2, M.ZERO_PAGE: 2, M.ZERO_PAGE_X: 2,
            M.ZERO_PAGE_Y: 2, M.RELATIVE: 2, M.INDEXED_INDIRECT: 2,
            M.INDIRECT_INDEXED: 2, M.ABSOLUTE: 3, M.ABSOLUTE_X: 3,
            M.ABSOLUTE_Y: 3, M.INDIRECT: 3,
        }
        operand = ""
        if lengths[instruction.mode] == 2:
            value = self.read(pc)
            forms = {
                M.IMMEDIATE: f"#$%02X" % value,
                M.ZERO_PAGE: f"$%02X" % value,
                M.ZERO_PAGE_X: f"$%02X,X" % value,
                M.ZERO_PAGE_Y: f"$%02X,Y" % value,
                M.INDEXED_INDIRECT: f"($%02X,X)" % value,
                M.INDIRECT_INDEXED: f"($%02X),Y" % value,
                M.RELATIVE: f"$%04X" % ((pc + 1 + (value - 256 if value & 0x80 else value)) & 0xFFFF),
            }
            operand = forms[instruction.mode]
        elif lengths[instruction.mode] == 3:
            value = self.read(pc) | (self.read((pc + 1) & 0xFFFF) << 8)
            forms = {
                M.ABSOLUTE: f"$%04X" % value,
                M.ABSOLUTE_X: f"$%04X,X" % value,
                M.ABSOLUTE_Y: f"$%04X,Y" % value,
                M.INDIRECT: f"($%04X)" % value,
            }
            operand = forms[instruction.mode]
        elif instruction.mode == M.ACCUMULATOR:
            operand = "A"
        marker = "*" if instruction.unofficial else " "
        return f"{address:04X}  {opcode:02X}  {marker}{instruction.name} {operand}".rstrip(), lengths[instruction.mode]

    def get_state(self) -> dict:
        return {
            "a": self.a,
            "x": self.x,
            "y": self.y,
            "sp": self.sp,
            "pc": self.pc,
            "p": self.p,
            "irq_inhibit": self._irq_inhibit,
            "total_cycles": self.total_cycles,
            "nmi_pending": self.nmi_pending,
            "nmi_delay": self._nmi_delay,
            "irq_sampled": self._irq_sampled,
            "halted": self.halted,
            "last_opcode": self.last_opcode,
        }

    def set_state(self, state: dict) -> None:
        self.a = int(state["a"]) & 0xFF
        self.x = int(state["x"]) & 0xFF
        self.y = int(state["y"]) & 0xFF
        self.sp = int(state["sp"]) & 0xFF
        self.pc = int(state["pc"]) & 0xFFFF
        self.p = (int(state["p"]) | int(F.UNUSED)) & 0xFF
        self._irq_inhibit = bool(
            state.get("irq_inhibit", self.p & int(F.INTERRUPT))
        )
        self.total_cycles = int(state["total_cycles"])
        self.nmi_pending = bool(state["nmi_pending"])
        self._nmi_delay = int(state.get("nmi_delay", 0))
        self._irq_sampled = bool(state.get("irq_sampled", False))
        self.halted = bool(state["halted"])
        self.last_opcode = int(state["last_opcode"]) & 0xFF
