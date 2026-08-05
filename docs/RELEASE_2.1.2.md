# Cartaconda 2.1.2 validation record

Date: 2026-07-29

## Scope

This patch addresses the remaining Mapper 9 slowdown and audio starvation
reported against 2.1.1. The supplied Mike Tyson's Punch-Out!! Rev 1 image was
used only as an authorized local profiling and differential input. It is not
present in source, tests, wheels, source distributions, or release archives.

## Reproduced root cause

The reported 1,995-frame run averaged 22.071 ms of emulation and 12.625 ms of
host drawing. It recorded 36,613 DMC fetches, 84,169 device flushes, 37,318
stall spans, 805 empty-frame-queue waits, and 97 host-audio underruns.

The dominant `$AF06` span is:

```text
LDA $1F
BNE $AF06
```

The RAM value is unchanged until an interrupt handler runs. Version 2.1.1
nevertheless treated every DMC sample-buffer request as a separate scheduler
deadline. Each request repeatedly crossed the CPU, bus, APU, PPU, and console
dispatchers, then advanced the same four held clocks. That conservative path
preserved timing but discarded the central benefit of the already-proven RAM
poll batch.

The draw log exposed a second independent cost. The portable path converted
the 256x240 RGB frame, scaled it to the complete window in software, blitted
the large result, and then asked the Windows display driver to present it.

## Changes

### Cycle-accounted DMC RAM polling

A Mapper 9 RAM wait may now cross several DMC fetches in one operation when
all of these facts are already proven:

- the mapped code classifier recognizes a RAM load, compare, or `BIT` followed
  by a still-taken branch to itself;
- the DMC is active but cannot assert its IRQ;
- no OAM DMA, DMC hold, NMI, sampled IRQ, or unmasked IRQ is pending; and
- the operation ends before the nearest PPU/NMI or APU-frame event.

The 6502 advances only through its loop cycles. Each DMC read separately adds
four physical clocks to the APU, PPU, bus clock, and sample stream. The batch
therefore removes Python scheduler entries rather than emulated activity.
Writes, IRQ-enabled DMC, changing RAM, and boundary cases stay literal.

### PPU retained lines and MMC2 content caching

The PPU framebuffer persists between frames. A line whose complete visible
key is unchanged now remains in place, while its recorded sprite-hit result
and MMC2 outgoing latch state are replayed. A changed key follows the ordinary
renderer.

MMC2 tile runs are keyed by the full packed nametable/attribute fetch plan,
PPUMASK/palette state, all four FD/FE CHR bank bases, and both incoming
latches. Immutable matching rows can survive unrelated presentation-cache
invalidations. CHR-RAM writes clear the cache explicitly.

### APU hot path

Envelope, pulse, triangle, noise, and DMC objects use fixed slots. Their
save-state dictionaries remain explicit and stable. The active-DMC event loop
calculates its next fetch inline, and the two pulse timer advances are
unrolled. Generated PCM and serialized state remain bit-identical to 2.1.1.

### Accelerated Windows presentation

On Windows, Cartaconda first attempts an accelerated SDL Renderer with:

- one streaming 256x240 game texture;
- nearest-neighbor texture sampling;
- one native-resolution upload only when the frame changes; and
- GPU scaling into the existing aspect-correct letterbox rectangle.

Menus use a separate composed texture because they are not emulation-critical.
Pygame/SDL objects remain on the main thread. Textures are released before
their renderer, the renderer before its window, and the remaining display
subsystem afterward. If no accelerated driver can be created, Cartaconda
automatically uses the 2.1.1 Surface path. Setting
`CARTACONDA_SOFTWARE_DISPLAY=1` forces that fallback for diagnosis.

Startup diagnostics identify the selected route with `display-backend` and
`display-driver`. Final diagnostics add `ppu-scanline-replays`,
`cpu-dmc-poll-batches`, and `cpu-dmc-poll-fetches`.

## Correctness gates

- All 204 standard-library tests pass.
- A new synthetic Mapper 9 test runs the DMC wait both through the optimized
  scheduler and instruction-by-instruction reference for three frames. Every
  CPU, PPU, APU, bus, RAM, mapper, and framebuffer field agrees while the
  optimized path uses fewer than 100 CPU dispatches and batches more than 100
  DMC fetches.
- A retained-line test proves exact reuse and forces a miss after a
  render-visible nametable write.
- A presenter test proves that two draws of one unchanged frame perform one
  native texture upload, one scaled texture draw, and no software display
  flip.
- Across 300 consecutive DMC-heavy supplied-ROM frames, 2.1.2 and the packaged
  2.1.1 source agree at every serialized frame boundary. Aggregate PCM, frame,
  and final-state SHA-256 values also match exactly:

  - PCM: `24687aee43488ce174bc3df118dd8babd1404a29a7fbb7fe1f61d6639c5b132d`
  - frames: `92925d46a5d76fb41e5e2ccd17196347b3a6901dc886ec0ab08643fa896a5b82`
  - final state: `9c70941c251291a6a24d493735bb53d3f9ba24c0368f6a2096fdb623444e9e39`

## Performance

Five independent 180-frame runs began at the same active-DMC supplied-ROM
state and drained host PCM exactly as the frontend worker does:

| Core | Average | Range |
|---|---:|---:|
| 2.1.1 reference | 9.160 ms/frame | 9.073–9.283 ms |
| 1.1.1 reference | 7.651 ms/frame | 7.608–7.685 ms |
| 2.1.2 | 6.783 ms/frame | 6.738–6.811 ms |

Version 2.1.2 is about 26% faster than 2.1.1 and 11% faster than the previously
smooth 1.1.1 core in this captured interval. With diagnostic hot-span
collection enabled, 2.1.2 averaged 7.486 ms versus 9.929 ms for 2.1.1.

A separate 900-frame supplied-ROM continuation recorded:

- 6.891 ms average;
- 11.375 ms p95 and 12.770 ms p99;
- 19.837 ms maximum;
- 12,605 DMC fetches handled inside 982 DMC poll batches; and
- 139,453 retained scanlines out of 216,000 rendered scanline slots.

Three 300-frame exact-NTSC worker/rewind soaks completed as follows:

| Workload | Average | p95 | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|
| NROM | 8.332 ms | 8.720 ms | 0 | 0 |
| Mapper 9 / MMC2 | 9.731 ms | 15.619 ms | 0 | 0 |
| Mapper 4 / MMC3 | 11.403 ms | 12.803 ms | 0 | 0 |

All reached queue high-water two, reported no rewind failure, and shut down
cleanly. These core and virtual-audio measurements do not substitute for a
target Windows Direct3D/WASAPI/controller smoke test. The fallback path remains
available if a particular renderer or graphics driver behaves incorrectly.

## Dependency decision

Pygame-ce remains the only runtime dependency. Its SDL Renderer and streaming
Texture facilities already provide GPU upload, nearest-neighbor scaling, and
presentation on supported Windows drivers. Adding NumPy would not accelerate
the branch-heavy CPU/device scheduler, while a JIT/compiler dependency does
not currently match the supported Python 3.14 frozen-build target. This patch
therefore applies native acceleration only at the measured bulk-pixel boundary
and keeps deterministic hardware state in Python.
