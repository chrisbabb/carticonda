# Changelog

## 3.0.0 - 2026-08-04

- Fix the 2.2.8 host-audio handoff failure exposed by the supplied Mega Man 5
  log. Its 318 reported underruns, 278 concealment Sounds, and 318 recovery
  fades occurred despite a 4,096-sample staging reservoir and only 64 worker
  starvations. The mixer pump was mistaking a transient SDL queued-Sound
  promotion for a drained device and then interrupting valid PCM itself.
- Use the dedicated channel's atomic `queue()` operation for every native PCM
  submission. It fills the next slot while active and starts immediately when
  idle, eliminating the check-then-start race and the normal-stream calls to
  either `Sound.play()` or `Channel.play()`.
- Require 12 ms of stable `busy=False` plus an empty native queue before
  declaring a real underrun. A queued Sound observed during the transient
  handoff is left untouched; Cartaconda never stops or replaces it.
- Remove the late synthetic concealment Sound. A confirmed gap now rebuilds
  the ordinary reservoir and fades only the first recovered emulated PCM.
  Native submission failures retain the exact chunk and retry at the next
  two-millisecond service point without forcing a new prebuffer.
- Apply volume to every retained Sound while leaving the transport channel at
  neutral gain, preventing a queued-to-playing promotion from resetting the
  user's volume.
- Replace the PPU's periodic 1,024-row wholesale background-cache clear with a
  bounded 2,048-row LRU. Continuous Mapper 4 scrolling now evicts stale row
  versions incrementally instead of periodically discarding the complete hot
  level working set.
- Admit one third of a recurring Mapper 4 world map on its first appearance
  and every remaining row on its second. Deferred lines use the exact 33-tile
  viewport renderer; the asymmetric schedule balances both frames without a
  third pass or any change to pixels, mapper state, or hardware timing.
- Move rewind zlib compression to a dedicated convenience worker. The
  emulation owner thread freezes and pickles an immutable primitive state,
  publishes video and PCM first, and never waits for compression. Stop and
  rewind drain every accepted snapshot before mutable console state is read.
- Add `rewind-compression-deferrals`, `ppu-world-cache-misses`,
  `ppu-world-cache-evictions`, `ppu-world-cache-admission-deferrals`,
  `ppu-world-cache-size`, `audio-handoff-waits`, and
  `audio-grace-restarts` diagnostics. `audio-concealments` remains as a legacy
  field and stays zero in 3.0.
- A 1,200-frame exact-NTSC combined Mapper 4 field soak averages 7.936 ms with
  12.205/19.806 ms p95/p99, reaches queue depth two, keeps at least 3,610 PCM
  samples after startup, records zero queue starvation and zero audio
  underruns, retains 145 rewind snapshots, and shuts down cleanly.
- The audio transport regression submits 808 ordered samples across 202
  chunks and repeated ambiguous native handoffs with no lost PCM, channel
  stops, or false underruns. A separately supplied Mapper 9 cartridge image
  runs 1,200 core frames at 5.506 ms average and 15.935 ms p99 while producing
  the exact fractional-NTSC PCM count; no commercial ROM or derived content is
  included. All 236 standard-library tests pass.

## 2.2.8 - 2026-08-04

- Correct the 2.2.7 Mega Man 5 field-tail regression using the supplied
  3,344-frame diagnostic. SDL drawing averaged only 0.659 ms, while core p95
  and p99 rose to 37.750/61.000 ms, 1,597 optional Python blocks were compiled,
  the two-frame worker queue starved 366 times, and native audio underran 324
  times. The problem was bursty host code generation, not display scaling or
  the emulated APU.
- Replace the queue-dependent translation gate with a deterministic budget.
  At most one new hot block may be generated in any gameplay frame; up to four
  are allowed only in each of the first two frames while the initial worker
  reservoir is still being filled. Hotness counters saturate at their compile
  threshold instead of growing on every visit.
- Mark a budget-deferred block directly in its cache for the rest of the
  current frame. Repeated visits then take one dictionary lookup and the
  existing generic safe executor instead of allocating and checking a side-set
  key on every scheduler dispatch. The transient marks are removed at the next
  frame boundary.
- Continue an MMC3 span across an 8 KiB slot only when the destination already
  has a compiled hot block. A cold or merely classified destination returns to
  the Console scheduler, preventing the broad 2.2.7 recursion from combining
  cold-slot setup and code generation inside one presentation interval. The
  proven hot fixed-bank helper path remains capped at eight transitions.
- Retain the 2.2.7 content-addressed background-row cache, shared visible-line
  render context, rewind headroom policy, and underrun fade/recovery repair;
  the correction removes the CPU tail without rolling back their average-time
  and audio-continuity gains.
- Add an original combined MMC3 field workload with continuous horizontal
  scroll, moving sprites, sparse nametable writes, sprite-palette changes, and
  independent sprite CHR changes. It is available through
  `--mmc3-field-frames` in the direct benchmark and `--workload mmc3-field` in
  the exact-NTSC threaded soak. It contains no commercial code or data.
- In a 1,024-start translation-burst regression, packaged 2.2.7 compiles all
  1,024 blocks and has median 29.434 ms average / 42.933 ms p99 dispatch time.
  Version 2.2.8 compiles 70 under the bounded schedule and measures 2.468 ms /
  8.257 ms: 91.6% lower average and 80.8% lower p99. The first two hidden
  startup frames peak at four compilations; every later frame peaks at one.
- Across three alternating 1,200-frame runs, the combined field workload falls
  from a packaged-2.2.6 median 7.016 s to 6.254 s, 10.9% lower time and 1.122x
  throughput, with the identical final framebuffer hash. Median p99 falls from
  12.121 to 11.418 ms and median maximum from 33.775 to 24.234 ms.
- Three 900-frame real-worker field soaks all finish with zero queue
  starvation, zero audio underruns, intact rewind history, and clean shutdown.
  Median core time is 6.696 ms average, 9.603 ms p95, and 15.447 ms p99; the
  42.110 ms median maximum is cold frame 2 and is absorbed before presentation.
  All 231 standard-library tests pass.

## 2.2.7 - 2026-08-03

- Use the supplied 2.2.6 Mega Man 5 level telemetry to target the remaining
  side-scrolling tail instead of host presentation. Drawing averaged only
  0.618 ms, but scrolling pushed core p95/p99 to 28.250/40.250 ms, starved the
  two-frame worker queue 243 times, and produced 377 audio underruns.
- Keep content-addressed 512-pixel background world rows alive across sparse
  nametable writes. Exact physical nametable-row and attribute-row versions,
  CHR generation, effective MMC3 pattern mapping, palette, mirroring, and
  scroll remain in every cache key, so an updated row misses without flushing
  unrelated level rows.
- Share palette colors, MMC3 background context, and nametable signatures
  across complete visible-line batches. Cached immutable background rows now
  move directly into retained-line entries instead of being copied out of the
  framebuffer a second time.
- Continue a proven RAM/immutable-PRG CPU batch through JSR, RTS, or JMP into
  another already classified MMC3 8 KiB slot while the same device deadline
  remains active. The chain is bounded to eight slot transitions and falls
  back before an unclassified, dynamic, timed, interrupt, or mapper boundary.
  The scrolling regression executes 24,201 safe scheduler batches instead of
  34,367 over 900 frames, a 29.6% dispatch reduction.
- Allow hot Python-block generation in the threaded frontend only when at
  least one completed frame is already buffered. A depleted producer queue
  executes the existing safe generic batch, keeps accumulating hot evidence,
  and performs optional translation after headroom returns. This prevents the
  1,661 runtime translations seen in the field log from deepening an active
  video/audio stall; `cpu-block-deferrals` reports the protected events.
- Require twelve consecutive full-queue frames before optional rewind
  compression resumes after a producer tail. Recovered lead is therefore
  reserved for video and PCM before convenience history work.
- On a confirmed native audio underrun, ramp the last submitted PCM value to
  zero and fade the first recovered chunk back in. Normal audio is unchanged;
  the repair touches only samples after audio has already been lost. New
  `audio-concealments` and `audio-recovery-fades` counters make the behavior
  visible.
