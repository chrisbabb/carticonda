# Validation strategy

The project deliberately separates validation into layers so a graphical
failure does not have to be debugged as one indivisible console.

## Layer 1: deterministic unit tests

The included standard-library tests verify:

- CPU reset vectors, addressing, arithmetic, flags, stack order, page-cycle
  penalties, interrupts, indirect-wrap behavior, and unofficial operations;
- controller latch and serial order;
- cartridge header parsing and mapper bank wiring, including NES 2.0
  exponent/multiplier sizes, separate volatile/NVRAM storage, legacy-header
  recovery, MMC2/MMC4 delayed FD/FE CHR latches, discrete mapper bank wiring,
  mirroring, save-state restoration, side-effect-aware fast rendering, and ZIP
  loading;
- PPU memory mirroring, palette aliases, register latches, buffered reads,
  VBlank/NMI suppression, exact sprite-zero event time, and the secondary-OAM
  overflow scan including its diagonal increment bug;
- fast forced-blank vblank set and pre-render clear transitions at their
  cycle-zero entry boundary;
- APU length/status behavior, sample production, DMC stalls, and DMC read
  conflicts with PPU/controller registers;
- batched APU timer state against thousands of single reference clocks,
  including fastest-rate looping DMC reads and their four-clock CPU stalls;
- large APU spans across several frame-sequencer edges against 40,000 literal
  reference clocks;
- ordered CPU-driven DAC events against literal `$4011` writes, including a
  host-sample/write equality boundary and a zero-cycle event;
- deferred APU/PPU device clocks against the instruction-synchronized fast
  scheduler, including CPU device-register traffic;
- direct high-frequency CPU opcode handlers and compact batch classifications
  against the generic decoder;
- PCM backpressure retention, lossless multi-pump chunk order, prebuffer and
  underrun recovery, latency bounds, and stream resets;
- DMA timing and memory transfer, including mirrored-RAM bulk copies, nonzero
  OAMADDR wrap, open bus, and batched-vs-literal held-clock state;
- fast-render cache invalidation, background/sprite separation, repeated-frame
  cache admission, and bounded continuous-scroll retention;
- retained framebuffer scanline replay plus a forced miss after a
  render-visible nametable change;
- optimized idle-loop frames against instruction-by-instruction machine state;
- all four register-counter loop forms against literal execution, including a
  taken branch that crosses a PRG-ROM page;
- NROM RAM-poll loop batching against literal frame state plus a CPU-dispatch
  ceiling for the optimized path;
- Mapper 9 RAM-poll batching across active DMC fetches against a literal
  three-frame reference, including every four-cycle hold and a CPU-dispatch
  ceiling;
- NROM RAM/stack/ROM-only instruction spans against the literal scheduler over
  the action-platformer workload, including final framebuffer and full
  serialized machine state plus a gameplay dispatch ceiling;
- Mapper 9 RAM/stack/mapped-ROM instruction spans across every switchable PRG
  bank against the literal scheduler, including final framebuffer, complete
  serialized state, and a gameplay dispatch ceiling;
- Mapper 9 forced-blank PPUDATA streams against the fully synchronized
  scheduler, including exact CPU/PPU/APU/bus state, deferred-write admission,
  presentation-cache coalescing, and bounded stream dispatch;
- Mapper 4 continuous CHR-register invalidation without repeated scanline
  composition, plus continuous PRG-bank switching that classifies only the
  stable 8 KiB slot containing executed code and matches literal machine
  state;
- Mapper 4 lower/upper pattern-table token isolation, sprite-only CHR-bank
  invalidation with retained background rows, and empty-sprite scanline replay
  against conservative full invalidation;
- active Mapper 4 IRQ prediction against the actual filtered A12/counter edge,
  explicit pre-render fallback, and a fixed-bank cooperative scheduler batch
  against the fully synchronized frame and complete machine state;
- MMC2 fetch-plan and repeated-scanline caches against literal ordered CHR
  reads, including exact incoming/outgoing FD/FE latch-state replay;
- MMC2 PRG-only vs render-visible mapper writes for precise CPU-window refresh
  and PPU-cache invalidation;
