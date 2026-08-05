# Cartaconda 2.0.6 validation record

Date: 2026-07-29

## Scope

This release removes the remaining Python scheduler overhead from the
CPU-driven sampled-audio sequence in Mike Tyson's Punch-Out!! Rev 1. The
supplied ROM was used only for local differential validation and is excluded
from source, tests, distributions, and release archives.

## Change

Version 2.0.5 preserved each `$4011` write correctly but returned to the Python
scheduler after every sample. Version 2.0.6 produces a bounded sequence of
timestamped DAC events in one CPU dispatch. APU and PPU clocks still advance
between every event, and every DAC value is applied at its original cycle.
The common non-wrapping compressed-byte refill is included; uncommon pointer
wraps and command transitions retain literal execution.

## Correctness

- 400 consecutive supplied-ROM frames match the literal path for CPU
  registers/cycles, all RAM and bus state, mapper state, PPU state, APU state,
  generated PCM, and framebuffer pixels.
- Every machine-code region used by the fast path is signature checked.
- Frame/NMI admission retains a seven-cycle safety margin.
- DMC activity, pending interrupts, short deadlines, pointer wraps, command
  boundaries, and unrecognized code remain literal.
- The deterministic standard-library suite contains 196 passing tests.

## Performance

Diagnostic CPython validation on the supplied Rev 1 image:

| Metric | 2.0.5 | 2.0.6 |
|---|---:|---:|
| Bell average | about 6.9 ms/frame | about 4.8 ms/frame |
| PCM scheduler batches | one per optimized sample | 940 over 400 frames |
| Bell frame-budget misses | 0 | 0 |

Host timings vary. Exact differential state and PCM matching are the release
correctness gate.