- Across three alternating runs against packaged 2.2.6, the 900-frame
  cross-slot scrolling workload falls from a 2.874 s median to 2.570 s: 10.6%
  lower core time and 1.118x throughput with the same framebuffer hash. IRQ
  timing stays neutral and the independent sprite cadence improves 1.8%.
- The final 900-frame exact-NTSC scrolling worker soak averages 3.328 ms with a
  3.803 ms p95 and 5.053 ms p99, and completes with zero queue starvation,
  zero audio underruns, 149 rewind captures, and clean shutdown. All 230
  standard-library tests pass.

## 2.2.6 - 2026-08-03

- Use the supplied 2.2.5 Mega Man 5 level telemetry to identify a stable-slot
  MMC3 classification regression rather than another rendering problem.
  Presentation averaged 0.475 ms and scanline render/replay ratios remained
  healthy, but gameplay executed about 2,054 literal CPU instructions per
  frame, compiled only two code-bank/slot pairs, starved the worker queue 1,129
  times, and produced 399 audio underruns.
- Complete the two-observation code-table gate whenever control flow enters an
  unclassified MMC3 8 KiB slot, even when the mapper's four-bank tuple has not
  changed. Version 2.2.5 skipped the refresh whenever tuple identity was
  stable, so a level routine reached through JSR/JMP could remain literal for
  the entire stage while title and stage-select code happened to stay fast.
- Retain the low-overhead identity check after the current slot is ready. Data-
  bank churn still does not compile unexecuted banks, and code tables remain
  keyed by immutable bank identity plus CPU slot.
- Add an original scrolling Mapper 4 workload whose fixed-bank main loop calls
  RAM-heavy logic in the unchanged `$C000` slot. It changes scroll and performs
  OAM DMA each NMI without containing commercial code, graphics, or data.
- Across three alternating 300-frame runs, packaged 2.2.5 improves from a
  33.91 FPS median (29.489 ms/frame) to 318.64 FPS (3.138 ms/frame), a 9.40x
  throughput increase and 89.4% lower frame time with identical framebuffer
  hashes. A 120-frame diagnostic segment falls from 1,223,991 literal
  instructions to 773 while safe batches rise from 1,278 to 4,560.
- A repeated 900-frame exact-NTSC worker/audio soak averages 3.462 ms/frame,
  has a 4.513 ms p95, and completes with zero queue starvation, zero audio
  underruns, intact rewind history, and clean shutdown. All 229 standard-
  library tests pass.

## 2.2.5 - 2026-08-03

- Use the supplied 2.2.4 Mega Man 5 telemetry to isolate core work from the
  already inexpensive host presentation. Drawing averaged 0.456 ms, while
  worker CPU averaged 16.253 ms and produced 1,119 queue starvations plus 702
  audio underruns in 6,494 frames.
- Compile official `(d,X)` and `(d),Y` RAM/immutable-PRG operations into the
  existing generated hot blocks. Effective addresses and page-cross cycles
  are resolved at run time, with an early guard that returns before any PPU,
  APU, controller, mapper, or mutable-memory access. A representative indirect
  stream executes about 3.8x faster than the literal instruction path.
- Preclassify immutable code starts as unsafe, statically safe, or guarded
  indirect. The scheduler dispatches proven spans before expensive PPU-wait
  probes, no longer rechecks ordinary device opcodes as possible dynamic
  indirect starts, and admits useful four-cycle spans.
- Advance inaudible pulse, triangle, noise, and drained-DMC timers once across
  a quiet APU span. Silent DMC bits and noise LFSR clocks collapse
  arithmetically, while every timer remainder, LFSR bit, generated PCM sample,
  filter value, and serialized channel state remains reference-identical.
- Replace iterative MMC3 IRQ lookahead with a constant-time counter projection.
  The current and following A12 phase are handled explicitly; subsequent
  qualified edges are counted directly without scanning intervening lines.
- Batch complete visible PPU scanlines when no CPU-visible event interrupts
  them. MMC3 A12 phases, overflow, sprite-zero timing, scroll copies,
  framebuffer output, and frame completion retain the same ordering as the
  event path.
- Split palette dependencies into background and sprite halves. A sprite-only
  palette write can retain background-only scanlines just as a sprite-only
  MMC3 CHR change already does, and repeated scanline calls share precomputed
  palette identities.
- On three identical 1,200-frame Mapper 4 sprite/palette/bank cadence runs,
  median elapsed time falls from 6.122 to 3.604 seconds, or 5.102 to 3.003
  ms/frame: 1.70x throughput and 41.1% less core time with an identical final
  framebuffer hash. Separate 900-frame Mapper 4 and active-IRQ worker soaks
  average 4.246 and 3.036 ms/frame respectively, both with zero queue
  starvation and zero audio underruns. All 228 standard-library tests pass.

## 2.2.4 - 2026-08-03

- Use the supplied 2.2.3 Mega Man 5 telemetry to identify the remaining
  periodic Mapper 4 stall. Presentation averaged only 0.458 ms, while 268
  mapper invalidations in 4,776 frames formed almost exactly the cadence of
  the 34.000 ms p95 core tail, 728 worker-queue starvations, and 480 audio
  underruns.
- Split render-cache dependencies at the MMC3's real two-pattern-table
  boundary. A mapper exposes the effective four-bank mapping used by one
  4 KiB pattern table; MMC3 precomputes independent lower and upper tokens
  using complete bank indices, including large NES 2.0 CHR images.
- Preserve cached 512-pixel world rows and viewport backgrounds when an MMC3
  write changes only sprite-side CHR. Only composed sprite pixels and the
  sprite-zero overlap plan are invalidated. Empty-sprite scanlines can replay
  directly through those writes, while 8x16 sprites conservatively depend on
  both pattern tables.
- Keep separate background and sprite mapper generations in retained-line
  keys. This avoids both broad tuple construction and false invalidation while
  retaining mirroring, CHR-RAM, palette, scroll, OAM, latch, and status
  dependencies.
- Increase the bounded translated-block cache from 2,048 to 4,096 functions
  and preserve its established working set at capacity instead of clearing
  every compiled block at once. New starts remain on the already optimized
  safe dispatcher if the bound is reached.
- Add `ppu-mapper-background-preserves` to diagnostics and a deterministic
  1,800-frame Mapper 4 sprite-bank cadence benchmark. Against packaged 2.2.3,
  the identical workload lowers p95 frame time from 15.994 to 4.889 ms and
  bank-change average from 16.815 to 4.274 ms (3.93x faster). Its complete
  frame hash is unchanged.
- Compare 360 frames containing independent background-bank, sprite-bank, and
  mirroring changes against conservative full invalidation. Every framebuffer
  and complete serialized state matches. All 223 standard-library tests pass.

## 2.2.3 - 2026-08-03

- Use the supplied 2.2.2 Mega Man 5 telemetry to separate core work from host
  presentation. Drawing averaged only 0.452 ms, while emulation consumed
  16.382 ms of worker CPU time, the queue starved 930 times, and audio
  consequently underran 456 times in 4,559 frames.
- Translate repeatedly executed, already-proven RAM/immutable-PRG basic blocks
  into fixed Python-bytecode templates after eight observations. Registers,
  flags, stack pointer, open bus, cycles, and program counter remain local
  across ALU, memory, branch, call/return, and short control-flow bridges.
  Device accesses, mapper writes, interrupt hazards, DMA, DMC boundaries,
  indirect uncertainty, unofficial opcodes, and mutable code remain on the
  ordinary correctness path. This is a bounded pure-Python cache, not native
  code and not an external emulator core.
- Correct the MMC3 instruction classifier at the last byte of each 8 KiB slot.
  An implied opcode there performs its discarded fetch from the next mapped
  bank and therefore cannot be treated as a wrapping single-bank span.
- Hold complete APU channel, mixer, and three-stage filter state in locals
  across long no-fetch sample runs, then publish once. Short event bursts keep
  the compact prior path so register-heavy mapper traffic does not pay the
  larger local setup cost. Generated PCM and serialized APU state remain exact.