- bounded worker input/video/audio transfer, unchanged-video transfer
  suppression, fixed-buffer reuse, maximum
  run-ahead, orderly stop, and main-thread failure propagation;
- flip-targeted presentation lead, exact long-term NTSC deadlines, zero timing
  debt, explicit pause phase reset, automatic system-suspend segmentation,
  jitter measurement, and zero presentation dropping;
- persistent gamepad-source validation, compilation, conflict swapping, reset,
  and round trips;
- immutable copied frames and the frontend's identity-preserving live
  framebuffer path;
- complete state round trips plus wrong-ROM rejection;
- bounded rewind capture/eviction, one- and five-second restoration, ROM
  identity rejection, future-history truncation, worker ownership, and battery
  isolation;
- settings validation, atomic preference round trips, recent-ROM ordering, and
  save-slot/browser inventories;
- launcher CLI parsing with and without a direct ROM path;
- one-ROM ZIP autoload, multi-ROM selection, case-insensitive members, corrupt
  archives, missing ROMs, and expanded-size limits;
- explicit archive-member execution in info and headless modes; and
- native texture presentation with one upload for an unchanged frame and a
  no-hardware fallback that returns to the Surface display path.

## Layer 2: original integration cartridge

`tools/make_demo_rom.py` emits a small legal NROM image from machine
instructions and pattern bytes defined in the script. It:

- initializes CPU RAM and PPU registers;
- waits on PPU status;
- uploads a palette and fills a nametable;
- enables NMI rendering;
- polls controller 1 through `$4016`; and
- changes a palette entry when A is held.

This provides an end-to-end smoke test without relying on copyrighted game
software or another emulator's implementation.

`tools/benchmark.py` runs the same cartridge through the optimized scanline
path, a control with idle batching disabled, an instruction-synchronized
control with device-clock coalescing disabled, and the dot reference renderer.
It also runs an original rendered NROM workload with the DMC looping at its
fastest rate. A second original workload continuously runs game-logic-shaped
zero-page/ALU code while NMI polls input, scrolls, updates tonal audio, and
performs 64-sprite OAM DMA every frame. It prints throughput, real-time factor,
and deterministic frame hashes. `--mmc3-sprite-frames 1800` changes one
sprite-side 1 KiB bank every 18 frames over an unchanged scrolling background,
reproducing the periodic invalidation tail seen in the 2.2.3 field log. Treat
all controls as performance-regression signals rather than relying only on an
idle-heavy workload.

`tools/soak.py` drives the real `EmulationWorker`, two-buffer transfer pool,
two-frame queue, frontend-order frame acceptance, two-millisecond host wakeups,
virtual 4,096-sample PCM prebuffer, rewind capture, and orderly shutdown. Use a
fresh process per workload for release measurements:

```bash
PYTHONPATH=src python tools/soak.py \
  --workload nrom --frames 900 --strict --json build/soak-nrom.json
PYTHONPATH=src python tools/soak.py \
  --workload mmc2 --frames 900 --strict --json build/soak-mmc2.json
PYTHONPATH=src python tools/soak.py \
  --workload mmc3 --frames 900 --strict --json build/soak-mmc3.json
PYTHONPATH=src python tools/soak.py \
  --workload mmc3-irq --frames 900 --strict --json build/soak-mmc3-irq.json
```

The integration suite also assembles an original Mapper 9 image in memory. It
writes all four MMC2 CHR registers and mirroring control from 6502 code, enables
rendering, trips an FD latch from a nametable tile, and verifies a multicolor
frame through the interactive scanline path. No commercial ROM data is needed
for Mapper 9 regression coverage.

The Pygame frontend additionally has a dummy-video smoke pass that renders the
launcher, pause overlay, state manager, settings, controls, browser, and
confirmation modal. That pass exercises live game loading, reload/unload,
state thumbnails, keyboard rebinding, mixer volume, mute, and every configured
window scale without requiring a physical display. It also renders the
multi-ROM picker and verifies that the selected ZIP member survives reload and
continues using the same save-state identity. The audio pass paces rendered
looping-DMC frames through SDL's dummy mixer while checking that the stream
never enters its explicit underrun path, that borrowed PCM views are released
before staging advances, and that native Sound references are cleared on
stream reset. Branding validation checks the exact Cartaconda name/tagline,
packaged PNG signature and dimensions, rendered wordmark/mascot surfaces, and
window caption.

