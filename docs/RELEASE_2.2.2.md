# Cartaconda 2.2.2 validation record

Date: 2026-08-03

## Scope

This patch addresses the remaining Mapper 4 slowdown exposed by the supplied
2.2.1 diagnostics. Drawing averaged 0.432 ms, but emulation CPU time averaged
22.022 ms with a 42 ms p95 wall time. The core missed 7,443 of 9,086 frame
budgets, starved the bounded frame queue 3,868 times, and consequently
underran audio 1,120 times. The optimization target is therefore CPU/PPU core
work rather than SDL presentation, pacing, or a larger audio buffer.

No commercial ROM, title string, ROM hash, game data, or game-specific code is
included in or required by the optimization.

## CPU execution

The bounded CPU executor already proves that every instruction in a candidate
span can touch only mirrored CPU RAM, the stack, or immutable mapped PRG. It
now uses that proof for the complete official memory/ALU family rather than a
small set of immediate and zero-page forms.

The local path covers:

- LDA, LDX, LDY, STA, STX, and STY;
- AND, EOR, ORA, ADC, SBC, CMP, CPX, CPY, and BIT;
- ASL, LSR, ROL, ROR, INC, and DEC;
- all zero-page, indexed zero-page, absolute, indexed absolute, indexed
  indirect, and indirect indexed forms admitted by the safety table;
- accumulator shifts, register transfers, stack pushes/pulls, JSR, RTS, and
  all eight conditional branches.

The helper retains 2A03 flags, mirrored addressing, page-cross cycles, stack
byte order, segmented MMC3 bank reads, final open bus, final opcode, and total
cycles. Accesses that can reach PPU/APU/controller registers, cartridge write
registers, DMC/OAM DMA, interrupts, or an unclassified indirect target remain
on the literal instruction path.

## Row-addressed PPU replay

The ordinary fast renderer now keys retained scanlines and world rows by the
physical nametable tile rows and attribute rows they actually fetch. A write
to one row changes only the dependent replay keys; unaffected framebuffer
lines remain valid. Palette bytes, CHR-RAM generation, scroll, mirroring,
sprites, and the mapper's effective CHR map remain independent dependencies.

MMC3 bank transitions advance a conservative replay fallback counter, but the
effective CHR/mirroring map is already represented by the mapper token. The
world-row cache therefore excludes that redundant counter while retaining
every content version. This lets recurring scanline maps hit the same
immutable rows instead of allocating one cache generation per transition.
Nametable dependency plans are cached independently of mapper transitions,
and scenes that have never changed VRAM use the original constant-time key.

## Differential correctness gates

- Every newly accelerated official opcode is executed once through the batch
  path and once through the literal core from identical registers, stack,
  flags, RAM, and open-bus state.
- Indexed page crossings are checked for RAM reads, RAM writes, NROM reads,
  and MMC3 segmented-bank reads.
- Sparse row retention is compared with a forced whole-frame invalidation;
  framebuffer bytes and complete serialized console state must match.
- The transition-heavy Mapper 4 gameplay cartridge continues to match the
  instruction-synchronized path in final pixels and complete machine state.
- All 217 standard-library tests pass.

## Performance gates

Measurements are isolated process runs on the same host. The CPU workload is
original test code shaped around calls, stack traffic, indexed RAM accesses,
ALU work, and branches; it contains no commercial program bytes.

| Workload | 2.2.1 | 2.2.2 | Result |
|---|---:|---:|---:|
| CPU throughput, five-run median | 5.018M cycles/s | 6.783M cycles/s | 1.35x |
| 600 sparse-row frames | 4.016 s | 0.838 s | 4.79x |
| 1,000 unchanged frames | 0.971 s | 0.945 s | 1.03x |
| Mapper 4 gameplay | 121.31 FPS | 124.60 FPS | 1.03x |
| Mapper 4 active IRQ | 211.68 FPS | 219.53 FPS | 1.04x |
| Mapper 4 CHR-register churn | 17.32 FPS | 18.27 FPS | 1.05x |
| Mapper 4 PRG-bank churn | 16.74 FPS | 21.84 FPS | 1.30x |

Every compared Mapper 4 workload retains the same final framebuffer SHA-256
between 2.2.1 and 2.2.2. Timing is host-dependent; deterministic pixels,
complete state, and the differential test suite are the correctness gates.

## Field-validation request

The release workload verifies the mechanisms but does not substitute for the
reporter's Windows/pygame-ce environment. Re-run Mega Man 5 with
`--diagnostics` and compare emulation average/p95, budget misses, frame-queue
starvation, scanline replays, and audio underruns with the supplied 2.2.1 log.

No ROM image, save state, commercial asset, or game-specific data is present
in the source, wheel, sdist, or release archive.