- Fetch ordinary background and sprite pattern planes through the mapper's
  ordered pair boundary, and cache each nontransparent sprite pattern row as
  compact offset/pixel pairs. MMC2 latch side effects and the reference dot
  renderer remain explicit.
- Retry one transient Windows/WASAPI `Sound.play()` start race through the same
  guarded API after clearing a stale idle channel. Pending PCM is never
  discarded, and Cartaconda still avoids pygame-ce's unsafe explicit
  `Channel.play()` failure path.
- Against packaged 2.2.2 with identical framebuffer hashes, the 900-frame
  direct workloads improve from 125.49 to 236.36 FPS for NROM, 110.53 to
  192.70 FPS for Mapper 9, 127.80 to 218.14 FPS for Mapper 4, and 230.08 to
  258.71 FPS for active Mapper 4 IRQ. All four isolated 900-frame threaded
  soaks complete with zero queue starvation and zero audio underruns; their
  average core times range from 3.99 to 5.00 ms/frame. All 220 standard-library
  tests pass.

## 2.2.2 - 2026-08-03

- Use the reported Mega Man 5 telemetry to keep optimization focused on the
  emulation core: presentation averaged only 0.432 ms while the worker spent
  22.022 ms of CPU time per frame, rendered 2,168,203 scanlines, and missed
  7,443 frame budgets in 9,086 frames.
- Keep the complete official RAM/immutable-PRG instruction family in CPU
  locals during a scheduler-approved span. Loads, stores, logic, arithmetic,
  compares, shifts, rotates, increments, decrements, transfers, stack
  operations, JSR, RTS, all conditional branches, and every addressing mode
  now avoid repeatedly publishing and restoring the CPU object when their
  accesses have already been proven unable to touch a timed device.
- Preserve page-cross penalties, mirrored-RAM addressing, MMC3 segmented PRG
  reads, NMOS flags, stack order, final opcode, cycle count, and final open-bus
  value. Dynamic indirect accesses and every timed-device, mapper-register,
  DMC, DMA, NMI, or IRQ boundary retain the literal path.
- Extend physical nametable tile-row and attribute-row dependencies to the
  ordinary NROM/MMC3 renderer. A sparse update now recomposes only the lines
  that fetch the changed row instead of discarding all 240 retained lines.
  CHR-RAM, palette, mirroring, scroll, sprite, and effective mapper state stay
  in the content identity.
- Separate effective MMC3 mapping identity from nametable content versions.
  Recurring per-scanline CHR maps can reuse immutable world rows without a
  redundant invalidation counter creating a new key on every bank transition.
  Zero-write scenes retain a constant-time replay key, and cache admission
  avoids sequential FIFO thrashing when a working set reaches its bound.
- In five isolated runs, a call/RAM-heavy original CPU workload increased
  from a 5.018M to 6.783M cycles/s median (35.2%). Six hundred frames with one
  changed nametable row each fell from 4.016 s to 0.838 s (4.79x), while 1,000
  unchanged frames improved slightly from 0.971 s to 0.945 s.
- The deterministic Mapper 4 matrix also improved without changing final
  framebuffer hashes: gameplay 121.31 to 124.60 FPS, active IRQ 211.68 to
  219.53 FPS, CHR-register churn 17.32 to 18.27 FPS, and PRG-bank churn 16.74
  to 21.84 FPS. All 217 standard-library tests pass, including exhaustive new
  opcode, page-cross, MMC3-bank, pixel, and complete-state differential gates.

## 2.2.1 - 2026-08-03

- Put every windowed mode in a 4:3 host window while preserving an exact
  2x, 3x, or 4x integer-scaled 256x240 game image. The resulting window sizes
  are 640x480, 960x720, and 1280x960; the default 3x launcher now maps its
  960x720 design surface to the display one-to-one.
- Use filtered resampling only when the menu needs a fractional scale. The
  previous nearest-neighbor downscale could discard complete rows and columns
  from small font glyphs, producing garbled text at 2x and 3x. Exact and
  integer-upscaled pixel-art surfaces retain nearest-neighbor presentation.
- Add a `DRIVES` control and virtual `THIS PC` level to the ROM browser.
  Windows builds enumerate all mounted drive roots with `os.listdrives()` and
  retain a Win32 bitmask fallback for Python 3.11. Selecting a `DRV` entry
  opens that volume, and pressing `UP` at a drive root returns to the drive
  picker.
- Add deterministic drive enumeration and display-scaling regressions. All
  214 standard-library tests pass, and pygame-ce 2.5.7 renders the home,
  settings, controls, browser, and archive views successfully at all three
  window sizes.

## 2.2.0 - 2026-08-03

- Use the reported Mega Man 5 Mapper 4 telemetry to isolate the dominant
  regression. Version 2.1.3 averaged 33.528 ms/frame and performed 2,087,640
  device flushes in 3,544 frames because an enabled MMC3 IRQ made the worker
  stop at every possible A12 phase, even when the counter could not assert for
  many scanlines.
- Predict the exact next MMC3 IRQ assertion without mutating mapper or PPU
  state. The scheduler now spans non-asserting A12 edges and intervening
  scanlines, then returns to the literal instruction boundary at the one edge
  that can raise IRQ. Pre-render edges retain their explicit conservative
  deadlines.
- Recognize the immutable four-record cooperative scheduler used by late
  action-game engines. When all four RAM statuses select the idle path, a
  complete 93-cycle scan repeats in O(1) until the next hardware deadline.
  The path is gated by the full instruction/control-flow signature, current
  X/Y state, status values, and interrupt state—not a title, hash, or fixed
  address—and preserves CPU flags, registers, RAM, open bus, final opcode,
  and cycle count.
- Compile each executed MMC3 8 KiB code bank on first use. Bank tables remain
  keyed by immutable bank identity and CPU slot, so thousands of data-bank
  changes cannot trigger optimizer work. The fixed reset bank is classified
  during reset rather than after the first rendered frame.
- Cache MMC3's effective CHR/mirroring PPU token and refresh it only when a
  register write changes visible mapping. Equivalent register writes no
  longer allocate a token or invalidate retained scanline state.
- Add an original active-MMC3-IRQ/cooperative-scheduler cartridge to the
  benchmark and exact-NTSC soak tools. It contains no commercial code, data,
  graphics, or audio.
- On the same threaded exact-NTSC workload, packaged 2.1.3 averaged 20.750
  ms/frame with 58 queue starvations and eight audio underruns. Version 2.2.0
  averaged 5.088 ms/frame over 900 frames with zero queue starvation and zero
  audio underruns: 4.08x throughput, or about 308% faster. A separate
  three-run direct median fell from 26.675 to 4.830 ms/frame.
- Match the instruction-synchronized reference for 120 consecutive active-IRQ
  frames and complete serialized state. Add exact assertion, pre-render
  fallback, cooperative-poll, and end-to-end state regressions. All 211
  standard-library tests pass.

## 2.1.3 - 2026-07-30

- Use the reported 7,371-frame run to separate the remaining core and audio
  problems. Drawing now averages only 0.407 ms and the worker queue almost
  never starves, but a rare 87.914 ms scheduling pause exceeds the 46 ms of
  PCM that SDL_mixer could previously hold natively.
- Double each submitted PCM block to 2,048 samples. SDL_mixer's playing and
  queued slots now cover about 93 ms at 44.1 kHz while the startup reservoir
  remains exactly 4,096 samples, so resilience increases without adding
  startup audio latency or discarding generated samples.
- Replace the single approximate DC blocker with the NES's complete
  first-order analog output chain: 90 Hz and 440 Hz high-pass stages followed
  by a 14 kHz low-pass stage. Bilinear-transform coefficients are calculated
  once per host rate and the scalar recurrences remain inline. This removes
  the residual DC thump after sampled sounds and softens abrupt DAC edges
  without clipping.
- Track MMC2 retained-frame dependencies by physical nametable tile row and
  attribute row. A write now invalidates only scanlines that actually fetch
  the changed row instead of changing one global generation and forcing all
  240 lines to be recomposed. Mirroring, scroll, CHR, palette, mapper register,
  sprite, and latch state remain part of the replay key.