## Layer 3: public hardware test ROMs

For deeper accuracy work, run independently authored, freely distributable
hardware diagnostics for:

1. CPU instruction semantics and timing;
2. PPU VBlank/NMI race behavior;
3. sprite-zero hit and overflow;
4. APU frame sequencer and channel units;
5. mapper bank switching and IRQ timing; and
6. controller/DMC conflict behavior.

Do not copy expected traces or implementation code into the emulator. Treat a
failed diagnostic as a hardware observation, isolate the relevant circuit, and
add a small regression test before changing the core.

`tools/accuracy_runner.py` automates diagnostics that implement the common
`$6000` protocol. It waits for signature `$DE $B0 $61`, treats status `$80` as
running, honors status `$81` only after the required 100 ms delay, reads the
zero-terminated diagnostic at `$6004`, and reports terminal status `$00-$7F`.
It scans `.nes` files and ZIP archives recursively without extracting them:

```bash
PYTHONPATH=src python tools/accuracy_runner.py path/to/tests \
  --max-frames 3600 \
  --json build/accuracy.json \
  --junit build/accuracy.xml
```

The cycle/dot path is the default. `--fast` is useful for differential checks,
but a fast-path pass does not replace the dot-path release gate. A timeout
without the signature is reported separately from a signed diagnostic failure.

Older blargg suites write their result only to an ASCII nametable console.
Pass `--screen-console` to recognize an exact `Passed`, `Failed`, or `Error n`
line and preserve all other visible output in the result. CRC-only tests remain
timeouts by design: compare their preserved CRC against the recorded revision-
specific hardware result instead of treating arbitrary screen text as success.

## Layer 4: real-hardware comparison

The final accuracy bar is output captured from an NTSC front-loading or
top-loading NES:

- compare CPU-visible results from diagnostic cartridges;
- compare PPU frames at exact scanline/dot boundaries;
- compare channel waveforms before and after nonlinear mixing; and
- compare input behavior while DMC and OAM DMA are active.

Record console/PPU revision because analog palette and some timing behavior
varies across hardware.

## 3.0.0 release snapshot

The standard-library regression suite contains 236 passing tests. Version 3
uses `Channel.queue()` as the single native PCM transport, recognizes the
queued-Sound promotion window, and requires 12 ms of stable native idle state
before counting an underrun. The long transport regression submits 808 ordered
samples across 202 chunks without loss, reordering, a channel stop, or a false
underrun.

Mapper 4 world rows now use bounded LRU eviction instead of a periodic complete
reset. A cold recurring map admits one third of its rows on its first
appearance and the remainder on its second. Rewind zlib work runs on a bounded
convenience thread after immutable state capture and cannot block the video/PCM
producer.

The final 1,200-frame exact-NTSC combined field soak averages 7.936 ms, with
12.205 ms p95 and 19.806 ms p99. It reaches queue high-water two, records zero
queue starvation and zero virtual audio underruns, keeps at least 3,610 PCM
samples after startup, retains 145 rewind snapshots, and shuts down cleanly.
See [RELEASE_3.0.0.md](RELEASE_3.0.0.md) for the complete gate and Windows
acceptance request.

## 2.2.8 patch snapshot

The standard-library regression suite contains 231 passing tests. Version
2.2.8 bounds optional Python-block generation to four functions in each of the
first two worker-reservoir frames and one function in every gameplay frame.
MMC3 cross-slot spans enter only already-compiled destinations; a cold target
returns to the generic safe scheduler. Differential tests verify identical
pixels and complete serialized state against unchained/budgeted controls.

A 1,024-start compile-burst regression reproduces 2.2.7's host-code-generation
tail. Across three fresh processes, packaged 2.2.7 and 2.2.8 measure:

