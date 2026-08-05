# Cartaconda 2.0.3 validation record

This patch fixes the forced-blank Mapper 9/MMC2 slowdown and bell-audio
stutter reported before the first fight in Mike Tyson's Punch-Out!!.

## Verified cause

The affected title uses Mapper 9/MMC2. During the transition it disables
rendering, plays tonal audio, and performs dense PPUDATA (`$2007`) setup
traffic. Version 2.0.2 synchronized the APU and PPU around every timed write
and repeatedly prepared presentation work for frames whose pixels did not
change.

The 2.0.3 path:

- assigns every admitted PPUDATA write its exact future PPU master-clock
  timestamp;
- stops before PPU, APU, DMC, rendering-change, or frame deadlines;
- disables admission during DMC activity, visible rendering, CHR-space writes,
  or on any mapper other than MMC2;
- joins adjacent proven-safe CPU instructions and PPUDATA stores inside the
  same bounded stream span;
- coalesces repeated invalidation of already-empty presentation caches; and
- skips framebuffer transfer and software scaling for unchanged black frames.

## Automated results

| Gate | Result |
| --- | --- |
| Standard-library suite | 192 passed |
| Mapper 9 blank stream | Optimized and synchronized CPU/PPU/APU/bus state match exactly |
| DMC and device timing | Existing DMA, conflict, IRQ, and scheduler differential tests pass |
| Worker video path | Audio-only packet avoids framebuffer transfer |
| Presentation path | Repeated unchanged frame scales once and reuses the result |
| Shutdown | Bounded worker stops cleanly |

## Same-host comparison

The packaged releases and patched source were run in fresh consoles against
the same original synthetic Mapper 9 cartridge. It leaves rendering disabled,
starts a pulse-channel bell, and streams changing nametable bytes through
PPUDATA. It contains no commercial code, graphics, or audio data.

| Build | Median first-frame time | Relative to 2.0.2 |
| --- | ---: | ---: |
| Packaged 2.0.2 | 79.562 ms | baseline |
| Packaged 1.1.1 | 37.668 ms | -52.7% |
| 2.0.3 source | 14.188 ms | -82.2% |

These are host-local regression measurements, not performance promises. The
exact-state differential test is the correctness gate.

## New diagnostics

The final diagnostics line now includes:

- `unchanged-video-frames`
- `ppu-cache-invalidations`
- `ppu-cache-coalesces`
- `ppu-deferred-data-writes`
- `cpu-ppu-stream-batches`

During the affected black-screen transition, the last three relevant counters
should become nonzero while audio underruns and frame-queue starvation remain
zero.

The final Windows build should still be tested through the first-fight
transition with WASAPI enabled, then through visible gameplay, save/restore,
rewind, controller hot-plug, fullscreen changes, and clean shutdown.