- Cache the immutable palette signature and MMC2 PPU register token instead of
  rebuilding them for every scanline. Unknown mapper/CHR changes still take
  the conservative whole-view fallback.
- Add unrelated-row replay, analog-filter/DC settling, old-state filter
  migration, and native audio-coverage regressions. All 208 standard-library
  tests pass.
- Across 600 supplied-ROM active-DMC frames, 2.1.3 and 2.1.2 produce the same
  aggregate framebuffer hash and the same final non-filter hardware state.
  The new filter intentionally changes PCM. A separate 900-frame continuation
  averaged 5.940 ms with an 8.870 ms p95, 9.697 ms p99, and 14.686 ms maximum;
  86.04% of scanline slots replayed. Four-run checkpoints were about 9% faster
  in the active-DMC interval and 10% faster in ordinary first-fight gameplay
  than 2.1.2 on the validation host.

## 2.1.2 - 2026-07-29

- Reproduce the 2.1.1 Mapper 9 regression from the supplied Punch-Out!! DMC
  interval and compare it directly with both 2.1.1 and the previously smooth
  1.1.1 build. The dominant `$AF06` span is a side-effect-free RAM wait, but
  2.1.1 stopped at every DMC DMA request and repeatedly re-entered the CPU,
  APU, PPU, and scheduler.
- Collapse a preclassified Mapper 9 RAM wait across multiple DMC fetches in
  one bounded operation. The batch reserves every four-cycle read hold,
  advances CPU time separately from physical APU/PPU time, and stops before
  PPU/NMI or APU-frame edges. DMC IRQ, OAM DMA, writes, changing RAM, and
  interrupt-visible cases retain the literal path.
- Retain unchanged framebuffer scanlines in place and replay MMC2's recorded
  outgoing latch state instead of recomposing the same line. Content-address
  MMC2 tile runs by the complete packed fetch plan, palette, CHR bank bases,
  and incoming latches so recurring rows survive unrelated cache
  invalidations without accepting stale pixels.
- Remove active-DMC APU loop overhead, add slots to hot audio-channel objects,
  and use explicit stable save-state dictionaries. Host PCM values and every
  serialized APU field remain unchanged.
- On Windows, upload only the dirty native 256x240 frame to a streaming SDL
  texture and let the accelerated renderer perform nearest-neighbor scaling.
  This removes the full-window CPU scaler/converting blit from gameplay. The
  renderer, textures, window, mixer, controller, and Surfaces remain on the
  main thread and are destroyed in dependency order. Unsupported or failed
  hardware initialization falls back automatically to the portable Surface
  path.
- Extend startup diagnostics with `display-backend` and `display-driver`, and
  final diagnostics with `ppu-scanline-replays`,
  `cpu-dmc-poll-batches`, and `cpu-dmc-poll-fetches`.
- Add exact Mapper 9/DMC wait-loop, retained-scanline invalidation, texture
  upload/present, and diagnostic regressions. All 204 standard-library tests
  pass, and 300 consecutive supplied-ROM DMC-heavy frames match the 2.1.1
  serialized machine state at every boundary.
- In five 180-frame runs from the same captured state, 2.1.2 averaged 6.783
  ms/frame versus 9.160 ms for 2.1.1 and 7.651 ms for 1.1.1 on the validation
  host. A separate 900-frame supplied-ROM run averaged 6.891 ms with an
  11.375 ms p95. NROM, MMC2, and MMC3 exact-NTSC worker/rewind soaks completed
  with zero queue starvation, zero virtual audio underrun, and clean shutdown.

## 2.1.1 - 2026-07-29

- Reproduce the reported Mapper 9 slowdown in the first Glass Joe fight
  instead of treating the already-fast title/bell sequence as the complete
  workload. Punch-Out!! synchronizes its mid-frame scroll by executing
  `LDA $2002 / AND #$40 / BNE|BEQ` roughly one thousand times per frame.
- Collapse only the still-taken sprite-zero polling iterations before the
  next possible flag transition. APU frame edges, NMI, DMC fetches, visible
  sprite-hit dots, MMC2's ordered dot-256 latch commit, and the loop's exiting
  read all remain literal boundaries.
- Preserve the final status read, PPU open-bus decay clock, write-toggle
  effect, CPU flags/open bus, branch timing, and DMC-held read behavior. Three
  hundred consecutive supplied-ROM fight frames match the 2.1.0 execution
  path in complete serialized console state and framebuffer output.
- Convert the 256x240 RGB frame once into the display's native pixel format
  before scaling. The large scaled surface and final screen blit now share a
  format, avoiding a full-window 24-to-32-bit conversion on Windows.
- Split diagnostics into `draw-prepare-*` and `flip-*` timings so a slow
  scaler/blit can be distinguished from driver/compositor blocking.
- Add active-DMC, exact DMC-fetch-boundary, MMC2 dot-256 fallback, display
  reuse, and extended diagnostic regression coverage. The standard-library
  suite now has 201 passing tests.
- On the validation host, the captured first-fight core segment fell from a
  21.620 ms/frame median on the 2.1.0 path to 9.558 ms/frame (2.26x). A
  420-frame exact-NTSC worker/rewind soak averaged 9.219 ms with a 14.539 ms
  p95, zero frame-queue starvation, and zero virtual audio underruns.

## 2.1.0 - 2026-07-29

- Merge every timestamped Punch-Out!! `$4011` write with the host PCM sample
  boundaries in one APU event loop. The old path re-entered the complete APU
  dispatcher and PPU planner for every preserved DAC value; 2.1 advances the
  PPU once for the bounded span while retaining the old-DAC-on-equal-cycle
  mixer ordering.
- Guarantee that an immutable Mapper 9 code map containing the complete
  sampled-audio signature yields at `$908D`. A general safe CPU batch can no
  longer execute across the specialized entry point and silently return the
  bell routine to instruction-by-instruction scheduling.
- Apply a forced-blank MMC2 PPUDATA stream as one ordered VRAM transaction.
  Nametable, palette, address-increment, final open-bus value, and final write
  clock remain exact; CHR-ROM writes remain electrically ignored and
  presentation caches invalidate once per changed stream instead of once per
  byte.
- Index the raw safe-instruction table for the active MMC3 8 KiB code slot
  directly. This removes one Python segmented-view dispatch from every proven
  CPU-only instruction without weakening bank or slot boundaries.
- Gate Mapper 9 and forced-blank specialist probes by mapper, PPU state, first
  opcode, and minimum deadline so ordinary gameplay does not repeatedly enter
  helpers that can only reject it.
- Fix the fast PPU's forced-blank shortcut skipping the dot-1 vblank set or
  pre-render clear transition when it was entered at cycle zero. The optimized
  and instruction-synchronized fast paths now agree with the dot-timed event
  model at both edges.
- Add exact APU DAC-boundary, zero-cycle DAC, Mapper 9 scheduler-yield, and
  forced-blank vblank regression coverage. The standard-library suite now has
  198 passing tests.
- Compare 400 consecutive frames of the supplied Mapper 9 Rev 1 image against
  the instruction-synchronized path with identical CPU, PPU, APU/PCM, RAM,
  bus, mapper, and framebuffer state. A separate 420-frame exact-NTSC worker
  run completed with zero queue starvation, zero virtual audio underrun, and
  clean shutdown.
- On the same validation host, the packaged 2.0.6-to-2.1.0 Mapper 9
  forced-blank workload rose from 42.68 to 143.79 FPS (3.37x), and the Mapper
  4 gameplay workload rose from 86.43 to 95.04 FPS. The real supplied-ROM bell
  interval improved from a five-run median of 4.036 to 3.801 ms/frame.
- Keep pygame-ce as the only runtime dependency after profiling. The remaining
  hot work is branch-heavy, mutable CPU/PPU/APU state; adding a bulk-array
  package would increase the Windows distribution without accelerating those
  control-flow paths, and a JIT dependency would not match the supported
  Python 3.14/frozen release target.

## 2.0.6 - 2026-07-29