| Metric | Packaged 2.2.7 | 2.2.8 | Improvement |
|---|---:|---:|---:|
| Median average | 29.434 ms | 2.468 ms | 91.6% lower |
| Median p99 | 42.933 ms | 8.257 ms | 80.8% lower |
| Blocks generated in 64 frames | 1,024 | 70 | Deterministically spread |

Across three alternating 1,200-frame combined MMC3 field runs, packaged 2.2.6
falls from a 7.016 s median to 6.254 s in 2.2.8 (10.9% lower, 1.122x
throughput). Both produce framebuffer SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

Three 900-frame exact-NTSC field soaks have median 6.696 ms average, 9.603 ms
p95, and 15.447 ms p99 core time. All three complete with zero queue
starvation, zero audio underruns, intact rewind history, and clean shutdown.
Their maximum occurs on cold frame 2 while the initial worker reservoir is
still filling. See [RELEASE_2.2.8.md](RELEASE_2.2.8.md) for the complete gate.

## 2.2.7 patch snapshot

The standard-library regression suite contains 230 passing tests. Version
2.2.7 retains exactly versioned background world rows across sparse nametable
writes, shares immutable render context across visible-line batches, and
continues a device-safe CPU span through already classified MMC3 helper slots.
The optimized console matches conservative cache invalidation and unchained
CPU controls for complete pixels and serialized machine state.

Across three alternating 900-frame runs, packaged 2.2.6 and 2.2.7 produce the
same final scrolling framebuffer SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

| Metric | Packaged 2.2.6 | 2.2.7 | Improvement |
|---|---:|---:|---:|
| Median elapsed time | 2.874 s | 2.570 s | 10.6% lower |
| Median throughput | 313.2 FPS | 350.2 FPS | 1.118x |
| Safe batches, 900 frames | 34,367 | 24,201 | 29.6% fewer |

The final 900-frame exact-NTSC worker soak averages 3.328 ms/frame with a
3.803 ms p95 and 5.053 ms p99. It completes with zero queue starvation, zero
audio underruns, 149 rewind captures, no rewind failure, and clean shutdown.
Optional block generation and rewind compression consume only buffered
headroom; an actual underrun receives a decay tail plus recovery fade.

## 2.2.6 patch snapshot

The standard-library regression suite contains 229 passing tests. Version
2.2.6 makes MMC3 classification follow execution into a previously unused
stable 8 KiB code slot. The optimized console and an instruction-synchronized
reference execute an original fixed-bank-to-`$C000` scrolling workload with
identical pixels and complete serialized machine state.

Across three alternating 300-frame runs, packaged 2.2.5 and 2.2.6 produce the
same final framebuffer SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

| Metric | Packaged 2.2.5 | 2.2.6 | Improvement |
|---|---:|---:|---:|
| Median frame time | 29.489 ms | 3.138 ms | 89.4% lower |
| Median throughput | 33.91 FPS | 318.64 FPS | 9.40x |
| Literal instructions, 120 frames | 1,223,991 | 773 | 99.94% lower |
| Safe batches, 120 frames | 1,278 | 4,560 | 3.57x |
| Compiled bank/slot tables | 1 | 2 | Both executed slots |

A repeated 900-frame exact-NTSC worker soak averages 3.462 ms/frame with a
4.513 ms p95, zero queue starvation, zero audio underruns, complete rewind
history, and clean shutdown. Exact timings remain host-dependent; the complete
state comparison, framebuffer hash, and unit suite are the correctness gates.

## 2.2.5 patch snapshot

The standard-library regression suite contains 228 passing tests. Version
2.2.5 adds guarded official indirect hot-block translation, tri-state immutable
code classification, arithmetic inaudible-channel clocking, constant-time
MMC3 IRQ projection, complete-visible-line PPU batching, and independent
background/sprite palette dependencies.

The deterministic 1,200-frame Mapper 4 cadence moves sprites every frame and
periodically changes sprite palette plus sprite-side CHR. Packaged 2.2.4 and
2.2.5 produce the same final framebuffer SHA-256:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

