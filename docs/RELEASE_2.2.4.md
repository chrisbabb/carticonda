# Cartaconda 2.2.4 validation record

Date: 2026-08-03

## Scope

This patch addresses the remaining Mega Man 5 slowdown reported from
Cartaconda 2.2.3. The supplied 4,776-frame Windows run spent only 0.458 ms per
presentation but 15.834 ms of worker CPU per emulated frame. Its 34.000 ms p95,
728 worker-queue starvations, and 480 audio underruns therefore remained in the
core.

The decisive signal was 268 PPU mapper invalidations: one every 17.82 frames.
That cadence aligns with the periodic tail rather than the steady frames. The
complete MMC3 CHR mapping was part of every retained background key, so a bank
write used only by sprites could rebuild an unchanged scrolling background.

No commercial ROM, title-specific patch, ROM hash, game data, or code from
another emulator is included in or required by this change.

## Granular Mapper 4 render dependencies

The mapper boundary can now expose the effective CHR mapping for one 4 KiB
pattern table. MMC3 precomputes two immutable tokens, each containing the four
complete 1 KiB bank indices selected for that half. Complete indices avoid an
alias on large NES 2.0 CHR images.

The PPU tracks separate background and sprite dependencies:

- background includes its selected pattern table, effective four-bank token,
  and mirroring;
- 8x8 sprites include their selected table and token;
- 8x16 sprites include both tokens because the tile number selects the table.

When only the sprite dependency changes, the PPU keeps the 512-pixel world-row
cache, viewport backgrounds, nametable row versions, OAM scanline index, and
overflow plan. It discards composed sprite pixels and the sprite-zero overlap
plan. A retained line with no selected sprites can replay directly through the
write. A background or mirroring change retains the conservative viewport
invalidation path, while mappers without a granular token keep their complete
render token.

Retained-line keys use compact background and sprite mapper generations. The
sprite generation participates only when sprites are enabled and that line has
a selected sprite. These are host-only caches and generations; save-state
format and emulated hardware state are unchanged.

## Bounded CPU working set

The pure-Python translated-block cache is raised from 2,048 to 4,096 functions.
At capacity it keeps the established working set instead of clearing every
compiled block and causing a recompilation wave. A new start continues through
the existing optimized safe dispatcher. The cache remains derived only from
immutable, scheduler-proven RAM/PRG code and remains absent from save states.

## Differential correctness gates

- A mapper test changes only the sprite-side token and verifies that populated
  background/world caches survive while output matches conservative full
  invalidation.
- Empty-sprite retained lines replay through a sprite-bank change; visible
  sprite pixels are recomposed and compared exactly.
- Independent lower-bank, upper-bank, and mirroring changes run for 360 frames
  against a candidate forced through full invalidation. Every framebuffer and
  complete serialized state matches. The aggregate framebuffer SHA-256 is
  `dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`.
- A large-bank token regression proves that distinct NES 2.0 CHR bank numbers
  cannot alias.
- A cache-capacity regression proves that reaching the translated-block bound
  does not discard an established hot function.
- All 223 standard-library tests pass in 11.552 seconds on the validation host.

## Targeted performance gate

The original synthetic workload scrolls a populated Mapper 4 background,
places sparse visible sprites, and changes one sprite-side 1 KiB bank every 18
frames. Both builds run 1,800 frames at 44.1 kHz and finish with the same frame
SHA-256:

`82870f0effe54d69adeb76cf6a9310615b925dd858276c9bf4bacd71d023451b`

| Metric | Packaged 2.2.3 | 2.2.4 | Improvement |
|---|---:|---:|---:|
| All-frame average | 4.874 ms | 4.211 ms | 13.6% lower |
| All-frame p95 | 15.994 ms | 4.889 ms | 3.27x lower |
| All-frame p99 | 18.032 ms | 6.986 ms | 2.58x lower |
| Bank-change average | 16.815 ms | 4.274 ms | 3.93x lower |
| Bank-change p95 | 20.543 ms | 4.825 ms | 4.26x lower |

The candidate records 100 `ppu-mapper-background-preserves` events in the
measured segment. A separate three-run throughput median improves from 204.70
to 254.01 FPS (1.24x). Host timing varies; identical pixels and state are the
correctness gates.

## Threaded exact-NTSC gate

Representative 900-frame profiles use the real two-frame worker queue,
fractional-NTSC consumer, 4,096-sample virtual PCM reservoir, compressed
rewind captures, and orderly shutdown:

| Workload | Average | p95 | p99 | Maximum | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|---:|---:|
| NROM | 4.555 ms | 5.465 ms | 8.124 ms | 17.905 ms | 0 | 0 |
| Mapper 9 | 4.931 ms | 7.869 ms | 11.391 ms | 17.049 ms | 0 | 0 |
| Mapper 4 | 4.817 ms | 5.699 ms | 8.698 ms | 27.757 ms | 0 | 0 |
| Mapper 4 active IRQ | 4.098 ms | 5.080 ms | 7.145 ms | 11.276 ms | 0 | 0 |

Every row retains PCM, reports no rewind failure, and shuts down cleanly. The
shared validation host also produced intermittent 40–165 ms OS scheduling
pauses in repeat runs; those repeats drained the two-frame virtual reservoir
despite 4–6 ms average core times. They are host-tail observations, not hidden
core results, and are why the reporter's Windows run remains essential.

## Field-validation request

Re-run the affected section with `--diagnostics`. Compare emulation p95/p99,
budget misses, frame-queue starvation, and audio underruns. The new
`ppu-mapper-background-preserves` field should rise when Mapper 4 changes only
sprite-side CHR; those events should no longer produce a matching p95 spike.
`cpu-translated-blocks` should warm and remain bounded.

No ROM image, save state, commercial asset, or title-specific data is present
in the source, wheel, sdist, or release archive.
