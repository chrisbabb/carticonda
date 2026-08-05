# Cartaconda 2.2.6 validation record

Date: 2026-08-03

## Scope

This patch addresses the Mega Man 5 side-scrolling slowdown reported from
Cartaconda 2.2.5. Title and stage-select screens remained fast, but entering
and moving through a level slowed almost to unplayable speed.

The supplied 3,433-frame Windows diagnostic separates that failure from video
or SDL presentation:

- drawing averaged only 0.475 ms/frame;
- PPU render/replay ratios remained approximately 237/111 lines per frame;
- worker CPU averaged 18.115 ms/frame;
- 7,050,685 instructions ran literally, about 2,054 per frame;
- safe batches fell to about 101 per frame;
- only two MMC3 bank/slot tables compiled;
- the frame queue starved 1,129 times and audio underran 399 times.

The level engine was therefore falling out of the optimized CPU path. No
commercial ROM, title-specific patch, ROM hash, game data, graphics, audio, or
code from another emulator is included in or required by the correction.

## Root cause

MMC3 exposes four immutable 8 KiB CPU slots. Cartaconda classifies only a slot
that the CPU actually executes, avoiding optimizer work when a game changes a
data bank. A newly observed bank/slot pair intentionally uses a two-observation
stability gate before building its table.

Version 2.2.5 reduced refresh overhead by calling that classifier only when the
mapper's complete four-bank tuple changed. That is sufficient for a mapper
write, but not for ordinary control flow. When fixed-bank code used JSR or JMP
to enter a previously unused slot while the tuple remained unchanged, the
first observation was recorded and the second was never made. Every
instruction in that slot then stayed on the literal executor for the rest of
the level. Reset/title code in an already compiled fixed slot remained fast,
which explains the sharp menu-to-gameplay transition.

## Correction

Both the normal instruction stepping path and batched frame scheduler now
refresh when either:

1. the active MMC3 bank tuple has changed; or
2. the current PC is in an 8 KiB slot whose table is not ready.

The second scheduler boundary completes the existing stability gate. Once the
slot is ready, the low-cost tuple-identity check remains in effect. Unexecuted
data banks are still never compiled, tables remain keyed by immutable bank
identity and CPU slot, and mapper writes still end the current device span.

This is a host optimizer-state correction only. It does not change a CPU
opcode, cycle count, interrupt boundary, mapper mapping, PPU event, APU clock,
frame, or save-state field.

## Side-scrolling regression cartridge

The new original Mapper 4 workload has a fixed `$E000` main loop that repeatedly
calls RAM-heavy game logic in the unchanged second-last bank at `$C000`. Each
NMI advances horizontal scroll and performs OAM DMA. A populated background is
rendered through the ordinary fast PPU path. No mapper write accompanies the
cross-slot call, directly guarding the 2.2.5 failure mode.

The optimized build and an instruction-synchronized reference produce
identical frames and complete serialized machine state. Across three
alternating 300-frame runs, packaged 2.2.5 and 2.2.6 finish with the same frame
SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

| Metric | Packaged 2.2.5 | 2.2.6 | Improvement |
|---|---:|---:|---:|
| Median elapsed time, 300 frames | 8.847 s | 0.942 s | 89.4% lower |
| Median frame time | 29.489 ms | 3.138 ms | 89.4% lower |
| Median throughput | 33.91 FPS | 318.64 FPS | 9.40x |
| Real-time multiple | 0.56x NTSC | 5.30x NTSC | 9.40x |

The packaged 2.2.5 elapsed times are 8.987, 8.792, and 8.847 seconds. The
2.2.6 times are 0.960, 0.919, and 0.942 seconds.

## Dispatch evidence

An independently instrumented 120-frame segment records:

| Counter | Packaged 2.2.5 | 2.2.6 |
|---|---:|---:|
| Literal instructions | 1,223,991 | 773 |
| Safe batches | 1,278 | 4,560 |
| Compiled bank/slot tables | 1 | 2 |
| Deferred bank/slot candidates | 1 | 2 |
| Translated blocks | 8 | 17 |

Literal dispatch falls 99.94%. The extra compiled table is precisely the
executed `$C000` slot; no unexecuted bank is classified.

## Correctness gates

- All 229 standard-library tests pass.
- The new test begins with `$C000` unclassified, enters it without a mapper
  write, verifies its table becomes ready, and compares the resulting frame
  plus complete machine state with instruction-synchronized execution.
- Existing MMC3 bank-churn coverage still proves that changing unexecuted data
  banks does not build tables.
- Complete CPU, PPU, APU, mapper, DMA/DMC, save-state, rewind, audio queue,
  controller, UI, and ROM-loading regressions remain green.
- Python bytecode compilation succeeds for all source, tools, and tests.

## Threaded exact-NTSC gate

A repeated 900-frame run uses the real two-frame worker queue, fractional NTSC
consumer, 4,096-sample virtual PCM reservoir, compressed rewind history, and
orderly shutdown:

| Average | p95 | p99 | Maximum | Budget misses | Queue starves | Audio underruns |
|---:|---:|---:|---:|---:|---:|---:|
| 3.462 ms | 4.513 ms | 6.457 ms | 22.640 ms | 2 | 0 | 0 |

It retains 151 rewind captures, keeps at least 3,610 queued PCM samples after
playback starts, reports no rewind failure, and shuts down cleanly. An earlier
run on the shared validation host recorded one 38.610 ms tail and four video
queue starvations, but still zero audio underruns; the immediate isolated
repeat above passed the strict gate. Both observations are retained here.

## Field-validation request

Re-run the affected side-scrolling Mega Man 5 section with `--diagnostics`.
The decisive values are `emulation-average-ms`, p95/p99,
`cpu-literal-instructions`, `cpu-safe-batches`, `cpu-bank-compiles`,
`frame-queue-starves`, and `audio-underruns`. `cpu-bank-compiles` should rise as
new executable slots are first entered, while literal instructions should no
longer grow by thousands per frame.

The reporter's Python 3.14/Windows/Direct3D/WASAPI system and cartridge
sequence remain the final field acceptance gate. No ROM image, save state,
commercial asset, or title-specific data is present in the source, wheel,
sdist, or release archive.
