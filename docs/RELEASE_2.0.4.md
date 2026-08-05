# Cartaconda 2.0.4 validation record

This patch targets the remaining Mapper 9 first-fight slowdown reported with
2.0.3.

## Verified cause

The real 2.0.3 run recorded 2,522 deferred PPUDATA writes across 224 frames,
but still averaged 19.741 ms of worker CPU time. It also recorded 207
unchanged video frames while the Windows SDL path continued to spend 8.931 ms
per presentation. The deferred-write optimization was active, but those
numbers prove it was not the dominant workload.

The startup scene decoder repeatedly reads command bytes with dynamic 6502
indirect addressing. Static classification could not prove those accesses
safe, so 2.0 scheduled them instruction by instruction even when the resolved
address was immutable PRG ROM. Separately, re-presenting the same black image
continued to consume main-thread SDL time.

## Corrected paths

- Official `(d,X)` and `(d),Y` accesses are resolved at the scheduler
  boundary. Only real and dummy accesses wholly inside CPU RAM or immutable
  PRG ROM are admitted.
- The hot indirect LDA stream forms execute directly inside the bounded CPU
  span.
- DMC-active indirect accesses remain literal so DMA conflict timing is not
  approximated.
- Unchanged gameplay pixels and unchanged toast state skip scale, blit, and
  display flip while emulation, audio, input, events, and NTSC pacing continue.
- Expose, display, view, video, and toast changes force presentation.

## Automated results

| Gate | Result |
| --- | --- |
| Standard-library suite | 194 passed |
| Mapper 9 indirect stream | Optimized and literal complete machine state match |
| DMC overlap | Dynamic indirect admission is disabled during active DMC playback |
| Existing CPU/APU/PPU/mapper/state suite | Passed |
| Source compilation | Passed |

The original Mapper 9 indirect-stream regression cartridge collapses to fewer
than 100 top-level CPU dispatches for a complete frame and retains identical
CPU, PPU, APU, bus, mapper, RAM, and controller state.

## Diagnostic handoff

The final line now reports cycle-weighted `cpu-hot-spans` plus literal,
safe/poll/counter batch, device flush, stall, DMC fetch, and unchanged-display
counts. These fields make a remaining ROM-specific bottleneck identifiable
without guessing from a Python traceback.

The Windows build still requires a through-the-first-fight smoke test on the
affected host. The expected outcome is lower worker CPU time, no repeated SDL
presentation cost during the black interval, zero audio underruns, and no
frame-queue starvation.
