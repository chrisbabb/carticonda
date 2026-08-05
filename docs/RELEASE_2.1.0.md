# Cartaconda 2.1.0 validation record

Date: 2026-07-29

## Scope

This release is a measured core-performance and timing-accuracy pass focused
on the reported Mapper 9 startup workload, while also removing a Mapper 4 CPU
span cost and correcting a forced-blank PPU edge. The supplied commercial ROM
was used only as a local differential input. It is not present in source,
tests, wheels, source distributions, or release archives.

## Competitive release bar

No emulator core or source implementation was copied. Current official project
materials were used only to define product and validation expectations:

- [MesenCE 2.2.1](https://github.com/nesdev-org/MesenCE/releases) reports
  continued CPU/APU/PPU accuracy work and additional AccuracyCoin passes.
- [puNES 0.111](https://github.com/punesemu/puNES/releases) demonstrates the
  breadth expected of a mature NES product: NES 2.0, extensive mapper and
  peripheral coverage, configurable controls, save-state migration, and audio
  regression fixes.
- [BizHawk](https://github.com/TASEmulators/BizHawk) sets a high tooling bar
  with deterministic frame control, input mapping, rerecording, and debugging.
- [AccuracyCoin](https://github.com/100thCoin/AccuracyCoin) remains a useful
  independent hardware-observation target for CPU, PPU, APU, DMA, and open-bus
  behavior.

Cartaconda 2.1.0 does not claim universal compatibility with those mature
projects. Its release claim is narrower and testable: an original Python core,
the documented mapper set, exact state preservation across each new optimized
path, and stable real-time delivery for the reproduced Windows problem.

## Root causes and changes

### Software-DAC scheduler

The 2.0.6 CPU executor already preserved many `$4011` values in one dispatch,
but the console still re-entered the full APU and PPU planners at every DAC
write. Version 2.1.0 merges DAC-write and host-sample boundaries in one APU
event loop. When both occur on the same CPU cycle, the mixer observes the old
DAC level before the CPU write, matching the literal path. The PPU advances
once across the bounded audio-only span.

The Mapper 9 immutable-code classifier also marks the complete recognized
routine's `$908D` entry as a scheduler yield. This prevents the generic CPU
span executor from crossing the specialist without invoking it.

### Forced-blank PPUDATA stream

Adjacent safe `$2007` values are returned as one compact ordered stream.
Nametable and palette bytes, PPU address increments, the final open-bus value,
and its exact decay deadline remain preserved. Production PxROM CHR-ROM writes
are ignored as hardware does, and CHR-RAM boards retain the literal path.
Presentation caches invalidate once when the stream changes visible data.

### Mapper 4 CPU span

MMC3 code executes from one active 8 KiB slot. The batch executor now indexes
that slot's immutable classification bytes directly instead of calling a
segmented table-view object for each proven-safe 6502 instruction.

### Forced-blank vblank edge

The fast PPU's short no-rendering branch previously skipped dot 1 when called
at cycle zero on scanline 241 or 261. That could omit vblank set or pre-render
clear in instruction-sized control runs. These two boundaries now enter the
event path and match the dot-timed model.

## Correctness gates

- 198 standard-library tests pass.
- 400 consecutive supplied-ROM frames match the instruction-synchronized
  control for CPU registers and cycles, full PPU state and pixels, APU state
  and generated PCM, RAM, bus/open-bus state, and mapper state.
- A standalone APU differential places a 44.1 kHz sample on the same cycle as
  a DAC write and compares the complete serialized result.
- The synthetic Mapper 9 forced-blank stream matches synchronized machine
  state while protecting CHR-RAM fallback.
- The fast forced-blank PPU has explicit vblank-set and pre-render-clear tests.
- A 420-frame exact-NTSC bounded-worker run completes with queue high-water 2,
  zero queue starvation, zero virtual audio underrun, and clean shutdown.
- Three 300-frame strict worker soaks (NROM, MMC2, MMC3) complete with zero
  queue starvation, zero virtual audio underrun, no rewind failure, and clean
  shutdown.

## Performance

Same-host packaged 2.0.6 versus 2.1.0:

| Workload | 2.0.6 | 2.1.0 | Change |
|---|---:|---:|---:|
| Mapper 9 forced blank | 42.68 FPS | 143.79 FPS | 3.37x |
| Mapper 4 gameplay | 86.43 FPS | 95.04 FPS | +10.0% |
| Supplied-ROM bell median | 4.036 ms/frame | 3.801 ms/frame | -5.8% |

The actual 420-frame bounded-worker run averaged 4.37 ms per emulated frame,
with a 6.00 ms p95 and 10.77 ms p99. One isolated 42.48 ms cold/rewind tail was
absorbed by the two-frame queue without starving presentation or PCM.

Host timings are not an accuracy proof and will vary by Python, CPU, and OS.
The state/PCM differential and regression suite are the correctness gates.

## Library decision

Pygame-ce remains the sole runtime dependency and the SDL owner remains on the
main thread. Profiles show the remaining core time in branch-heavy mutable
CPU/bus scheduling, mapper side effects, and scanline composition—not a large
homogeneous numeric operation. NumPy would not remove that dispatch, while a
JIT/compiler dependency would complicate Python 3.14 and frozen Windows
support, add startup behavior, and require a second release matrix. The
measured gains here come from original algorithmic event coalescing that works
in ordinary CPython and source installations.

## Known boundary

This release targets NTSC and the mapper list in the README. PAL/Dendy timing,
FDS, expansion audio, light guns, hundreds of uncommon boards, revision-
selectable MMC3/MMC6 behavior, and the full debugging/movie feature depth of
mature emulators remain future work. Those limits are stated explicitly so a
successful 2.1 performance gate is not confused with universal NES coverage.
