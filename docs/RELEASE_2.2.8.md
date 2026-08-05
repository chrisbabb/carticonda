# Cartaconda 2.2.8 validation record

Date: 2026-08-04

## Scope

This patch corrects the performance regression reported in Cartaconda 2.2.7
during side-scrolling Mega Man 5 gameplay. The supplied Windows diagnostic
separates the expensive subsystem clearly:

- drawing averaged 0.659 ms/frame, so Direct3D presentation was not the
  sustained bottleneck;
- core time averaged 14.478 ms, with 37.750 ms p95 and 61.000 ms p99 tails;
- the worker queue starved 366 times and native audio underran 324 times;
- 1,597 optional hot Python blocks were generated during 3,344 frames; and
- only 36 generation deferrals were recorded by the 2.2.7 queue gate.

The correction is general to immutable banked CPU execution. It contains no
commercial ROM, title hash, game code, graphics, audio, or data and does not
depend on a game-specific patch.

## Root cause

Version 2.2.7 allowed optional block generation whenever one completed frame
was buffered. That prevented compilation only after the producer had already
lost all lead. A newly entered level could therefore generate many Python
functions in one emulated frame while the queue still contained one packet,
turning recoverable average work into 40–60 ms bursts.

The 2.2.7 cross-slot executor also recursed into any safe target MMC3 slot.
Although that reduced scheduler-call counts in a steady synthetic loop, it
could move cold target classification, generic execution, and block generation
into the same device deadline. A low dispatch counter consequently did not
guarantee a short frame.

## Bounded runtime translation

`Console.run_frame()` now opens an explicit host-code-generation budget before
running the emulated frame:

- the first two frames may generate at most four blocks each while the initial
  two-packet worker reservoir is being filled;
- every subsequent frame may generate at most one block; and
- execution never waits for a future budget. A deferred start immediately uses
  the existing deterministic generic safe executor.

The hotness counter saturates at its threshold. Once the frame budget is
closed, a transient cache sentinel marks each deferred start until the next
frame boundary. Repeated visits therefore avoid counter growth, tuple
allocation, and side-set membership checks. `cpu-block-deferrals` counts one
unique deferred hot start per frame, while `cpu-block-compile-peak` reports the
largest number of blocks generated in one frame. A normal run can report four
because of the two hidden startup frames; gameplay is capped at one.

Cross-slot execution remains available only when the destination cache entry
is already a callable compiled block. Cold, generic, unclassified, dynamic,
timed, interrupt-sensitive, or mapper-visible targets return to the Console
scheduler. Already-hot fixed-bank helper calls can still share the remaining
device deadline, with the existing limit of eight slot transitions.

## Retained 2.2.7 improvements

The correction does not discard the useful 2.2.7 work. Exact-versioned
background world rows remain cached across unrelated nametable writes;
visible-line batches continue sharing mapper, palette, and nametable context;
rewind capture still waits for sustained producer headroom; and confirmed
native underruns still receive a decay tail and recovery fade. This keeps the
average-time, presentation, and discontinuity-repair gains while removing the
new CPU tail.

## Translation-burst regression

An original 16 KiB immutable NOP image preheats 1,024 distinct safe starts to
one observation below the translation threshold, then exposes 16 new hot
starts per simulated gameplay frame for 64 frames. It measures optional host
generation directly and contains no commercial data.

Three fresh-process runs on the same interpreter produced these medians:

| Metric | Packaged 2.2.7 | 2.2.8 | Change |
|---|---:|---:|---:|
| Average dispatch time | 29.434 ms | 2.468 ms | 91.6% lower |
| p99 dispatch time | 42.933 ms | 8.257 ms | 80.8% lower |
| Blocks generated | 1,024 | 70 | Bounded over time |
| Gameplay generation peak | Unbounded | 1/frame | Deterministic |

The 2.2.8 count includes four blocks in each of its first two startup-budget
frames and one in each of the remaining 62 frames. Deferred starts are not
lost; they remain eligible in later frames and execute through the safe generic
path in the meantime.

## Combined field benchmark

The new `fast-mmc3-field-tail` workload combines the established cross-slot
scrolling program with 16 moving sprites, a sparse nametable mutation every six
frames, a sprite-palette change every 16 frames, and a sprite-side CHR change
every 14 frames. It models the CPU/PPU cache cadence in the supplied field log
without using the game ROM.

Across three alternating 1,200-frame runs, both builds produced final
framebuffer SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

| Metric | Packaged 2.2.6 | 2.2.8 | Change |
|---|---:|---:|---:|
| Median elapsed time | 7.016 s | 6.254 s | 10.9% lower |
| Median throughput | 171.05 FPS | 191.87 FPS | 1.122x |
| Median p99 | 12.121 ms | 11.418 ms | 5.8% lower |
| Median maximum | 33.775 ms | 24.234 ms | 28.2% lower |

This comparison confirms that retaining the 2.2.7 PPU changes and using
hot-only slot chaining remains faster than the known-good 2.2.6 field path.
The translation-burst comparison above isolates the separate 2.2.7 tail
regression.

## Correctness gates

- All 231 standard-library tests pass.
- CPU differential tests compare bounded translation against the generic path
  for complete machine state and verify one gameplay compilation per frame.
- A cross-slot differential test proves already-hot chaining preserves pixels
  and serialized CPU/PPU/APU/mapper state while a cold target is not admitted.
- Existing CPU opcode, interrupt, DMA/DMC, APU, PPU, mapper, controller,
  save-state, rewind, presentation, audio-queue, ROM/ZIP, and UI tests remain
  green.
- The release benchmark and soak images are original synthetic fixtures; no
  supplied commercial ROM is included in source, wheel, sdist, or archive.

## Threaded exact-NTSC field gate

Three 900-frame runs use the real two-buffer emulation worker, fractional NTSC
consumer, 4,096-sample virtual PCM reservoir, compressed rewind history, and
orderly shutdown. Every run reaches queue depth two and completes with zero
queue starvation, zero audio underruns, no rewind failure, and clean shutdown.

| Average | p95 | p99 | Maximum | Budget misses | Queue starves | Audio underruns |
|---:|---:|---:|---:|---:|---:|---:|
| 6.696 ms | 9.603 ms | 15.447 ms | 42.110 ms | 9 | 0 | 0 |

The values are medians of the three complete soaks. The maximum in each run was
cold frame 2, while the initial worker reservoir was still filling; it did not
starve presentation or audio. At least 3,610 virtual PCM samples remained
after playback started.

## Field-validation request

Re-run the affected side-scrolling level for at least 60 seconds with
`--diagnostics`. Compare `emulation-p95-ms`, `emulation-p99-ms`,
`frame-queue-starves`, and `audio-underruns` first. Also include
`cpu-translated-blocks`, `cpu-block-deferrals`,
`cpu-block-compile-peak`, `cpu-safe-batches`, `ppu-scanline-replays`,
`rewind-captures`, `audio-concealments`, and `audio-recovery-fades`.

On the reporter's Python 3.14/Windows/Direct3D/WASAPI system, zero queue
starvation and zero audio underruns are the target. Deferrals are protective,
not failures: they show cold optional translation was spread across later
frames. A compile peak above four would indicate that the bounded path was not
active; a normal startup peak may be four, while gameplay itself is capped at
one.
