# Cartaconda 2.0.5 validation record

Date: 2026-07-28

## Scope

This release fixes the remaining startup bell/audio delay in Mike Tyson's
Punch-Out!! Rev 1 (Mapper 9/MMC2). The supplied ROM was used only for local
reproduction and differential validation. It is not included in source,
packages, tests, or release archives.

## Root cause

The black-screen bell is software-driven sampled audio. The game spends most
of each frame decoding two-bit source values, running calibrated delay work,
and writing each resulting sample directly to the `$4011` DAC. Earlier PPU
stream optimizations were active but could not remove this CPU interpreter
cost.

## Correctness

- 230 consecutive supplied-ROM frames matched the literal optimized-disabled
  control for CPU registers/cycles, all CPU RAM, bus state, mapper state, PPU
  state, APU state, generated PCM, and framebuffer pixels.
- A seven-cycle admission margin preserves the literal instruction boundary
  at frame/NMI edges.
- DMC activity, pending interrupts, unrecognized code, compressed-stream
  transitions, and short device deadlines retain literal execution.
- Original synthetic Mapper 9 and PPU-status-loop tests protect both new
  optimizers without including commercial game content.

## Performance

Headless CPython validation on the supplied Rev 1 image:

| Interval | Before | 2.0.5 |
|---|---:|---:|
| Black-screen bell average | about 12.1 ms/frame | about 6.9 ms/frame |
| Simple startup `$2002` wait | about 44 ms | about 2 ms |
| Sustained bell budget misses | present on slower hosts | 0 on validation runs |

Host timings vary. Correctness is established by differential state/PCM
matching and the deterministic test suite, not by a single timing result.

## Test gate

- Standard-library deterministic suite: 196 tests.
- Exact supplied-ROM differential: 230 frames.
- Release archive contains only the wheel, source distribution, and checksum
  manifest.