- Use the 2.0.5 field diagnostics to identify its remaining Windows/Python
  overhead: every preserved `$4011` DAC write was still its own optimized
  scheduler batch, producing about 50,500 Python dispatches in the reported
  333-frame run.
- Process a bounded run of CPU-driven DAC samples per scheduler dispatch while
  retaining every individual write value and exact cycle offset. APU and PPU
  clocks still advance separately between each hardware-visible `$4011` write.
- Include the common non-wrapping compressed-byte refill path, allowing spans
  to cross ordinary four-sample source-byte boundaries. Pointer wrap, command
  transitions, interrupts, DMC activity, and short device deadlines remain
  literal.
- Validate every machine-code region used by the specialized path before it
  can activate. Add multi-sample/refill differential regression coverage and
  report both `cpu-pcm-batches` and preserved `cpu-pcm-writes`.
- Exact supplied-ROM validation now covers 400 consecutive frames with
  identical CPU/RAM/bus/mapper/PPU/APU state, framebuffer pixels, and PCM.
  The diagnostic bell interval averages about 4.8 ms/frame on the validation
  host with zero budget misses; only 940 PCM batches are required across the
  400-frame run.

## 2.0.5 - 2026-07-28

- Reproduce the reported Mike Tyson's Punch-Out!! Rev 1 startup directly and
  identify the actual black-screen workload: the game drives the raw `$4011`
  DAC from a calibrated 6502 bitstream/delay loop rather than using ordinary
  buffered APU playback.
- Add a code-signature-gated software-DAC executor. It folds the loop's
  RAM/immutable-ROM work into local arithmetic while retaining every `$4011`
  value, its exact CPU-cycle write offset, APU/PPU clocks, interrupt deadline,
  CPU/RAM/open-bus state, and literal fallback at compressed-stream updates.
- Collapse forced-blank `$2002` vblank wait loops between observable PPU/APU
  deadlines. The final status read and repeated PPU setup writes retain their
  exact bus clocks and side effects.
- Keep seven cycles of headroom at PPU/NMI boundaries so a multi-instruction
  DAC span stops at the same frame instruction as the literal core.
- Extend diagnostics with `cpu-pcm-batches` and `cpu-ppu-wait-batches`, and add
  original synthetic regression cartridges for both optimizers.
- Validate 230 consecutive frames of the supplied Mapper 9 Rev 1 image against
  the literal path with identical CPU, RAM, mapper, PPU, APU, pixels, and PCM.
  On the validation host the bell interval fell from about 12.1 ms/frame to
  about 6.9 ms/frame, with no sustained frame-budget misses.
- The deterministic standard-library suite now contains 196 passing tests.

## 2.0.4 - 2026-07-28

- Correct the remaining Punch-Out!! startup diagnosis. The 2.0.3 nametable
  path was active, but real diagnostics showed only about eleven deferred
  writes per frame while the CPU still averaged 19.7 ms. The remaining scene
  decoder reads command streams through dynamic 6502 `(d),Y` addressing,
  which the static 2.0 span classifier had conservatively rejected.
- Resolve official indexed-indirect and indirect-indexed accesses at the
  bounded scheduler boundary. Reads whose real and dummy addresses remain
  wholly inside CPU RAM or immutable PRG ROM can now stay in a CPU-only span;
  device, expansion, cartridge-RAM, and mapper-register accesses remain
  literal.
- Inline the hot `LDA (d),Y`/`LDA (d,X)` stream forms inside the span
  executor. Active DMC playback deliberately retains literal per-instruction
  timing so fetch/read conflicts cannot be reclassified against a neighboring
  CPU write.
- Stop asking SDL to scale, blit, and flip a gameplay frame when its pixels
  and toast state are unchanged. Emulation, PCM generation, input polling,
  event pumping, and exact fractional-NTSC deadlines continue; window expose,
  view, display-mode, new-video, and toast transitions force a redraw.
- Extend diagnostics with actual unchanged-display skips, literal and batched
  CPU scheduler counts, device flush/stall counts, DMC fetches, and the eight
  CPU span addresses accounting for the most emulated cycles.
- Add Mapper 9 indirect-command-stream and DMC-conservative regression
  coverage. The deterministic suite now contains 194 passing tests.

## 2.0.3 - 2026-07-28

- Correct the first-fight regression diagnosis: Mike Tyson's Punch-Out!! uses
  Mapper 9/MMC2, not Mapper 4/MMC3. Its forced-blank setup path streams
  nametable data through `$2007` while bell audio is active.
- Timestamp safe MMC2 forced-blank PPUDATA writes at their exact logical PPU
  clocks without entering both device steppers for every byte. DMC activity,
  CHR-space writes, rendering changes, device deadlines, and all other mappers
  retain the fully synchronized path.
- Join repeated `$2007` stores with adjacent proven-safe CPU work inside one
  bounded decompression span. Interrupt polling and APU/PPU/DMC deadlines
  remain the batch boundaries.
- Coalesce repeated invalidation of already-empty presentation caches while a
  blank setup screen changes nametable data.
- Mark audio-only/unchanged-video worker packets explicitly. The SDL thread
  skips the 184 KiB framebuffer copy and reuses the already-scaled surface
  instead of scaling the same black frame every presentation.
- Add forced-blank Mapper 9, audio-only worker, unchanged-scale, exact-state,
  and extended diagnostic coverage. The deterministic suite now contains 192
  passing tests.
- Against the packaged 2.0.2 wheel on the same host and original synthetic
  Mapper 9 workload, median first-frame time falls from 79.562 ms to
  14.188 ms (-82.2%). The patched path is also faster than the packaged 1.1.1
  median of 37.668 ms on the same workload.

## 2.0.2 - 2026-07-28

- Remove the remaining Mapper 4 scene-transition render storm. Exact
  sprite-zero scheduling now uses a cheap eight-pixel status probe only when
  the beam reaches sprite zero instead of composing the same full scanline
  after every CHR write. A continuous MMC3 invalidation frame renders its 240
  visible lines once each instead of 2,054 times.
- Replace eager 32 KiB MMC3 CPU-map compilation with stable, executed-bank
  admission. Only the 8 KiB slot containing the current PC is classified;
  data banks that churn while fixed-bank code runs no longer allocate and
  compile dozens of unused map combinations during one frame.
- Prioritize the next emulated frame and PCM block over optional rewind
  compression whenever the two-frame reservoir is not full.
- Extend diagnostics with rewind deferrals, PPU scanline renders/status
  probes/mapper invalidations, and MMC3 code-bank compilation/deferral counts.
- Add original continuous MMC3 CHR- and PRG-register regression cartridges.
  On the same loaded validation host, their first-frame times fall from
  162.8 to 95.1 ms and 160.8 to 92.8 ms respectively versus the packaged
  2.0.1 wheel, with identical optimized-vs-literal console state.
- Expand the deterministic standard-library suite to 189 passing tests.

## 2.0.1 - 2026-07-28

- Cache MMC3's four PRG slots and eight CHR offsets at register-write time
  instead of reconstructing bank maps on every CPU and PPU read. CPU code
  fetches now use direct 8 KiB immutable slots.
- Extend safe multi-instruction CPU spans to MMC3 without enumerating its
  thousands of possible 32 KiB bank combinations. New banks are classified
  independently by slot, while active maps and loop tables are retained.
- Render only the 33 visible tiles after a cold MMC3 CHR or mirroring change.
  Recurring transition states retain content-keyed world rows, and stable
  states automatically return to the reusable 512-pixel path.
- Continue clocking every MMC3 A12 edge inside coalesced PPU spans while its
  IRQ output is disabled, avoiding three unnecessary CPU-worker stops per
  scanline. Enabling IRQ first synchronizes devices, after which assertion
  edges remain exact CPU-visible scheduler boundaries.
- Fix MMC3 save snapshots sharing the live bank-register list.
- Publish completed video and PCM to the two-frame queue before optional
  rewind compression. Snapshot work remains on the console-owner thread but
  can no longer hide an already-finished frame from presentation/audio.
- Add deterministic Mapper 4 gameplay, transition-cache, synchronized-state,
  benchmark, and threaded audio-reservoir soak coverage. The suite now
  contains 185 passing tests. On the validation host the transition-heavy
  Mapper 4 benchmark reaches 82.7 FPS; a 300-frame bounded worker soak with
  rewind capture records zero frame-queue starvation and zero audio underruns.

