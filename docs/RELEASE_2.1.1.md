# Cartaconda 2.1.1 validation record

Date: 2026-07-29

## Scope

This patch addresses the Mapper 9 first-fight slowdown reported against
Cartaconda 2.1.0 and the accompanying 17 ms gameplay draw time. The supplied
commercial ROM was used only as an authorized local differential input. It is
not present in source, tests, wheels, source distributions, or release
archives.

## Reproduced root cause

The 2.1.0 startup/bell optimizations were active and fast. The later visible
fight uses a different synchronization path:

```text
LDA $2002
AND #$40
BNE/BEQ loop
```

Punch-Out!! executes this sprite-zero clear/set wait roughly one thousand
times per frame. Each nine-cycle iteration entered the CPU, bus, APU, and PPU
Python dispatchers three times. In the captured state, 100 frames produced
107,006 literal instructions, 106,174 short safe-batch entries, and roughly
94,000 `$2002` reads.

The field log's separate 17.021 ms draw average also exposed a host-side
format conversion. Cartaconda scaled the RGB24 NES surface into a large RGB24
surface and then blitted it into the usual 32-bit Windows display surface,
converting every scaled pixel.

## Changes

### Exact sprite-zero wait batching

The scheduler recognizes the complete immutable seven-byte wait signature.
It replays only the final still-taken status read before the next possible
sprite-zero transition. The batch is bounded by:

- the pre-render dot-1 sprite-flag clear;
- the current scanline's exact or conservative sprite-hit candidate;
- MMC2's ordered scanline commit at dot 256;
- APU frame-sequencer and PPU/NMI deadlines;
- the next DMC sample-buffer fetch; and
- the ordinary emulated-frame boundary.

The transition-observing iteration remains literal. The optimized path
preserves the final `$2002` value and write-toggle side effect, PPU open-bus
decay deadline, CPU A/P/PC/open-bus state, taken-branch timing, and DMC-held
read behavior.

### Native-format presentation

Each changed RGB24 frame is converted once at 256x240 into a reusable surface
matching the display format. Scaling, the large reusable destination, and the
final blit then use that same format. The existing integer nearest-neighbor
pixel presentation is unchanged.

Diagnostics now report preparation and flip timing separately:

- `draw-prepare-average-ms` / `draw-prepare-max-ms`
- `flip-average-ms` / `flip-max-ms`

This distinguishes CPU scale/blit cost from an SDL driver or compositor block.

## Correctness gates

- 201 standard-library tests pass.
- 300 consecutive supplied-ROM first-fight frames match the 2.1.0 path in
  complete serialized CPU, PPU, APU/PCM, RAM, bus/open-bus, mapper, controller,
  and framebuffer state at every frame boundary.
- A synthetic active-DMC differential proves that overflow/status flags and
  buffered playback remain identical across a long polling batch.
- A second differential places the next DMC fetch exactly one CPU cycle after
  a completed polling iteration, then compares the held `$2002` read and
  four-cycle DMA stall.
- A dot-256 regression prevents an MMC2 latch-ordered scanline commit from
  being crossed when it can reveal the definitive sprite hit.
- Three 300-frame NROM/MMC2/MMC3 strict soaks finish with zero queue
  starvation, zero virtual audio underrun, no rewind failure, and clean
  shutdown.

## Performance

Four GC-suspended 120-frame measurements from the same captured fight state:

| Path | Wall median | CPU median |
|---|---:|---:|
| 2.1.0 execution path | 21.620 ms/frame | 21.588 ms/frame |
| 2.1.1 sprite-wait path | 9.558 ms/frame | 9.552 ms/frame |

That is a 2.26x core speedup and moves the median from over the 16.639 ms NTSC
budget to roughly 7 ms of headroom.

A 420-frame exact-NTSC run through the real bounded worker, virtual audio
consumer, and rewind compressor recorded:

- 9.219 ms emulation average;
- 14.539 ms p95 and 15.670 ms p99;
- three isolated budget misses absorbed by the two-frame queue;
- zero queue starvation and zero virtual audio underrun;
- 70 rewind captures; and
- clean shutdown.

The native-format SDL dummy-display benchmark measured:

| Output size | RGB24 scale + converting blit | Native-format path |
|---|---:|---:|
| 512x480 | 0.280 ms | 0.148 ms |
| 768x720 | 0.614 ms | 0.295 ms |
| 1024x960 | 1.093 ms | 0.490 ms |
| 1920x1080 | 2.341 ms | 1.007 ms |

These display figures isolate CPU preparation and do not claim to reproduce a
particular Windows compositor. Field diagnostics now measure the flip
separately.

## Dependency decision

Pygame-ce 2.5.7 remains the sole runtime dependency and officially supports
Python 3.14. Its documented reusable scale destination requires the source
format, while `Surface.convert()` provides the display-optimized blit format.
The two-stage native-resolution conversion applies those APIs without adding a
numeric/JIT dependency or moving any SDL object off the main thread.

Host timing is not an accuracy proof and varies by CPU, OS, Python, and video
driver. The frame-by-frame machine-state differential and regression suite
remain the release gates.
