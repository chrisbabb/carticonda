# Cartaconda 2.1.3 validation record

Date: 2026-07-30

## Scope

This patch targets the remaining slow-computer headroom and the crackle/pop
reported after sampled sounds. The supplied Mike Tyson's Punch-Out!! Rev 1
image was used only as an authorized local profiling and differential input.
It is not present in source, tests, wheels, source distributions, or release
archives.

The field run already showed that native presentation was no longer the
bottleneck:

- 10.431 ms average, 17.250 ms p95, and 87.914 ms maximum emulation time;
- 0.407 ms average draw time;
- 13 worker queue starves across 7,371 frames;
- 1,059,717 retained-line replays out of 1,711,440 scanline slots; and
- two host-audio underruns.

The average was close to real time, but a rare Windows scheduling pause could
still exhaust audio that SDL_mixer had already accepted. The PPU also
discarded useful retained lines whenever any nametable byte changed.

## Changes

### Row-specific MMC2 retained frames

Version 2.1.2 keyed every retained MMC2 line with one global PPU-world
generation. A single tile update advanced that generation and made all 240
lines miss, even when the changed tile row was elsewhere on screen.

Version 2.1.3 tracks:

- 30 tile-row versions for each physical nametable;
- eight attribute-row versions for each physical nametable;
- CHR and conservative fallback generations; and
- the mirroring and horizontal viewport needed to select the two physical
  rows fetched by MMC2's ordered 33-tile scanline.

Palette, scroll, PPUCTRL, PPUMASK, mapper register, sprite, and incoming latch
state remain explicit replay dependencies. Unknown render-visible changes
advance the conservative fallback generation. The optimization therefore
retains only lines whose complete source data is unchanged.

The immutable 32-byte palette signature and MMC2 PPU register token are cached
as well, avoiding repeated allocation at every scanline.

### Native PCM coverage

Pygame exposes one playing and one queued `Sound` per mixer channel. The old
1,024-sample blocks covered only about 46 ms in those two native slots at
44.1 kHz, less than the reported 87.914 ms scheduler outlier.

Cartaconda now submits 2,048-sample blocks. Playing plus queued audio covers
about 93 ms. Playback still waits for exactly two blocks, so the startup
reservoir remains 4,096 samples and no extra startup latency is introduced.
Fewer, larger native `Sound` objects also halve steady-state mixer submission
churn.

### NES analog output chain

The nonlinear pulse/TND mixer now feeds the three documented first-order
stages:

1. 90 Hz high-pass;
2. 440 Hz high-pass; and
3. 14 kHz low-pass.

The implementation derives bilinear-transform coefficients once per host
sample rate and keeps each recurrence inline in the existing scalar mixer.
The high-pass stages remove residual DC after `$4011` sampled playback, while
the low-pass stage softens abrupt DAC transitions. New filter history is
serialized; 2.1.2 and older states seed the added stages from their previous
output so restoring an old state does not manufacture a full-scale edge.

The frequencies follow the [NESdev APU mixer
specification](https://www.nesdev.org/wiki/APU_Mixer).

## Correctness gates

- All 208 standard-library tests pass.
- A new retained-line regression proves that a write to an unrelated
  nametable row replays the existing line and a write to the fetched row
  forces composition.
- Audio regressions prove abrupt-edge attenuation, DC settling, safe migration
  from an older state, unchanged 4,096-sample startup buffering, and the new
  2,048-sample native block size.
- Across 600 consecutive supplied-ROM active-DMC frames, 2.1.3 and the
  packaged 2.1.2 source have identical aggregate pixels and identical final
  hardware state after excluding only output-filter history:

  - frames:
    `a6eed4f5fed3695b2527adc8dd927f3534ab4f3ec259f40dae96b0024a6270a2`
  - normalized final state:
    `634fc09e68904870fbe4dc2fb24b814539a3ef2d4304758f27d4dd37b32bd4b3`
  - new filtered PCM:
    `cff9f3eba94d4444aa6fad1a7d4fe0323b4b79232d43ae41f73649404a8c1789`

The PCM hash intentionally changes because the missing analog stages are now
present. CPU, PPU, mapper, RAM, bus, channel, timer, and framebuffer state do
not.

## Performance

Four independent 300-frame runs started from captured supplied-ROM states:

| Core/checkpoint | Average | Range |
|---|---:|---:|
| 2.1.2 active DMC | 7.299 ms/frame | 7.159–7.482 ms |
| 2.1.3 active DMC | 6.643 ms/frame | 6.557–6.705 ms |
| 2.1.2 normal fight | 6.573 ms/frame | 6.343–6.797 ms |
| 2.1.3 normal fight | 5.921 ms/frame | 5.853–6.066 ms |

The active-DMC checkpoint is about 9% faster and the normal first-fight
checkpoint about 10% faster. A separate 900-frame continuation recorded:

- 5.940 ms average;
- 8.870 ms p95 and 9.697 ms p99;
- 14.686 ms maximum; and
- 198,227 retained lines out of 230,400 scanline slots (86.04%).

Three 300-frame strict exact-NTSC worker/rewind soaks were deliberately run
concurrently to add host contention:

| Workload | Average | p95 | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|
| NROM | 8.673 ms | 10.146 ms | 0 | 0 |
| Mapper 9 / MMC2 | 10.278 ms | 16.772 ms | 0 | 0 |
| Mapper 4 / MMC3 | 11.255 ms | 12.325 ms | 0 | 0 |

All reached queue high-water two, retained at least 3,610 PCM samples in the
virtual reservoir, reported no rewind failure, and shut down cleanly.

The result leaves substantial headroom under NTSC's approximately 16.64 ms
frame interval on the validation host. It does not replace a target Windows
Direct3D/WASAPI/controller smoke test.

## Audio signal checks

Across the same 300-frame active-DMC interval:

| Signal measure | 2.1.2 | 2.1.3 |
|---|---:|---:|
| Largest adjacent-sample edge | 6,185 | 3,264 |
| 99.9th percentile edge | 3,158 | 1,673 |
| PCM mean | -3.819 | 0.105 |
| Clipped samples | 0 | 0 |

The edge and DC reductions directly target clicks and post-sound thumps.
Subjective listening on the affected Windows/WASAPI system remains the final
acceptance check.

## Dependency decision

Pygame-ce remains the only runtime dependency. Profiling still places the
remaining work in branch-heavy, mutable CPU/APU/PPU control flow. NumPy would
add conversion and distribution cost at these small scalar boundaries, and
the supported Python 3.14/frozen target does not justify making a JIT compiler
a runtime requirement. This patch instead reduces work before it reaches
Python and uses SDL's existing native queue more effectively.