## 2.0.0 - 2026-07-27

- Decode NES 2.0 exponent/multiplier ROM sizes and separate volatile PRG/CHR
  RAM from battery-backed PRG/CHR NVRAM. Preserve legacy raw PRG saves while
  using a versioned container only when mixed or CHR-backed persistence needs
  it, and recover archaic `DiskDude!` iNES headers safely.
- Emit both writes of official and stable-unofficial 6502 read/modify/write
  instructions. Add a timed mapper-write boundary so MMC1 ignores the second
  RMW data write while still accepting reset writes.
- Add explicit NES 2.0 bus-conflict behavior and iNES mappers 10, 11, 13, 34,
  71, 87, 94, 140, and 180: MMC4, Color Dreams, CPROM, NINA-001/BNROM,
  Camerica, Jaleco's reversed CHR board, UN1ROM, Jaleco JF-11/JF-14, and the
  Crazy Climber UNROM wiring.
- Precompute immutable PRG mappings for the finite-window discrete mappers.
  Their CPU reads now use the same direct, preclassified span path introduced
  for MMC2, while render caches invalidate only when CHR or mirroring state
  actually changes—including mapper registers below `$8000`.
- Add a recursive public-diagnostic runner for the blargg `$6000` protocol,
  delayed reset requests, safe multi-ROM ZIP traversal, JSON output, and JUnit
  CI output. External test ROMs remain unbundled.
- Model the PPU's sprite-zero timing, secondary-OAM overflow evaluation and
  diagonal overflow bug, VBlank/NMI suppression windows, odd-frame timing, and
  open-bus behavior on both the dot reference and optimized interactive paths.
  The recorded fast-path PPU gate passes all 24 public open-bus, sprite-hit,
  sprite-overflow, and VBlank/NMI subtests; the dot path passes the same suites.
- Model DMC DMA read conflicts with PPU and controller ports. The public
  controller/write tests terminate successfully, while the two CRC-only read
  diagnostics produce the hardware-reference output rows and CRCs `159A7A8F`
  and `D84F6815`.
- Add a 30-second bounded rewind ring with compressed, ROM-identified internal
  snapshots. `Backspace` restores about one second and
  `Shift`+`Backspace` about five seconds. Rewind capture stays on the worker
  that owns the console, cannot write battery storage, and flushes host audio
  after restoration.
- Remove sustained PPU allocator churn during continuous scrolling. Full
  viewport and sprite-composed lines are admitted only after their frame
  signature repeats; the reusable 512-pixel world-row cache remains available
  immediately. A 2,000-frame scrolling NROM run rose from 93.2 to 140.3 FPS
  and eliminated all frames above 20 ms on the validation host.
- Compact each MMC2 fetch plan from 33 nested tuples to 66 bytes and retain a
  full horizontal scroll cycle. Combined with bounded latch-aware tile caches,
  this removes Mapper 9's cache-thrash cycle without suppressing any FD/FE
  read side effect.
- Preclassify hot CPU batch opcodes without resolving an `Instruction`
  dataclass for each safe instruction, and give the APU a no-DMC-fetch span
  path that does not recompute an impossible fetch deadline for every host
  sample.
- Use a measured 5 ms gameplay thread-switch interval. The former 1 ms value
  forced excessive GIL handoffs against the two-millisecond host audio pump;
  the worker queue still bounds presentation variance while the main thread
  continues to own every SDL call.
- Add thread-CPU timing to diagnostics so OS/GIL descheduling can be separated
  from actual emulation work, and add `tools/soak.py` for the real bounded
  worker, two-frame queue, audio-reservoir, rewind, and clean-shutdown release
  gate.
- Expand deterministic coverage from the 119-test 1.1.1 release baseline to
  182 passing tests. Recorded public gates pass 35/35 CPU tests, 24/24 PPU
  tests, and 17/17 terminal APU/DMC tests, with two additional CRC-only DMC
  results matching their recorded outputs. The common Sharp/new MMC3 IRQ
  revision passes 10 applicable tests; the mutually exclusive NEC/revision-A
  behavior remains an explicit board-variant boundary.
- On the final validation-host benchmark, the NROM gameplay workload reaches
  142.4 FPS (2.37x NTSC), the same workload with rewind capture reaches
  131.9 FPS (2.19x), and the latch-heavy Mapper 9 workload reaches 125.4 FPS
  (2.09x). Their final frame hashes remain
  `a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c`
  and
  `8dd3eb48053a0e82d5ff6d940b14ee3f29f18968f0745a271889f6f43175d22a`.

## 1.1.1

- Remove Mapper 9's largest gameplay bottleneck. Version 1.1.0 could use
  bounded CPU instruction spans only for NROM, so MMC2 software paid the full
  scheduler and Python method-dispatch cost for nearly every instruction.
- Precompute every immutable MMC2 32 KiB CPU mapping and its safe-instruction,
  counter-loop, and RAM-poll tables. Switching the `$8000-$9FFF` bank now swaps
  table references; mapper writes and all device-visible operations still run
  through the literal scheduler.
- Fetch opcodes and operands directly from the active mapped PRG window and
  inline the common RAM, branch, load, compare, arithmetic, flag, and
  increment/decrement operations inside proven-safe spans.
- Cache MMC2 background fetch plans without caching away CHR reads. Repeated
  identical scanlines may reuse pixels only after recording and restoring the
  exact incoming and outgoing FD/FE latch state, preserving every
  render-visible mapper side effect.
- Stop invalidating PPU presentation caches for an MMC2 PRG-only bank change.
  CHR-register and mirroring writes still invalidate them whenever the
  render-visible mapper token changes.
- Replace the pattern-row cache's all-at-once clear with bounded FIFO eviction,
  eliminating a periodic cache-rebuild and memory-management spike.
- Raise the CPU-heavy, scrolling Mapper 9 gameplay stress workload from
  31.8 FPS in the unmodified 1.1.0 release to 84.7 FPS (1.41x NTSC, +166.6%)
  on the validation host with identical frames and serialized state. A
  900-frame threaded audio/presentation soak completed with zero queue
  starvations and zero virtual audio underruns.
- Add Mapper 9 bank-aware span, cache-invalidation, latch-replay, and
  optimized-vs-literal state tests. The release suite now contains 119
  deterministic tests.

## 1.1.0

- Add iNES Mapper 9 (Nintendo MMC2/PxROM) with its switchable 8 KiB PRG bank,
  three fixed PRG banks, four 4 KiB CHR registers, dynamic horizontal/vertical
  mirroring, optional PRG RAM window, and complete save-state serialization.
- Model both MMC2 FD/FE latches as read side effects. The trigger access is
  served by the previously selected CHR bank and changes the latch only for
  subsequent reads; latch 0 uses the exact `$0FD8`/`$0FE8` addresses while
  latch 1 decodes `$1FD8-$1FDF`/`$1FE8-$1FEF`.
- Add a side-effect-aware interactive PPU path that fetches only the 33 pattern
  tiles intersecting a scrolled viewport, in left-to-right order. MMC2 frames
  bypass background/composed-frame caches so cached pixels can never suppress
  a latch transition.
- Read each latched low/high pattern pair through one specialized mapper call
  and cache immutable PRG/CHR bank counts. A rendered Mapper 9 stress workload
  reaches about 106.5 FPS (1.77x NTSC real time) on the release validation
  host.
- Extend side-effect-free non-MMC3 `JMP $self` batching across ordinary visible
  scanlines while retaining APU, DMC, NMI, register-access, and frame
  boundaries. MMC3 continues to stop before each scanline IRQ edge.
- Add original Mapper 9 ROM boot/render coverage plus PRG/CHR banking, delayed
  latch, exact-address, mirroring, four-screen, save-state, fast-render cache,
  pair-vs-literal, and ZIP-loading tests. The release suite now contains 116 deterministic
  tests.

## 1.0.0

