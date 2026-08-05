# Cartaconda 2.2.7 validation record

Date: 2026-08-03

## Scope

This patch addresses the intermittent lag, sound skipping, and crackle still
reported during side-scrolling Mega Man 5 gameplay in Cartaconda 2.2.6. The
supplied 3,956-frame Windows diagnostic made the remaining boundary clear:

- drawing averaged 0.618 ms/frame, excluding SDL presentation as the sustained
  bottleneck despite one isolated 82.313 ms host flip;
- core time averaged 13.115 ms, but p95/p99 reached 28.250/40.250 ms;
- 940,643 scanlines were requested and 449,692 replayed, approximately 238/114
  per frame;
- the worker queue starved 243 times and native audio underran 377 times;
- 1,661 Python hot blocks were generated during play; and
- sparse mapper/nametable changes coincided with a continuously moving
  viewport.

The correction is general to specification-derived Mapper 4 execution and
render state. It contains no commercial ROM, title hash, game code, graphics,
audio, or data and does not depend on a game-specific patch.

## Scrolling PPU correction

The world-row cache already keyed each entry by the effective physical
nametable tile row, attribute row, CHR generation and mapping, palette,
mirroring, fine Y, and mask. A content-tracked nametable write nevertheless
also advanced the broad world generation and disabled the complete cache. In
a scrolling level this discarded rows whose exact dependencies had not
changed.

Version 2.2.7 retains the cache after a content-tracked write. The modified
physical tile or attribute row receives a new version and therefore misses its
old key; every unrelated row remains valid. Writes that cannot be represented
by those exact versions still take the conservative invalidation path.

Complete visible-line batches now also compute palette colors and effective
MMC3 background mapping once. Each line computes its nametable signature once
and shares it between retained-frame and background lookup. An immutable
background byte string moves directly into the retained entry instead of
being copied back out of the framebuffer.

## Cross-slot CPU spans

Version 2.2.6 classified a newly entered stable MMC3 slot correctly, but a
safe JSR, RTS, or JMP still ended the current scheduler call at every 8 KiB
boundary. Both the source and target slots are immutable in the active mapping
and no device has advanced inside that proven span.

The safe executor can now continue under the original device deadline when
the target slot is already classified. The chain is capped at eight slot
transitions. An unclassified target, dynamic indirect address, timed access,
mapper write, interrupt hazard, DMA/DMC edge, or exhausted device deadline
returns to the ordinary scheduler exactly as before.

The 900-frame cross-slot scrolling regression executes 24,201 safe scheduler
batches instead of 34,367, a 29.6% reduction. A dedicated differential test
disables chaining in the control console and verifies identical framebuffer
and complete serialized CPU/PPU/APU/mapper state.

## Tail-latency protection

Pure-Python hot-block generation remains bounded and valuable after warm-up,
but the field session generated 1,661 blocks. Compilation is optional host
work, not emulated hardware work. The threaded frontend now permits it only
while at least one completed frame is already in the ready queue. If the queue
is empty, the CPU uses its existing safe generic batch, keeps the hot
observation, and compiles when buffered lead returns. `cpu-block-deferrals`
counts these protected events.

Rewind compression is similarly optional. After a producer tail, capture
resumes only after twelve consecutive published frames leave the ready queue
full. Stable scenes retain the normal six-frame history cadence; a marginal
scene spends recovered lead on video and PCM first. `rewind-captures` reports
the completed snapshots alongside the existing deferral count.

## Audio discontinuity repair

A true underrun has already lost samples, so exact output is no longer
possible at that boundary. Cartaconda now ramps the final submitted signed PCM
value to zero over at most 256 samples, then fades the first recovered chunk
from zero over the same short window. This removes both the stop and restart
step without touching any uninterrupted emulated PCM. The normal mixer path,
44.1 kHz sample clock, nonlinear APU mix, and filter state are unchanged.

`audio-concealments` counts accepted decay tails and
`audio-recovery-fades` counts accepted first recovered chunks. The ordinary
`audio-underruns` counter remains the decisive continuity metric.

## Packaged 2.2.6 comparison

Three uncontended alternating runs compare the packaged 2.2.6 wheel with the
2.2.7 source on the same interpreter and host. Both builds produce the same
final scrolling framebuffer SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

| Workload | Packaged 2.2.6 median | 2.2.7 median | Change |
|---|---:|---:|---:|
| MMC3 IRQ, 900 frames | 2.110 s | 2.116 s | 0.3% slower (noise/neutral) |
| Sprite cadence, 1,200 frames | 3.091 s | 3.035 s | 1.8% lower |
| Cross-slot scroll, 900 frames | 2.874 s | 2.570 s | 10.6% lower |

The scrolling workload rises from 313.2 to 350.2 FPS by median elapsed time,
or 1.118x throughput. The independent IRQ gate remains neutral, which is
expected because it neither scrolls sparse nametable content nor crosses the
new helper-slot boundary.

## Correctness gates

- All 230 standard-library tests pass.
- Batched CPU, PPU, APU, DMA/DMC, mapper, controller, save-state, rewind,
  presentation, audio-queue, ROM/ZIP, and UI paths remain green.
- A new test proves sparse nametable writes retain cache admission while exact
  row versions force the changed content to miss.
- A new cross-slot differential test compares chained and unchained execution
  for complete frame and machine-state identity.
- Worker tests prove runtime translation is disabled for the unbuffered first
  frame and enabled after one frame of producer lead exists.
- Audio tests prove the underrun tail, rebuffer gate, recovery fade, retained
  PCM on native start failure, and pygame-ce-safe `Sound.play()` retry.

## Threaded exact-NTSC gates

The five-workload 300-frame gate uses the real two-buffer emulation worker,
fractional NTSC consumer, 4,096-sample virtual PCM reservoir, compressed rewind
history, and orderly shutdown. Every workload reaches queue depth two and
finishes with zero queue starvation, zero audio underruns, and clean shutdown.

The final isolated 900-frame scrolling run records:

| Average | p95 | p99 | Maximum | Budget misses | Queue starves | Audio underruns |
|---:|---:|---:|---:|---:|---:|---:|
| 3.328 ms | 3.803 ms | 5.053 ms | 20.695 ms | 1 | 0 | 0 |

It retains at least 3,610 virtual PCM samples after playback starts, completes
149 rewind captures with 13 headroom deferrals, reports no rewind failure, and
shuts down cleanly. The single over-budget frame is the cold translation
warm-up and is absorbed by the bounded queue.

## Field-validation request

Re-run the affected Mega Man 5 level for at least 60 seconds with
`--diagnostics`. Compare `emulation-p95-ms`, `emulation-p99-ms`,
`frame-queue-starves`, and `audio-underruns` first. Also include
`cpu-safe-batches`, `cpu-translated-blocks`, `cpu-block-deferrals`,
`ppu-scanline-replays`, `rewind-captures`, `audio-concealments`, and
`audio-recovery-fades`.

On the reporter's Python 3.14/Windows/Direct3D/WASAPI system, zero underruns is
the target. Nonzero block deferrals are not a fault: they show that optional
translation yielded while the producer lead was empty. The supplied hardware,
driver, ROM revision, and play sequence remain the final field acceptance
gate.