Across three alternating runs, median throughput improves from 196.02 to
332.98 FPS (1.70x), reducing average frame time from 5.102 to 3.003 ms. Two
separate 900-frame exact-NTSC worker soaks average 4.246 ms for Mapper 4 and
3.036 ms for active-IRQ Mapper 4; both report zero queue starvation, zero
audio underruns, complete rewind histories, and clean shutdown. Exact timings
remain host-dependent; the exhaustive CPU/APU/PPU differential tests and
pixel hash are the correctness gates.

## 2.2.4 patch snapshot

The standard-library regression suite contains 223 passing tests. Version
2.2.4 adds independent Mapper 4 background/sprite pattern dependencies,
line-local sprite-bank replay keys, a conservative large-CHR token test, and a
translated-cache capacity regression.

A 360-frame synthetic Mapper 4 scene independently changes the upper/sprite
CHR half every 17 frames, the lower/background half every 53 frames, and
mirroring every 79 frames. The optimized cache and a candidate forced through
conservative full invalidation produce identical pixels and complete
serialized machine state for every frame. The aggregate framebuffer SHA-256
is:

`dd493585bde88d5307e760b29cdd3a721ad9fa8fa5cb16618c77889c0d6be401`

The deterministic 1,800-frame sprite-bank cadence produces the same final
frame SHA-256 in packaged 2.2.3 and 2.2.4:

`82870f0effe54d69adeb76cf6a9310615b925dd858276c9bf4bacd71d023451b`

On the validation host, one instrumented run gives:

| Metric | Packaged 2.2.3 | 2.2.4 | Change |
|---|---:|---:|---:|
| All-frame average | 4.874 ms | 4.211 ms | 13.6% lower |
| All-frame p95 | 15.994 ms | 4.889 ms | 3.27x lower |
| All-frame p99 | 18.032 ms | 6.986 ms | 2.58x lower |
| Bank-change average | 16.815 ms | 4.274 ms | 3.93x lower |
| Bank-change p95 | 20.543 ms | 4.825 ms | 4.26x lower |

The candidate records 100 background-preserving mapper invalidations and
427,956 retained scanline replays. Exact timings remain host-dependent; the
pixel/state differential and unit suite are the correctness gates. A final
Windows/Direct3D/WASAPI run of the reporter's cartridge dump remains the field
acceptance gate.

## 2.2.0 patch snapshot

The standard-library regression suite contains 211 passing tests. Version
2.2.0 adds exact future MMC3 IRQ-assertion projection, conservative pre-render
edge coverage, immediate executed-bank classification, cached effective MMC3
PPU mapping tokens, and a signature-gated four-record cooperative scheduler
batch.

The original `mmc3-irq` test cartridge combines filtered A12 scanline IRQs
with an independently written fixed-bank cooperative scheduler. It contains no
commercial code, data, graphics, or audio. Across 120 consecutive frames, the
optimized and instruction-synchronized paths produce identical aggregate
pixels and complete serialized state:

- frames: `561261e7d44fcb0b4d2eade10cd9140262030d769a55ad29dde48bc0423745da`
- final state:
  `7d73bd2e8403e90fb85a179be19b35e4f224037b9f90d25596f33f03d658991d`

The benchmark's cooperative loop is discovered from its complete immutable
instruction/control-flow signature. The optimized executor additionally
checks X/Y, all four status values, the reset/status locations, and interrupt
state before it can run. The ready-record path and partial scans remain
literal. Differential coverage compares the optimized core with idle and
device batching both disabled.

Exact-NTSC bounded-worker/rewind measurements on the same host:

| Build | Frames | Average | p95 | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|---:|
| Packaged 2.1.3 | 300 | 20.750 ms | 22.252 ms | 58 | 8 |
| 2.2.0 | 900 | 5.088 ms | 7.792 ms | 0 | 0 |

Version 2.2.0 is 4.08x as fast by average frame time (about 308% more
throughput), while the longer run retains at least 3,610 virtual PCM samples,
reports no rewind failure, and shuts down cleanly. A separate three-run direct
benchmark median fell from 26.675 to 4.830 ms/frame. Diagnostic sampling over
300 frames records about 32.8 device flushes per frame, versus roughly 589 per
frame in the supplied 2.1.3 field log.

