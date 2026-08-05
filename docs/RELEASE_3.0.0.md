# Cartaconda 3.0.0 validation record

Date: 2026-08-04

## Scope

Version 3 fixes the host-audio discontinuity reported during side-scrolling
Mapper 4 gameplay and removes two remaining sources of producer-frame bursts.
The changes are general to SDL_mixer transport, retained PPU rows, and rewind;
there is no title-specific patch.

The supplied 2.2.8 diagnostic contained the key distinction:

- core time averaged 11.070 ms and the worker queue starved 64 times in 3,653
  frames;
- the 4,096-sample prebuffer did not resynchronize and native starts did not
  fail; but
- the frontend counted 318 underruns, emitted 278 concealment Sounds, and
  applied 318 recovery fades.

Those counters were not consistent with PCM production alone. The frontend
was turning short SDL_mixer handoffs into real interruptions.

## Lossless host-audio transport

Pygame exposes one playing Sound and one queued Sound per mixer channel. In
2.2.8, the pump inspected `get_busy()` and then chose a separate start or queue
operation. SDL could promote its queued Sound between those calls. One poll
could consequently observe `busy=False` while a valid Sound was still owned by
the native queue; the recovery path then stopped or replaced that PCM.

Version 3 uses `Channel.queue()` for every submission. The operation fills the
next slot when playback is active and starts immediately when it is idle. The
pump therefore has no check-then-start channel selection and does not call
either play API during an uninterrupted stream.

Additional safeguards are explicit:

- `busy=False` with a queued Sound is treated as a native handoff and left
  untouched;
- idle plus an empty queue must remain stable for 12 ms before it is a real
  underrun;
- a failed Sound construction or queue submission retains the exact staged
  chunk and retries on the next two-millisecond service point;
- a confirmed gap rebuilds the ordinary prebuffer and fades only the first
  recovered emulated PCM—no late synthetic Sound is inserted; and
- requested gain is attached to each retained Sound while the dedicated
  channel remains at neutral volume.

`audio-handoff-waits` and `audio-grace-restarts` are healthy lossless events.
Only `audio-underruns` denotes a confirmed gap. `audio-concealments` remains in
the log for comparison with 2.2.8 and stays zero in version 3.

## Mapper 4 tail control

The content-addressed 512-pixel background cache previously cleared all 1,024
entries when full. Continuous scrolling plus sparse nametable versions could
therefore discard every hot level row in one frame and make a later frame
decode 15,360 tiles again.

Version 3 uses a 2,048-entry `OrderedDict` LRU. It evicts one stale row at a
time, starts recency mutations only near the capacity bound, and never performs
a whole-cache reset. A newly recurring mapper/palette state admits one third
of its rows on the first appearance and all remaining misses on the second.
The first pass uses the exact 33-tile viewport renderer for deferred lines.
Both passes stay similar in cost, while the third pass required by an equal
three-phase scheme is avoided.

Every cache key still contains mirroring, pattern-table selection, effective
MMC3 CHR mapping, exact nametable/attribute-row versions, palette, mask, fine
row, and CHR generation. The cache is presentation-only and excluded from save
states. The final framebuffer hash for the 1,200-frame combined field workload
remains:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

## Nonblocking rewind compression

Snapshot ownership remains strict: only the emulation worker may inspect the
running console. It now calls `get_state()` and protocol-5 pickle on that owner
thread, producing immutable bytes, then publishes video and PCM before handing
those bytes to `CartacondaRewindCompressor`. The convenience thread performs
zlib level-1 compression and appends to the bounded ring.

The compression FIFO is bounded to two jobs. If it is full, rewind capture is
deferred rather than slowing emulation. Worker stop places a sentinel after all
accepted jobs and joins the compressor before save, restore, ROM replacement,
or shutdown can access console state. Compression failure disables rewind but
does not terminate emulation, audio, or SDL.

On the Mapper 4 field state used for profiling, state capture plus pickle is
about 0.11 ms while zlib was about 2.4–2.7 ms on the validation host. Version 3
removes that compression cost from the producer's critical path.

## Correctness gates

- All 236 standard-library tests pass.
- Audio transport submits 808 sequential signed-16 samples in 202 ordered
  chunks while repeatedly injecting the reported `busy=False`/queued handoff.
  The result has no missing or reordered sample, no channel stop, and no false
  underrun.
- Native queue failure retains PCM and retries without waiting for another
  four-chunk prebuffer.
- Rewind tests prove compression executes on
  `CartacondaRewindCompressor`, restores an earlier complete frame, enforces
  cartridge identity, bounds memory, and drains accepted work at shutdown.
- PPU tests cover cache invalidation, mapper/palette dependency isolation,
  exact retained rows, and one-at-a-time LRU eviction.
- Existing CPU opcode/interrupt/DMA/DMC, APU, PPU, mapper, controller,
  save-state, ROM/ZIP, presentation, gamepad-remapping, and UI tests remain
  green.
- No commercial ROM, save state, screenshot, audio, or derived cartridge
  content is present in the source, wheel, sdist, or release archive.

## Exact-NTSC worker gate

The final field soak uses the real two-frame worker, 60.0988 Hz consumer,
44.1 kHz virtual PCM drain, 4,096-sample startup reservoir, asynchronous rewind
compression, and orderly shutdown for 1,200 frames:

| Average | p95 | p99 | Maximum | Queue starves | Audio underruns | Minimum PCM reserve |
|---:|---:|---:|---:|---:|---:|---:|
| 7.936 ms | 12.205 ms | 19.806 ms | 44.556 ms | 0 | 0 | 3,610 samples |

The run reaches queue high-water two, captures 145 rewind snapshots, reports
no rewind failure or compression-queue deferral, and shuts down cleanly. The
maximum is cold frame 1 and is absorbed by the initial queue; no completed
presentation or audio chunk is discarded.

A separately supplied Mapper 9 cartridge image was also executed locally for
1,200 core frames. It averaged 5.506 ms with an 8.613 ms p95 and 15.935 ms p99,
and emitted the expected fractional-NTSC 674–734 samples per frame. This is a
private compatibility check only; the image and all derived state remain
outside the release.

## Windows acceptance request

The release artifacts are platform-independent Python packages. The target
Windows/WASAPI/Direct3D machine should still run the affected scrolling section
for at least 60 seconds:

```powershell
cartaconda game.nes --diagnostics
```

Confirm the startup line contains `Cartaconda=3.0.0` and
`audio-start=channel-queue`. In the final line, compare
`frame-queue-starves`, `audio-underruns`, `audio-handoff-waits`,
`audio-grace-restarts`, `audio-start-failures`, the PPU world-cache fields, and
`rewind-compression-deferrals`. Handoff waits may rise and are expected; an
underrun should not accompany them. A native binary still requires a long
WASAPI/controller/fullscreen qualification on the exact distribution machine.