- Replace the accumulating late-frame debt algorithm with one absolute
  fractional-NTSC clock. Host lateness can no longer make the emulator run
  slightly fast, overfill PCM staging, or leave seconds of unrecovered pacing
  debt.
- Add a bounded two-frame emulation pipeline. A pure-Python worker owns the NES
  core and reusable transfer buffers, while every Pygame/SDL display, mixer,
  event, and controller operation remains on the main thread. Cheap frames now
  provide headroom for expensive game-logic frames without dropping or
  partially presenting a frame.
- Service both native audio slots every two milliseconds while waiting for a
  completed frame or presentation deadline. PCM remains ordered with its
  originating video frame, and pausing, resetting, loading state, replacing a
  ROM, or shutting down first joins the core owner and resets the reservoir.
- Reduce the Python thread-switch interval to 1 ms only while gameplay is
  active, bounding input/audio/display service delay; restore the host setting
  on every stop and cleanup path.
- Give a newly started empty worker one extra frame period to warm its queue.
  ROM startup and menu resume cannot count an expected cold-cache first frame
  as a gameplay starvation.
- Preclassify NROM instructions that can touch only CPU RAM, stack RAM, or
  immutable PRG ROM. Execute those instructions inside one device-deadline
  batch, while device I/O, DMA, DMC fetches, interrupt-unmasking operations,
  mapper writes, and known O(1) idle loops retain their exact paths.
- Inline the specialized NROM CPU dispatcher inside safe spans. On the included
  action-platformer-shaped workload, this raises throughput from about 79 FPS
  in 0.9.1 to about 113 FPS on the validation host, with the same frame hash
  and serialized CPU/PPU/APU/bus state.
- Keep two fixed framebuffer transfer buffers and one main-thread SDL backing
  buffer. No per-frame framebuffer or Surface allocation crosses into Pygame.
- Segment presentation statistics across menus, resets, display changes,
  debugger breaks, and system suspend. Multi-second gaps no longer appear as
  gameplay jitter; the compatibility `pacing-debt-ms` field remains zero.
- Prepare software scaling before the host deadline, wait again, and invoke
  only the final display flip on the deadline. Variable scaling cost no longer
  moves an otherwise on-time visible frame.
- Extend diagnostics with the exact-rate/worker mode, queue capacity,
  queue high-water, queue starvation/repeated-frame count, worker starts, and
  the temporary thread-switch interval.
- Add bounded-worker transfer, input, audio, failure-propagation, and pool
  tests; exact-clock drift and suspend-gap tests; and frame-for-frame NROM batch
  comparisons against the literal scheduler. The release suite now contains
  107 deterministic tests.

## 0.9.1

- Remove predictive presentation dropping. Cartaconda now displays every
  emulated frame, eliminating the 30 Hz-looking gaps that could affect roughly
  one frame in six even when average emulation and drawing were comfortably
  inside the NTSC budget.
- Pace the actual display flip instead of flipping immediately and sleeping at
  the end of the loop. The frontend estimates scale/present cost, sleeps until
  that lead point, and completes the flip on the fractional-NTSC deadline, so
  variable game-logic time no longer becomes variable on-screen frame spacing.
- Repay rare late-frame timing debt over multiple intervals, limiting the
  recovery contribution to 0.25 ms per frame. Pauses longer than three frames
  reset the phase rather than causing a visible catch-up burst.
- Add presentation-jitter p95/max and outstanding pacing-debt diagnostics.
  Retain the presentation-skip fields for log compatibility; they remain zero
  under the steady-present scheduler.
- Evict scrolling-background cache entries incrementally instead of clearing
  thousands of cached rows at once, removing a periodic allocation/deallocation
  spike without changing rendered output.
- Add deterministic pacing tests for tail-heavy workloads and expand the suite
  to 98 tests.

## 0.9.0

- Reserve a continuously measured display cost before each host deadline.
  When a tail-heavy emulation frame has already consumed that reserve, skip
  only its obsolete presentation while still emulating every NES frame and
  generating every audio sample. Isolated spikes force the following frame to
  present; exceptional timing debt permits at most two catch-up skips.
- Replace tiny end-of-frame sleeps with a coarse sleep plus a bounded 0.35 ms
  precision tail, reducing Windows scheduler jitter without adopting an
  integer-60-Hz clock in place of the NES's fractional NTSC cadence. Explicitly
  disable display-driver vsync so a 60 Hz swap interval cannot compete with the
  software deadline.
- Count `late-frames` only when the completed host deadline is at least 1 ms
  late. Add presented-frame, presentation-skip, and maximum-skip-streak
  diagnostics so harmless timer overshoot is distinguishable from visible
  recovery work.
- Hold a four-chunk (4,096-sample) PCM reservoir before starting or restarting
  playback. Together with deadline-aware presentation recovery, this adds one
  chunk of protection against the observed 60–70 ms Windows scheduling spikes
  without increasing native Sound construction frequency.
- Preclassify and batch side-effect-free NROM `load/compare/BIT RAM; branch
  back` wait loops between exact PPU, APU, DMC, DMA, NMI, IRQ, and mapper
  boundaries. Preserve registers, flags, PC, open bus, page-cross timing, and
  serialized machine state against literal instruction execution.
- Add persistent, per-player gamepad remapping for buttons, D-pads/hats, and
  analog-axis directions. The Controls screen now has Keyboard and Gamepad
  tabs, conflict swapping, separate reset actions, controller-count feedback,
  and capture cancellation with Escape.
- Expose remapping directly from System Options and migrate settings to version
  2 while retaining existing keyboard, audio, display, recent-ROM, and save
  preferences.
- Expand regression coverage to 95 tests, including polling-loop differential
  state/dispatch, presentation catch-up bounds, controller-source compilation,
  remap conflict swapping, and settings validation/round trips.

## 0.8.1

- Stop calling pygame-ce 2.5.7's unsafe explicit `Channel.play` path. Its
  currently open native bug can index `channeldata[-1]` after an SDL start
  failure, corrupting memory and producing a later segmentation fault at an
  unrelated Python line. Start PCM through the guarded `Sound.play` path and
  retain its returned channel instead.
- Copy each 2 KiB PCM submission into an immutable Python owner before crossing
  into SDL and leave staged samples intact if the mixer cannot start them.
- Replace per-frame gamepad polling with cached `JOY*` event state, eliminating
  dozens of native HID calls per active controller per frame.
- Open and remove controllers only through their exact hot-plug events and
  unique SDL instance IDs, avoiding full-device rescans and duplicate handles.
- Extend diagnostics with the Cartaconda version, mixer driver, safe audio-start
  marker, event-input marker, frozen-runtime status, mixer-start failures, and
  gamepad event count.
- Add regression coverage for safe mixer-start failure, event-derived gamepad
  mapping, and zero-native-call input polling.

## 0.8.0

- Add an original action-platformer-shaped performance cartridge combining
  continuous game logic, NMI, controller polling, horizontal scrolling,
  four tonal audio channels, and a 64-sprite `$0200` OAM DMA every frame.
- Raise that gameplay workload from about 33 FPS in 0.7 to about 72 FPS on the
  validation host, moving the deliberately busy workload above NTSC real time.
- Advance the 513/514 held CPU clocks of OAM DMA in one exact device span,
  while retaining DMA parity, PPU/NMI timing, APU clocks, DMC fetch stalls, and
  serialized state against literal one-clock execution.
- Copy mirrored internal-RAM DMA pages by contiguous slice and transfer all 256
  OAM bytes in bulk, preserving OAMADDR wrap, final open-bus value, and
  side-effectful reads for device and cartridge pages.
- Index sprite rows once after OAM changes instead of scanning all 64 entries
  on every visible line, and cache palette-resolved sprite colors.
- Cache complete 512-pixel background world rows so ordinary horizontal
  scrolling becomes a wrapped slice instead of rebuilding 33 tiles on all 240
  scanlines every frame.
- Install direct operation/mode handlers for 147 official opcode encodings;
  fixed `JMP` and `NOP` paths remain inline, while BRK, indirect JMP, and
  unofficial encodings retain the generic reference decoder.