These synthetic and differential gates target the exact Mapper 4 failure mode
without redistributing a game image. A final Windows/Direct3D/WASAPI run of the
user's cartridge dump remains the hardware-specific acceptance gate.

## 2.1.3 patch snapshot

The standard-library regression suite contains 208 passing tests. Version
2.1.3 adds row-specific MMC2 retained-line dependencies, cached immutable
palette/mapper signatures, the complete three-stage NES output filter, old
filter-state migration, and a 93 ms native SDL_mixer queue with the same
4,096-sample startup reservoir.

The supplied Mapper 9 Rev 1 image remains a local differential input and is
excluded from every source and release artifact. Across 600 consecutive
active-DMC frames, 2.1.3 and the packaged 2.1.2 source produce the same
aggregate framebuffer SHA-256 and final hardware-state SHA-256 after removing
only the deliberately changed host-output filter history:

- frames: `a6eed4f5fed3695b2527adc8dd927f3534ab4f3ec259f40dae96b0024a6270a2`
- normalized final state:
  `634fc09e68904870fbe4dc2fb24b814539a3ef2d4304758f27d4dd37b32bd4b3`
- 2.1.3 filtered PCM:
  `cff9f3eba94d4444aa6fad1a7d4fe0323b4b79232d43ae41f73649404a8c1789`

Four independent 300-frame runs measured:

| Core/checkpoint | Average | Range |
|---|---:|---:|
| 2.1.2 active DMC | 7.299 ms | 7.159–7.482 ms |
| 2.1.3 active DMC | 6.643 ms | 6.557–6.705 ms |
| 2.1.2 normal fight | 6.573 ms | 6.343–6.797 ms |
| 2.1.3 normal fight | 5.921 ms | 5.853–6.066 ms |

A separate 900-frame continuation averaged 5.940 ms with an 8.870 ms p95,
9.697 ms p99, and 14.686 ms maximum. It replayed 198,227 of 230,400 scanline
slots (86.04%).

Concurrent 300-frame strict exact-NTSC NROM, MMC2, and MMC3 worker/rewind
soaks all reached queue high-water two with zero queue starvation, zero
virtual audio underrun, no rewind failure, and clean shutdown.

On the same 300-frame PCM interval, the complete analog output chain reduced
the largest adjacent-sample edge from 6,185 to 3,264, reduced the 99.9th
percentile edge from 3,158 to 1,673, and moved the sample mean from -3.819 to
0.105. These signal checks complement rather than replace a Windows
WASAPI/headphone listening test.

## 2.1.2 patch snapshot

The standard-library regression suite contains 204 passing tests. Version
2.1.2 adds exact Mapper 9 RAM-wait batching across active DMC requests,
retained-scanline invalidation, native texture upload/presentation, and new
diagnostic-field coverage.

The supplied Mapper 9 Rev 1 image remains a local differential input and is
excluded from every source and release artifact. Across 300 consecutive
DMC-heavy frames, 2.1.2 and 2.1.1 produce identical complete serialized state
at every frame boundary. Aggregate PCM, framebuffer, and final-state SHA-256
values also match.

Five 180-frame captured-state runs measured:

| Core | Average | Range |
|---|---:|---:|
| 2.1.1 | 9.160 ms | 9.073–9.283 ms |
| 1.1.1 | 7.651 ms | 7.608–7.685 ms |
| 2.1.2 | 6.783 ms | 6.738–6.811 ms |

A 900-frame continuation averaged 6.891 ms with an 11.375 ms p95, 12.770 ms
p99, and 19.837 ms maximum. The three 300-frame exact-NTSC release soaks all
reached queue high-water two with zero queue starvation, zero virtual audio
underrun, no rewind failure, and clean shutdown:

| Workload | Average | p95 | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|
| NROM | 8.332 ms | 8.720 ms | 0 | 0 |
| Mapper 9 / MMC2 | 9.731 ms | 15.619 ms | 0 | 0 |
| Mapper 4 / MMC3 | 11.403 ms | 12.803 ms | 0 | 0 |

