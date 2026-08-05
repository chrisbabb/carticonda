# Cartaconda 2.2.5 validation record

Date: 2026-08-03

## Scope

This patch addresses the remaining Mega Man 5 latency and CPU cost reported
from Cartaconda 2.2.4. The supplied 6,494-frame Windows run spent only 0.456 ms
per draw, but 16.253 ms of worker CPU per emulated frame. It missed 3,161
budgets, starved the two-frame queue 1,119 times, and consequently reported 702
audio underruns. Optimization therefore remains inside the Python CPU/APU/PPU
core; resolution, frame presentation, audio buffering, and emulated timing are
not reduced.

No commercial ROM, title-specific patch, ROM hash, game data, or code from
another emulator is included in or required by this change.

## CPU and scheduler

The immutable-code classifier now has three values: unsafe/device-visible,
statically safe, and dynamically guarded indirect. Official `(d,X)` and
`(d),Y` operations can enter generated hot blocks. The block reads the zero-
page pointer at run time and exits before publishing the opcode or touching the
target if either the effective or page-cross dummy address can reach a device,
mapper register, or mutable cartridge memory. RAM and immutable PRG accesses
continue with the same cycle count, dummy-read ordering, open bus, flags, and
final program counter as literal execution.

The scheduler consults that classification directly, dispatches a proven CPU
span before attempting more expensive PPU wait-loop recognition, and admits
useful spans with four remaining CPU cycles. Mapped fast tables refresh only
when the active immutable bank tuple actually changes. On a representative
five-million-cycle `(d),Y` stream, the generated path measured 24.96 million
emulated cycles/s versus 6.55 million on the literal path, approximately 3.8x
faster.

## APU

Quiet sample runs now advance inaudible pulse, triangle, noise, and drained-DMC
timers once per span rather than once per host sample. Silent DMC shifts are
counted directly. Noise mode 0 and mode 1 LFSR clocks collapse in exact
14-clock and 9-clock groups before processing the remainder. The common
44.1 kHz phase-to-next-sample interval is also table-driven.

These shortcuts do not suppress hardware clocks. Differential tests compare
every channel timer, sequencer, length/envelope state, DMC state, noise LFSR,
PCM sample, analog-filter accumulator, IRQ, and complete serialized APU state
against per-cycle execution.

## PPU and MMC3

The fast PPU can consume complete visible scanlines when no CPU-visible event
interrupts them. One operation retains the exact MMC3 A12 phases, sprite
overflow, sprite-zero event, scanline pixels, horizontal and vertical scroll
copies, odd-frame state, and frame completion of the existing event path.

MMC3 IRQ prediction no longer iterates across every prospective phase and
scanline. It evaluates the current and following line explicitly, computes the
number of qualified counter edges required for assertion, and projects the
remaining regular cadence in constant time. Exhaustive phase-state tests cover
both pattern-table layouts, every visible/pre-render position, A12 filter age,
reload, latch, counter, enable, and pending combinations.

Palette cache identity is split at the real background/sprite boundary. A
sprite-only palette write can retain background-only rows; complete scanline
batches compute those compact identities once and reuse them across their
lines. The new `ppu-palette-background-preserves` diagnostic counts those
events.

## Correctness gates

- All 228 standard-library tests pass in 13.432 seconds on the validation host.
- Every supported official indirect opcode is forced through a generated
  one-instruction block and compared with literal execution.
- A pointer changed by a preceding instruction is proven to exit before a
  device access, including page-cross dummy reads.
- Complete visible-line batching is compared with generic fast events for
  pixels, scroll, status, frame timing, and MMC3 state.
- Constant-time MMC3 IRQ projection is compared exhaustively with the prior
  phase-by-phase reference.
- Muted/inaudible APU timer collapsing is compared with every reference cycle.
- Sprite-only palette changes preserve eligible background rows while matching
  conservative invalidation output.
- Python bytecode compilation succeeds for all source, tools, and tests.

## Targeted Mapper 4 throughput gate

The original synthetic workload uses a scrolling populated Mapper 4 scene,
moves 16 sprites every frame, changes a sprite palette entry every six frames,
and changes a sprite-side CHR bank every 13 frames. Each build runs 1,200
frames at 44.1 kHz three times in alternating processes. Both finish with the
same frame SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

| Metric | Packaged 2.2.4 | 2.2.5 | Improvement |
|---|---:|---:|---:|
| Median elapsed time | 6.122 s | 3.604 s | 41.1% lower |
| Median time per frame | 5.102 ms | 3.003 ms | 41.1% lower |
| Median throughput | 196.02 FPS | 332.98 FPS | 1.70x |
| Real-time multiple | 3.26x NTSC | 5.54x NTSC | 1.70x |

The three 2.2.4 elapsed times are 6.186, 6.093, and 6.122 seconds. The three
2.2.5 times are 3.497, 3.604, and 3.605 seconds. Host timing varies; matching
pixels and state are the correctness gates.

## Threaded exact-NTSC gate

Separate 900-frame runs use the real two-frame worker queue, exact fractional-
NTSC consumer, 4,096-sample virtual PCM reservoir, compressed rewind history,
and orderly worker shutdown:

| Workload | Average | p95 | p99 | Maximum | Budget misses | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|---:|---:|---:|
| Mapper 4 | 4.246 ms | 5.257 ms | 8.495 ms | 21.906 ms | 3 | 0 | 0 |
| Mapper 4 active IRQ | 3.036 ms | 3.253 ms | 12.190 ms | 41.263 ms | 4 | 0 | 0 |

Both runs retain 150 rewind captures, preserve at least 3,610 queued PCM
samples after playback starts, report no rewind failure, and shut down cleanly.

## Field-validation request

Re-run the affected Mega Man 5 sequence with `--diagnostics`. The principal
acceptance values are `emulation-average-ms`, p95/p99, `budget-misses`,
`frame-queue-starves`, and `audio-underruns`. The synthetic result provides
substantial headroom, but the reporter's Python 3.14/Windows/Direct3D/WASAPI
system and cartridge sequence remain the decisive field test.

No ROM image, save state, commercial asset, or title-specific data is present
in the source, wheel, sdist, or release archive.
