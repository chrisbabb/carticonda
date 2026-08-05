# Cartaconda 2.2.0 validation record

Date: 2026-08-03

## Scope

This release targets the unplayable Mapper 4 slowdown reported with Mega Man
5. The 2.1.3 field run established that display work was already inexpensive
while core scheduling was not:

- 33.528 ms average and 52.000 ms p95 emulation time;
- 0.483 ms average draw time;
- 2,087,640 CPU/device flushes over 3,544 frames;
- 97,840,392 cycles spread across those flushes, only about 47 cycles each;
- 1,709,251 literal instructions and 1,763,696 safe batches;
- 3,607 frame-queue starvations and 571 audio underruns; and
- only eight code-bank compilations but 7,140 deferrals.

The cycle-weighted hot addresses `$FEB4`–`$FEBE` are the fixed-bank subsystem
scanner documented by the public
[Mega Man 5 disassembly](https://github.com/refreshing-lemonade/megaman5-disassembly).
That documentation also confirms MMC3 and 32 8 KiB PRG banks. No commercial
ROM, code, graphics, or audio is included in Cartaconda's source, tests, or
release archives.

## Root cause

Version 2.1.3 knew the three possible filtered A12 phases on every rendered
scanline. When MMC3 IRQ output was enabled, it conservatively stopped the CPU
worker at each phase so the mapper could clock its counter. Most of those
edges could not assert IRQ, but each still forced the Python scheduler to:

1. publish CPU state;
2. enter the APU and PPU planners;
3. advance a very short device interval;
4. sample interrupt lines; and
5. rebuild the next deadline.

The 6502 then spent its remaining time repeatedly interpreting the same
four-record idle scan until an interrupt arrived. The emulator was doing
correct work in extremely small fragments rather than doing too much video or
host presentation work.

## Changes

### Exact MMC3 assertion projection

The fast PPU now projects the mapper's current latch, counter, reload,
enable/pending state, filtered A12 level, and low-period timestamp across the
remaining visible fetch schedule. It returns the dot distance to the first
edge that can actually assert IRQ without changing live PPU or mapper state.

The console can therefore cross earlier non-asserting A12 edges and whole
scanlines in one device span. It still flushes before the asserting edge so
the 6502 observes IRQ at the same instruction boundary. CPU PPU-register
accesses always synchronize first, and pre-render A12 phases retain explicit
deadlines rather than relying on the visible-line projection.

### Cooperative scheduler batching

A new load-time classifier recognizes this complete immutable structure:

```text
LDX #0 / STX reset / LDY #4
LDA status,X / CMP #limit / BCS work
INX / INX / INX / INX / DEY / BNE scan
JMP setup
```

At runtime the batch is admitted only when:

- execution is at the canonical scan entry;
- X is zero and Y is four;
- all four zero-page records select the idle branch;
- the setup write cannot overlap a scanned record;
- no NMI, sampled IRQ, interrupt-enable transition, or unmasked IRQ is
  pending; and
- the console has already bounded the span before the next PPU/APU/DMC event.

One complete idle pass costs 93 6502 cycles. Whole passes are applied in O(1),
with any partial final pass left to the normal core. The executor preserves
the setup RAM write, A/X/Y/P, PC, open bus, last opcode and final-instruction
length, total CPU cycles, and the console's instruction-boundary IRQ poll.
A ready subsystem takes the literal path immediately.

The optimization is keyed to code shape and live hardware state. It does not
contain a game title, ROM checksum, or fixed execution address.

### Bank and PPU-map admission

MMC3 exposes four independently selected 8 KiB CPU slots. Version 2.1.3 waited
for 64 observations before classifying an unrecognized bank/slot pair, which
made short-lived scene and sound engines remain literal. Version 2.2.0
classifies a bank the first time the CPU executes it and classifies the fixed
reset bank during reset. Data-bank churn cannot trigger this work because the
cache key always uses the CPU's current code slot.

MMC3's effective CHR offsets and mirroring mode are now cached as one PPU
token. The token changes only when a mapper write changes visible mapping, so
equivalent register writes avoid repeated tuple construction and retained-line
invalidation.

## Correctness gates

- All 211 standard-library tests pass.
- The IRQ predictor stops at the exact future asserting edge, not the next
  candidate edge.
- A separate test keeps an explicit pre-render A12 boundary.
- An original Mapper 4 cartridge combines active filtered-A12 IRQs and a
  four-record cooperative scheduler without commercial data.
- Across 120 consecutive frames, the optimized core and an
  instruction-synchronized reference have identical aggregate pixels and
  complete serialized state:

  - frames:
    `561261e7d44fcb0b4d2eade10cd9140262030d769a55ad29dde48bc0423745da`
  - state:
    `7d73bd2e8403e90fb85a179be19b35e4f224037b9f90d25596f33f03d658991d`

The synthetic cartridge is generated in memory by `tools/benchmark.py` and is
also available to `tools/soak.py` as `mmc3-irq`.

## Performance

The real bounded worker, two-frame queue, exact fractional-NTSC consumer,
44.1 kHz virtual PCM reservoir, and rewind capture path were exercised with
the active-IRQ scheduler workload:

| Build | Frames | Average | p95 | p99 | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|---:|---:|
| Packaged 2.1.3 | 300 | 20.750 ms | 22.252 ms | 24.209 ms | 58 | 8 |
| 2.2.0 | 900 | 5.088 ms | 7.792 ms | 13.303 ms | 0 | 0 |

The average improves by 4.08x, equivalent to about 75.5% less frame time or
308% greater throughput. The 2.2.0 run reached queue high-water two, retained
at least 3,610 PCM samples, captured 150 rewind snapshots, reported no rewind
failure, and shut down cleanly.

A separate three-run direct benchmark measured:

| Build | Median frame time |
|---|---:|
| Packaged 2.1.3 | 26.675 ms |
| 2.2.0 | 4.830 ms |

Over 300 diagnostic frames, the new workload averages 32.8 device flushes per
frame and compiles one fixed code bank after one admission observation. The
reported 2.1.3 Mega Man 5 run averaged about 589 flushes per frame.

These measurements exceed the requested 200% speed increase on the isolated
failure mode. Timing remains host-dependent; the user's Python 3.14/Windows
Direct3D/WASAPI run is the final game-specific acceptance test.

## Audio impact

No APU rate, channel equation, sample clock, nonlinear mixer, output-filter
coefficient, or host PCM buffer size changes in 2.2.0. The improvement removes
producer starvation: the validation run generates every PCM frame on time and
records zero underruns. Version 2.1.3's 2,048-sample native blocks and complete
90 Hz/440 Hz high-pass plus 14 kHz low-pass chain remain intact.

## Dependency decision

Pygame-ce remains the only runtime dependency. This workload is mutable
branch/control-flow scheduling, not bulk numeric processing, so NumPy would
not accelerate it. A required JIT would add Windows/frozen-runtime complexity
and warm-up behavior. The release instead removes redundant Python dispatch
while keeping the source-compatible CPython implementation and reference
paths.