The Windows SDL texture route cannot be hardware-qualified by the headless
validation host. Its ownership, upload, draw, fallback, and teardown behavior
is covered locally; Direct3D/WASAPI/controller behavior remains an explicit
target-build smoke gate.

## 2.1.1 patch snapshot

The standard-library regression suite contains 201 passing tests. Version
2.1.1 adds exact sprite-zero polling batches, active-DMC and fetch-boundary
coverage, the MMC2 dot-256 fallback boundary, native-format gameplay scaling,
and split draw/flip diagnostics.

The supplied Mapper 9 Rev 1 image remains a local differential input and is
excluded from every source and release archive. Across 300 consecutive
first-fight frames, the optimized and 2.1.0 execution paths produce identical
CPU, PPU, APU/PCM, RAM, bus/open-bus, mapper, and framebuffer state at each
frame boundary.

Four GC-suspended 120-frame runs on the same host measured a median 21.620
ms/frame for the 2.1.0 path and 9.558 ms/frame for 2.1.1 (2.26x). A separate
420-frame exact-NTSC worker run with the real rewind path averaged 9.219 ms,
with a 14.539 ms p95, 15.670 ms p99, zero queue starvation, zero virtual PCM
underrun, 70 rewind captures, and clean shutdown.

Three 300-frame release soaks also completed cleanly:

| Workload | Average | p95 | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|
| NROM | 8.756 ms | 9.361 ms | 0 | 0 |
| Mapper 9 / MMC2 | 9.622 ms | 15.599 ms | 0 | 0 |
| Mapper 4 / MMC3 | 11.218 ms | 12.270 ms | 0 | 0 |

An SDL dummy-display microbenchmark at 1024x960 measured the old 24-bit
scale/converting-blit path at 1.093 ms and the native-format path at 0.490 ms.
This isolates CPU scaling/blitting only; the new `draw-prepare-*` and `flip-*`
diagnostics separate that cost from Windows compositor behavior in field
logs.

## 2.1.0 release snapshot

The standard-library regression suite contains 198 passing tests. The 2.1
additions cover ordered software-DAC events, a mixer sample that lands on the
same cycle as a `$4011` write, zero-cycle DAC application, the signature-gated
Mapper 9 scheduler yield, stream-level forced-blank PPUDATA commits, and the
fast PPU's forced-blank vblank set/clear edges.

The supplied Mapper 9 Rev 1 image was used only as a local differential input
and is excluded from tests and distributions. Across 400 consecutive startup,
bell, and transition frames, the optimized and instruction-synchronized paths
produce identical CPU, PPU, APU/PCM, RAM, bus, mapper, and framebuffer state.
A separate 420-frame exact-NTSC bounded-worker run reached queue high-water 2
with zero queue starvation, zero virtual PCM underrun, and clean shutdown.

On the same validation host, comparing the packaged 2.0.6 wheel with 2.1.0:

| Workload | 2.0.6 | 2.1.0 |
|---|---:|---:|
| Mapper 9 forced-blank PPUDATA/audio | 42.68 FPS | 143.79 FPS |
| Mapper 4 transition gameplay | 86.43 FPS | 95.04 FPS |
| Supplied-ROM bell, five-run median | 4.036 ms/frame | 3.801 ms/frame |

The recorded public hardware-test results below remain the inherited accuracy
gate. External diagnostic ROMs are not bundled, so they must be rerun on the
target release environment when available.

## 2.0 release snapshot

The standard-library regression suite contains 194 tests. It covers the core,
17 supported mapper numbers, UI models, dummy SDL surfaces/audio, keyboard and
gamepad remapping, ZIP loading, save/battery/state handling, rewind, worker
ownership, pacing, and deterministic optimized-vs-reference comparisons.

The recorded public gate contains:

- 35/35 passing CPU tests across branch timing, execution space, interrupts,
  reset, official/unofficial instruction behavior, dummy reads, and timing;
- 24/24 passing PPU tests on the fast path and the same passing families on the
  dot path: one open-bus, eleven sprite-hit, five sprite-overflow, and seven
  VBlank/NMI cases;
- 17/17 terminal APU/reset/DMC tests, plus the two deliberately nonterminal
  DMC read diagnostics producing their exact output rows and CRCs `159A7A8F`
  and `D84F6815`; and