- Preclassify immutable NROM counter-loop addresses, avoiding three speculative
  PRG reads and failed batch checks on every ordinary game instruction.
- Coalesce non-MMC3 visible PPU spans until a register access, APU event, NMI,
  DMA/DMC event, or frame boundary; retain explicit dot-260 deadlines for MMC3
  scanline IRQs.
- Split large APU batches at exact frame-sequencer edges rather than reverting
  the entire span to one Python call per CPU clock.
- Inline the host-sample channel-level mix while preserving the existing
  nonlinear lookup curves and bit-identical PCM/state results.
- Extend `--diagnostics` with emulation p95/p99, drawing and active-loop timing,
  late-frame counts, and maximum pacing lateness.
- Expand regression coverage from 78 to 83 tests, including large APU spans,
  bulk mirrored-RAM DMA with nonzero OAMADDR, literal-vs-batched stall clocks,
  gameplay-workload integration, and diagnostic tail latency.

## 0.7.0

- Batch taken iterations of common `INX`/`DEX`/`INY`/`DEY` plus `BNE`
  cartridge-ROM counter loops between exact PPU, APU, DMC, DMA, NMI, IRQ, and
  mapper-visible boundaries.
- Preserve the counter loop's final zero iteration, branch-page penalty, flags,
  PC, open-bus result, total cycles, and serialized state against the literal
  scheduler.
- Raise the rendered fastest-rate looping-DMC workload from about 76 FPS to
  about 156 FPS on the validation host; reduce its 1,200-frame mean from
  13.3 ms to 6.5 ms and its measured maximum from 22.3 ms to 14.2 ms.
- Add a borrowed-frame API for the host frontend while retaining immutable
  frame bytes as the default core API.
- Bind one long-lived Pygame Surface directly to the PPU's stable framebuffer,
  eliminating a 184,320-byte copy and transient buffer-backed SDL Surface from
  every interactive frame.
- Increase PCM and mixer buffers to 1,024 and 2,048 samples respectively, use a
  three-chunk startup reservoir, and approximately double submitted native
  audio headroom while halving Sound construction frequency.
- Move PCM through buffer views instead of transient byte strings and retain
  every current/queued Sound explicitly until playback stops.
- Defer cyclic garbage collection during gameplay and collect after entering a
  menu, keeping collection pauses outside emulation/audio deadlines.
- Reuse controller handles across hot-plug refreshes, recover from removed HID
  handles, and release removed devices deterministically on the main thread.
- Initialize display, font, mixer, and joystick subsystems individually so
  `--mute` never opens SDL audio and `--no-gamepad` never opens an HID driver.
- Replace broad quit teardown with ordered mixer, Sound, joystick, font,
  Surface, display, and Pygame shutdown, including partially initialized runs.
- Add `--diagnostics` for Python/SDL/mixer and clean-run timing counters plus
  `--no-gamepad` for native HID isolation; document a Windows/frozen-build
  crash triage workflow.
- Require pygame-ce 2.5.7 or newer, which officially supports Python 3.14, and
  retain Pygame as the only runtime dependency after evaluating bulk/JIT
  numeric libraries against the profiled stateful CPU/bus hot path.
- Expand regression coverage from 70 to 78 tests for counter-loop differential
  state, page-cross timing, borrowed framebuffer identity, zero-copy PCM
  staging, native Sound retention, and new CLI switches.

## 0.6.0

- Rename the emulator to **Cartaconda — Python-powered 8-bit emulation** while
  retaining the `nes_from_scratch` module, legacy command alias, state format,
  and existing save-data directory compatibility.
- Add the supplied Cartaconda pixel-art logo to the launcher, package it as an
  installed application asset, and derive a matching window icon from it.
- Advance active DMC playback arithmetically between exact bit, sample, frame,
  and memory-fetch boundaries instead of forcing an APU call for every CPU
  clock.
- Preserve four-clock DMC CPU stalls and exact fetch ordering while allowing
  both deferred device scheduling and safe self-jump batching during DMC
  playback.
- Precompute the two hardware nonlinear DAC mixing curves, removing repeated
  floating-point divisions from the 44.1 kHz host-sample hot path.
- Skip host PCM generation entirely in muted and noninteractive modes while
  continuing to clock every emulated APU timer, IRQ, and DMC bus event.
- Raise the rendered fastest-rate looping-DMC stress workload from roughly
  25 FPS to 70 FPS on the same validation host, moving it above NTSC real time.
- Replace frame-only audio submission with a continuously serviced 512-sample
  PCM stream, four-chunk prebuffer, and two-slot mixer pump.
- Pump audio before and after emulation and every two milliseconds while frame
  pacing; after a true underrun, rebuild the reservoir before playback resumes.
- Lock mixer output to signed 16-bit mono, adopt the device's negotiated sample
  rate, and reapply that host rate after restoring a portable save state.
- Increase bounded staging headroom, retain four recent chunks during an
  exceptional backlog, and add explicit underrun/resynchronization counters.
- Redraw every launcher and pause-menu surface in an original NES-era pixel
  style with a hardware-inspired palette, aliased monospace lettering, clipped
  corners, hard shadows, pixel controls, and nearest-neighbor UI scaling.
- Add looping-DMC scheduler differential coverage, lossless multi-pump audio
  ordering tests, underrun recovery tests, a DMC benchmark workload, and
  headless visual checks for every menu.

## 0.5.0

- Coalesce APU and fast-PPU clocks across CPU-only RAM/ROM instruction spans,
  stopping at every register access, interrupt-visible event, DMA/DMC stall,
  mapper edge, and frame boundary.
- Add direct handlers for 33 frequent CPU opcodes and collapse hot arithmetic
  flag updates into single bit operations.
- Replace lossy one-frame Pygame sounds with a bounded PCM staging buffer that
  retains samples whenever the host channel and its queue are both occupied.
- Stream 1,024-sample audio chunks, restart safely after exceptional host
  backlog, and clear stale audio across pause, reset, ROM, and save-state
  transitions.
- Pace the frontend at the measured NTSC frame rate instead of an integer
  60 Hz, eliminating long-term audio production/consumption drift.
- Reuse the gameplay scaler destination and cached toast fonts to avoid
  per-frame host-surface and font allocation.
- Add scheduler/reference differential tests, specialized-opcode comparisons,
  audio-backpressure tests, and an instruction-synchronized benchmark control.

## 0.4.0

- Load `.nes` images directly from ZIP archives without extracting files.
- Open one-ROM archives automatically and show an in-app member picker for
  multi-ROM collections.
- Preserve the selected archive member across reloads, save states, battery
  saves, and direct command-line launches.
- Show `.zip` collections in the launcher browser and recent-game shelf.
- Add `--zip-member` for deterministic info, trace, and headless commands.
- Bound archive entry counts and expanded ROM size, reject encrypted or corrupt
  members, and add archive/CLI regression coverage.

## 0.3.0

- Add a cheerful, scalable launcher that can start without a ROM path, browse
  folders, reopen recent cartridges, and safely load, reload, or unload games.
- Add an in-game pause overlay with live game preview and quick access to every
  host-side feature.
- Add a ten-slot save-state manager with frame thumbnails, timestamps, restore
  controls, and confirmed deletion.
- Add persistent volume, mute, fullscreen, 2x/3x/4x window-size, recent-game,
  and keyboard-binding preferences.
- Add two-player keyboard rebinding with conflict swapping, defaults restore,
  and controller-driven menu navigation.
- Add filesystem-model and launcher CLI regression tests plus headless visual
  and interaction smoke validation.

## 0.2.0

- Batch APU clocks arithmetically between observable events, with an exact
  per-cycle fallback for DMC fetches and frame-sequencer boundaries.
- Add direct CPU addressing dispatch, cached bus/mapper call targets, NROM
  read specialization, and fixed-opcode fast paths.
- Add event-aware PPU stepping plus separately invalidated background and
  sprite-composition scanline caches.
- Batch side-effect-free cartridge-ROM self-jump idle loops while preserving
  interrupt boundaries and serialized machine state.
- Add optimized/reference differential tests and a deterministic benchmark.

## 0.1.0

- Initial end-to-end NTSC NES implementation.