- 10 passing common Sharp/new MMC3 IRQ tests. Two mutually exclusive
  NEC/revision-A diagnostics remain outside the generic iNES Mapper 4 selection
  because those ROMs carry no board-revision metadata.

The final 1,200-frame deterministic benchmark measured 142.4 FPS (2.37x NTSC)
for the NROM gameplay workload, 131.9 FPS (2.19x) for the same workload with
rewind capture enabled, and 125.4 FPS (2.09x) for the Mapper 9 gameplay
workload. Final frame hashes were:

- NROM: `a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c`
- MMC2: `8dd3eb48053a0e82d5ff6d940b14ee3f29f18968f0745a271889f6f43175d22a`

Separate 900-frame, 15-second strict worker soaks reached queue high-water 2
with zero queue starvation, zero virtual PCM underrun, and clean shutdown.
The NROM run averaged 7.67 ms wall / 7.61 ms thread CPU with 8.94 ms wall p95.
The MMC2 run averaged 9.86 ms wall / 9.59 ms thread CPU; its isolated tail was
absorbed by the two-frame queue. These runs validate Python core/worker rates,
not SDL drivers. A target Windows frozen build still requires the documented
WASAPI, controller, display, hot-plug, and long-duration qualification.

## 1.0 release snapshot

The original action-platformer workload was measured in three independent
600-frame optimized runs on the release validation host. Median throughput was
112.6 FPS (1.87× NTSC real time), and all three runs produced frame SHA-256
`a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c`.
The optimized NROM span scheduler was also run beside the same fast scheduler
with span batching disabled for 240 frames; every frame byte and the final
serialized CPU/PPU/APU/bus/controller state matched exactly.

A separate 900-frame, 15-second wall-clock producer/consumer soak used the same
NMI, controller, scrolling, four-channel audio, 64-sprite DMA workload and the
release two-frame worker. It reached queue high-water 2 with zero presentation
starvation and zero virtual audio-reservoir underruns. Core timing was 9.14 ms
average and 10.15 ms p95; one 23.1 ms host/core tail was absorbed by the queue
without a repeated presentation. These measurements validate the pure-Python
pipeline and exact producer/consumer rates; final WASAPI, controller-driver, and
display behavior must still be smoke-tested on the target Windows build.

## 1.1.1 Mapper 9 performance snapshot

The release workload runs game-logic-shaped zero-page and ALU code, polls a
controller, drives tonal audio, performs 64-sprite OAM DMA, changes the
switchable MMC2 PRG bank, scrolls, and renders latch-trigger tiles on every
frame. The unmodified 1.1.0 code completed 300 frames at 31.8 FPS. The 1.1.1
path completed 600 frames at 84.7 FPS (1.41x NTSC real time), a 166.6%
throughput increase on the same validation host.

For the differential control, 240 optimized frames were compared with both
CPU span batching and MMC2 latch replay disabled. Every framebuffer byte and
the final serialized CPU, PPU, APU, bus, mapper, and controller state matched
exactly. A separate 900-frame, 15-second bounded-worker soak reached queue
high-water 2 with zero queue starvations and zero virtual audio-reservoir
underruns. Core timing was 11.85 ms average, 14.27 ms p95, 15.68 ms p99, and
21.20 ms maximum; the two-frame queue absorbed the isolated tail.

A static latch-heavy scene reached 147.0 FPS with exact outgoing latch state,
demonstrating the repeated-scanline replay path without making that
best-case workload the primary performance claim. Final WASAPI, controller,
and display behavior must still be smoke-tested on the target Windows build.

## 1.1.0 Mapper 9 snapshot

An original rendered MMC2 stress image filled a scrolling nametable with
ordinary and latch-trigger tiles while exercising all 32 possible 4 KiB CHR
banks. After ten warm-up frames, a 300-frame run completed at 106.5 FPS, or
1.77x the NES NTSC frame rate, on the release validation host. That image spent
most of its CPU time in a self-jump and did not represent the mapper's
game-logic cost. The test still confirmed ordered latch changes and continuous
APU sample production.
