# Cartaconda

**Python-powered 8-bit emulation.**

Cartaconda is an original, specification-driven Nintendo Entertainment System
emulator written in Python. No prebuilt CPU core, emulator framework, or
existing emulator source code is used. Pygame is only the host adapter for the
window, audio device, keyboard, and game controllers.

The project currently targets the North American NTSC console. It emulates the
Ricoh 2A03 CPU/APU, Ricoh 2C02 PPU, CPU and PPU buses, two standard controllers,
cartridge hardware, battery RAM, and portable save states.

> ROM images are not included. Use public-domain homebrew or cartridge images
> that you personally have the legal right to use.

## Quick start

Python 3.11 or later is required.

```bash
cd cartaconda
python -m pip install -e .
python tools/make_demo_rom.py demo.nes
cartaconda
```

The command opens the game shelf, where you can browse to `demo.nes`. Raw
`.nes` images and `.zip` collections are supported. Pass either path to start
it directly:

```bash
cartaconda demo.nes
cartaconda homebrew-collection.zip
```

The previous `nes-from-scratch` executable remains as a compatibility alias.

You can also open the launcher without installing the command:

```bash
PYTHONPATH=src python -m nes_from_scratch
```

Inspect a cartridge or run deterministic headless emulation:

```bash
cartaconda game.nes --info
cartaconda game.nes --headless-frames 60
cartaconda game.nes --trace 100
```

If a ZIP contains several ROMs, the graphical launcher presents a picker.
For noninteractive commands, select the exact archive member:

```bash
cartaconda collection.zip \
  --zip-member "games/demo.nes" \
  --headless-frames 60
```

The interactive frontend defaults to its batched scanline presentation path for
playability. `--cycle-accurate` selects the slower dot-by-dot PPU pipeline.
Headless and trace modes use the dot pipeline automatically so diagnostics
remain deterministic. The batched path applies visual state at scanline
boundaries; software built around mid-scanline palette or scroll effects should
use `--cycle-accurate`.

## Launcher and in-game UI

The host UI is drawn entirely in Pygame and does not use an emulator framework
or prebuilt console components. Its original NES-era pixel treatment uses
hard-edged panels, a compact hardware-inspired palette, and aliased
bitmap-like lettering. The default 3x window presents the 960x720 UI exactly;
other fractional menu sizes use a text-safe filtered scale. It provides:

- an in-app `.nes`/`.zip` browser with a Windows drive picker and a
  recent-cartridge shelf;
- automatic one-ROM ZIP loading and a picker for multi-ROM collections;
- safe load, reload, unload, and desktop-exit actions;
- a ten-slot save-state manager with screenshots and timestamps;
- a bounded 30-second rewind history with one- and five-second shortcuts;
- persistent volume, mute, 2x/3x/4x window, and fullscreen settings;
- remappable two-player keyboard and gamepad controls with conflict swapping;
  and
- mouse, keyboard, D-pad, analog-stick, and controller-button navigation.

Press `Esc` while playing to open the overlay. Settings and controls are also
available from the launcher before a game is loaded. In the ROM browser,
choose `DRIVES` to switch between mounted Windows volumes; `UP` at a drive
root opens the same drive picker.

## Performance

The interactive path keeps every emulated clock domain deterministic while
reducing Python dispatch overhead:

- A two-frame, fixed-memory producer/consumer pipeline lets inexpensive frames
  absorb occasional expensive CPU/PPU frames. The worker owns only the Python
  console core; Pygame, SDL display, mixer, and controller objects remain on
  the main thread. Optional hot-block generation is capped at one new block per
  gameplay frame. Rewind capture waits for sustained full-queue headroom after
  a slow section, then a separate convenience worker performs its zlib
  compression without blocking the frame producer.
- CPU-only RAM/ROM instruction spans defer and coalesce APU/PPU work until the
  next device access, interrupt-visible event, mapper edge, or stall.
- NROM and banked-mapper instructions proven to touch only CPU RAM, the stack,
  or an immutable mapped PRG window execute inside one bounded scheduler
  dispatch. Discrete mappers and MMC2/MMC4 precompute proof tables for every
  possible 32 KiB mapping and swap references with the bank. The batch ends
  before the next PPU/APU/DMC event and yields to the specialized wait-loop
  paths. Device I/O, interrupt-unmasking instructions, DMA, and mapper writes
  stay literal.
- A repeatedly executed safe basic block is translated after eight
  observations into a fixed pure-Python function. Registers and flags stay in
  locals across arithmetic, loads/stores, branches, calls, returns, and short
  control bridges, removing the remaining per-opcode dispatch from hot banked
  code. Official `(d,X)` and `(d),Y` accesses resolve their effective address
  inside the generated block, then continue only when both the real and dummy
  access are proven to remain in CPU RAM or immutable PRG. Device I/O, mapper
  writes, DMA, DMC, interrupt hazards, unofficial opcodes, and mutable code
  stay outside the cache. It retains up to 4,096 established blocks without a
  periodic whole-cache purge. JSR, RTS, and JMP may continue the same proven
  device-safe span through as many as eight MMC3 slots only when each target
  already has a compiled hot block, avoiding a scheduler round trip at steady
  fixed-bank helper boundaries without admitting cold setup work.
- 147 official opcode encodings use direct operation/mode handlers, fixed JMP
  and NOP paths remain inline, hot arithmetic updates status flags with one
  bitwise assignment, and safe batches classify opcodes without resolving a
  decoder dataclass for each instruction.
- APU channel timers advance arithmetically between frame-sequencer, sample,
  and DMC events. When no DMC fetch can occur, a dedicated span avoids testing
  that impossible deadline at every generated host sample. Long spans also
  retain all channel, nonlinear-mixer, and three-stage filter state in locals
  until the final sample; short spans keep the lower-setup-cost event path.
  Inaudible channel timers, silent DMC shifts, and noise LFSR clocks collapse
  across a quiet span without changing their final hardware state or PCM.
- Recognized CPU-driven sample loops carry their exact `$4011` write values
  and cycle offsets into one APU event loop. DAC changes and 44.1 kHz mixer
  boundaries are merged without changing their equality ordering, and the PPU
  advances once across the device-safe span.
- The two nonlinear DAC response curves are generated once as lookup tables,
  removing repeated divisions from the host-sample loop.
- Muted, trace, and headless runs skip host PCM generation while continuing to
  clock all emulated APU state and DMC bus activity.
- Active DMC playback is split only at exact bit and memory-fetch boundaries,
  so four-clock bus stalls remain cycle ordered without forcing the complete
  scheduler back to one Python call per emulated clock.
- Mapper 9 RAM wait loops can advance across several DMC memory requests in
  one scheduler operation. Every four-cycle DMA hold remains in physical
  APU/PPU time while the 6502 executes only its original loop cycles; PPU/NMI,
  APU-frame, interrupt, write, and DMA hazards end the batch.
- CPU addressing uses prebound bus calls and direct mode dispatch; fixed NROM
  and active immutable PRG-window reads bypass redundant cartridge wrappers.
- Fast PPU output caches 512-pixel background world rows, viewport scanlines,
  and composed lines. Horizontal scrolling is a wrapped row slice; VRAM,
  palette, mapper, and OAM writes invalidate the affected cache layer.
  Sparse nametable updates version only their exact physical tile/attribute
  rows, so unrelated side-scrolling level rows remain reusable.
  Viewport and sprite-composed rows are admitted only after their exact frame
  signature repeats, preventing continuously scrolling scenes from allocating
  thousands of rows that would be evicted before reuse.
- Background and sprite pattern planes cross the mapper boundary as one
  ordered pair. Sprite rows additionally cache their compact nontransparent
  offset/pixel sequence, avoiding eight repeated bit tests whenever identical
  pattern data is composed at another position.
- MMC3 exposes independent effective mappings for its two 4 KiB pattern-table
  halves. A sprite-only bank change therefore preserves unchanged background
  world rows and viewports; only sprite composition is refreshed. An 8x16
  sprite conservatively depends on both halves. Background and sprite palette
  halves are tracked independently for the same reason.
- MMC2 pattern reads retain their hardware side effects in the interactive
  renderer. A cached address plan still performs every ordered CHR fetch.
  Repeated identical scanlines can reuse pixels only with the exact incoming
  latch state, and restore the recorded outgoing latch state on a hit. Each
  33-tile plan is packed into 66 bytes, allowing a complete horizontal scroll
  cycle to remain resident without nested-tuple allocator churn.
- An unchanged scanline remains in the retained framebuffer instead of being
  copied out and recomposed. MMC2 tile runs are content-addressed by their
  complete fetch plan, palette, CHR bank bases, and incoming latches, allowing
  recurring scene rows to survive unrelated cache invalidations safely.
- Sprite evaluation indexes each OAM entry across the scanlines it can touch
  once after a change, instead of testing all 64 sprites on all 240 lines.
- The conventional `$0200` sprite-DMA path copies mirrored CPU RAM and OAM in
  bulk, then advances the 513/514 held clocks as one exact device span.
- Side-effect-free `JMP $self` cartridge-ROM idle loops advance in bounded
  batches. Batches stop before APU, PPU, NMI, frame, and MMC3 IRQ boundaries.
- Common register-counter delay loops (`INX`, `DEX`, `INY`, or `DEY` followed
  by `BNE`) collapse their taken iterations between the same hardware-visible
  boundaries. NROM candidates and every immutable MMC2 PRG mapping are
  preclassified once, so ordinary instructions do not repeatedly probe for a
  loop. The final zero iteration, interrupt sampling, status flags, open bus,
  page-cross cycle, and state remain literal.
- Side-effect-free NROM and banked-mapper RAM wait loops (`load`, `compare`, or
  `BIT` followed by a branch back) collapse between the same event boundaries.
  This targets the frame-complete polling pattern used by NMI-driven software
  without guessing across a device event or interrupt boundary.
- Four-record cooperative scheduler scans can collapse complete 93-cycle idle
  passes in O(1). Admission is keyed to the complete immutable instruction and
  control-flow signature plus the live RAM/register state; a ready record,
  changed status, pending interrupt, or partial scan stays literal.
- Complete uninterrupted visible scanlines execute in one PPU operation while
  preserving MMC3 A12 phases, overflow, sprite-zero timing, scrolling, and
  frame boundaries. When MMC3 IRQ is enabled, a constant-time non-mutating
  counter projection finds the exact future A12 edge that can assert IRQ,
  crossing all earlier non-asserting edges and scanlines in one span.
  Pre-render edges remain explicit, and any PPU register access flushes prior
  clocks before its side effect.
- Large APU spans split at the exact frame-sequencer edge rather than falling
  back to a per-clock loop for the entire batch.
- DMA, DMC memory fetches, register I/O, save states, and the dot reference
  renderer retain their explicit event paths.
- The worker recycles two fixed 184,320-byte transfer buffers. The main thread
  copies a completed frame into one long-lived buffer-backed SDL Surface; no
  per-frame framebuffer or Surface allocation crosses the native boundary.
- Host audio uses retained 2,048-sample Sounds, a two-chunk 4,096-sample
  startup reservoir, and a 2,048-sample SDL mixer buffer. The playing and
  queued channel slots hold about 93 ms of submitted PCM at 44.1 kHz without
  increasing startup buffering.
- PCM moves from the APU array into staging and then the mixer through borrowed
  buffer views, avoiding two intermediate byte-string allocations.
- Both mixer slots are pumped whenever a completed frame arrives and every two
  milliseconds while the main thread waits. Emulation can no longer monopolize
  the same loop that services host audio. Cyclic garbage collection is deferred
  until a menu while a game is actively meeting audio deadlines.
- Every native PCM submission uses the dedicated channel's `queue()` operation,
  which fills the next slot while active and starts immediately when idle.
  A transient queued-Sound promotion is never stopped or replaced, and a
  failed submission retains the exact chunk for the next two-millisecond pump.
- The frontend schedules presentation itself—not the arbitrary end of the
  loop—on each fractional-NTSC deadline. Windows uploads only the dirty native
  256x240 RGB frame to a streaming texture and lets the accelerated SDL
  renderer scale it with nearest-neighbor sampling. The portable fallback
  prepares its software-scaled Surface at a measured lead point and leaves
  only the final flip deadline-sensitive.
- Deadlines advance from an absolute fractional-NTSC clock. Host lateness never
  changes the emulated clock rate, so timing debt cannot accumulate or overfill
  audio. Menu, display-mode, debugger, and system-suspend gaps start a fresh
  presentation segment instead of polluting gameplay jitter.
- A measured 5 ms Python thread-switch interval avoids the repeated GIL
  handoffs caused by the earlier 1 ms setting. The two-frame queue absorbs core
  variance while the main thread still pumps audio and events every two
  milliseconds. A bounded sub-millisecond tail keeps Windows deadline waits
  precise.
- Scrolling-background caches use a bounded LRU and evict one old row at a
  time instead of clearing the full working set. A new recurring Mapper 4 map
  admits one third of its rows on the first appearance and the remainder on
  the second, so a level transition cannot decode every double-width row in
  one producer frame or pay for a third admission pass.

The profiled hot path is branch-heavy CPU/bus scheduling over interconnected
mutable hardware state, not bulk numeric array work. NumPy would not accelerate
that control flow; introducing a JIT/compiler runtime would also enlarge and
complicate the first cross-platform release. Cartaconda therefore keeps
Pygame-ce as its only runtime dependency and removes the measured Python
dispatch directly. The fixed scheduler batches remain usable in ordinary
CPython and frozen builds without a compiler warm-up.

The included original gameplay workload continuously executes 6502 game logic,
scrolls, polls input, drives four tonal channels, and performs 64-sprite OAM
DMA on every NMI. The final 2.0 validation run reaches 142.4 FPS (2.37x NTSC)
over 1,200 frames, or 131.9 FPS (2.19x) while capturing rewind history, with
the same final frame hash and serialized machine state.

The equivalent Mapper 9 workload also switches its MMC2 PRG bank every frame
and renders a latch-heavy scrolling scene. It reaches 125.4 FPS (2.09x NTSC)
over 1,200 frames in 2.0, up from 31.8 FPS in unmodified 1.1.0 and 84.7 FPS in
1.1.1, while preserving every frame and final serialized machine state.

Version 2.0.2 adds transition-heavy Mapper 4 workloads modeled on the
CHR/mirroring and fixed-bank pressure seen around action-game scene changes.
MMC3 now uses cached PRG/CHR slots, stable executed-bank CPU spans,
sprite-zero-only status probes, recurring-state viewport caches, and
coalesced A12 bookkeeping while IRQ output is disabled. Continuous CHR and
PRG register-churn cartridges permanently guard the two startup regressions
without bundling commercial game data.

Version 2.0.3 fixes the separate Mapper 9 forced-blank transition used before
the first fight in Mike Tyson's Punch-Out!!. Safe nametable streams now retain
exact logical `$2007` timestamps while avoiding a full APU/PPU scheduler entry
for every byte. Audio-only black frames also bypass framebuffer transfer and
software rescaling.

Version 2.0.4 addresses the workload exposed by real 2.0.3 diagnostics:
Punch-Out!! also decodes scene commands through dynamic 6502 indirect
addressing. Runtime-resolved RAM/immutable-ROM streams now stay inside bounded
CPU spans, while DMC-active and device-visible accesses retain literal timing.
Unchanged gameplay frames also skip the final SDL blit and display flip, not
just the framebuffer copy and scaler.

Version 2.0.5 fixes the remaining Punch-Out!! startup audio delay. The
black-screen bell is a CPU-driven sampled-audio routine that writes every
output level directly to `$4011`; it is not the nametable decoder or ordinary
DMC playback. A code-signature-gated executor now preserves every DAC value
and exact write clock while collapsing the surrounding RAM/ROM delay loop.
Forced-blank `$2002` polling is batched separately. Both paths retain literal
execution near device, frame, NMI, DMC, and stream-transition boundaries.

Version 2.0.6 removes the remaining per-sample Python dispatch cost revealed
by Windows diagnostics. A bounded software-DAC span now carries many exact
`$4011` write events and crosses ordinary non-wrapping source-byte refills.
Each write is still applied at its original APU/PPU clock; only redundant
Python scheduler re-entry is removed.

Version 2.1.0 closes the next scheduler layer. Timestamped DAC writes and host
sample boundaries now share one APU loop; the PPU advances once across that
bounded audio-only span. Mapper 9 forced-blank PPUDATA values are committed as
one ordered stream with a single cache invalidation, and Mapper 4 safe spans
index the active raw 8 KiB classification table directly. It also corrects the
fast forced-blank PPU shortcut at the vblank-set and pre-render-clear dots.
Against the packaged 2.0.6 build on the same validation host, the forced-blank
Mapper 9 workload improved from 42.68 to 143.79 FPS and the Mapper 4 gameplay
workload from 86.43 to 95.04 FPS. A 420-frame exact-NTSC run of the reported
startup sequence recorded no frame-queue starvation or virtual audio
underrun.

Version 2.1.1 targets the visible first-fight regression exposed by the 2.1.0
log. Punch-Out!! repeatedly polls sprite-zero through `$2002` to time its
split-screen scroll. Cartaconda now coalesces only iterations proven to finish
before the next hit/clear, DMC, APU, NMI, or MMC2 latch boundary. The captured
fight segment improves from 21.620 to 9.558 ms/frame on the validation host
while 300 consecutive frames retain identical complete machine state and
pixels. Gameplay presentation also converts at 256x240 into the display's
native format before scaling, avoiding a large per-frame pixel-format
conversion on Windows.

Version 2.1.2 addresses the remaining Mapper 9/DMC regression shown by the
2.1.1 field log. A read-only RAM wait can now span several precisely accounted
DMC fetches instead of re-entering the full scheduler for each four-cycle DMA
hold. Unchanged PPU lines remain in the framebuffer, recurring MMC2 tile runs
stay content-addressed, and Windows gameplay uses a native streaming texture
for GPU nearest-neighbor scaling. In five captured-state runs on the validation
host, the core averaged 6.783 ms/frame versus 9.160 ms in 2.1.1 and 7.651 ms
in 1.1.1, with exact complete machine-state parity across 300 frames.

Version 2.1.3 uses row-level MMC2 dependencies so a nametable update no longer
invalidates unrelated retained scanlines. In a 900-frame supplied-ROM
continuation, 86% of scanline slots replayed and the core averaged 5.940
ms/frame with an 8.870 ms p95. PCM blocks now keep about 93 ms inside
SDL_mixer—enough to bridge the reported 87.914 ms Windows scheduling
outlier—without increasing the existing 4,096-sample startup reservoir. The
output stage also implements the NES's complete 90 Hz/440 Hz high-pass and
14 kHz low-pass chain, removing residual DC pops after sampled sounds.

Version 2.2.0 removes the active-IRQ Mapper 4 scheduler regression exposed by
Mega Man 5 diagnostics. Instead of stopping the worker at every possible MMC3
A12 phase, Cartaconda projects the counter to the exact edge that can assert
IRQ. Executed 8 KiB code banks compile on first use, effective PPU mapping
tokens are cached, and a signature-proven four-record cooperative idle scan is
collapsed between hardware deadlines. On the release workload, the packaged
2.1.3 worker averaged 20.750 ms/frame with 58 queue starvations and eight audio
underruns; 2.2.0 averaged 5.088 ms/frame with neither starvation nor underrun,
a 4.08x throughput increase. The optimized and instruction-synchronized paths
retain identical complete state and pixels.

Version 2.2.1 corrects host-UI presentation without changing emulation timing.
Windowed modes are now 4:3 containers sized 640x480, 960x720, and 1280x960,
while the 256x240 game viewport remains an exact 2x, 3x, or 4x image centered
inside each window. Fractionally scaled menus use filtered resampling so small
glyph strokes are retained. The ROM browser also includes a `DRIVES` view for
moving between mounted Windows volumes.

Version 2.2.2 addresses the remaining Mapper 4 core cost reported by Mega Man
5 diagnostics. Scheduler-approved RAM and immutable-PRG spans now keep the
complete official load/store/ALU, transfer, stack, call/return, and branch
families in local CPU state. The general renderer also tracks physical
nametable tile and attribute rows, so one changed row no longer rebuilds every
retained scanline. A representative call/RAM-heavy loop is 35% faster than
2.2.1, sparse nametable-update frames are 4.8x faster, and all four Mapper 4
release workloads improve with identical final framebuffer hashes.

Version 2.2.3 removes the next layer of hot opcode dispatch exposed by the
2.2.2 field log. Repeated safe RAM/immutable-PRG basic blocks become bounded
pure-Python functions, while device and interrupt boundaries retain the
ordinary executor. Long APU sample spans publish channel/filter state once,
ordinary PPU fetches use ordered pattern pairs, reusable sprite rows are
packed, and a transient WASAPI start race is retried without dropping PCM.
Against packaged 2.2.2, the same 900-frame workloads improve from 125.49 to
236.36 FPS for NROM, 110.53 to 192.70 FPS for Mapper 9, and 127.80 to 218.14
FPS for Mapper 4 with identical framebuffer hashes. Four isolated threaded
soaks complete with zero queue starvation and zero audio underruns.

Version 2.2.4 removes the periodic Mapper 4 render-cache flush exposed by the
2.2.3 Mega Man 5 log. MMC3 background and sprite pattern dependencies now
invalidate independently, so a sprite-bank change no longer recomposes an
unchanged scrolling background. Against packaged 2.2.3, the deterministic
1,800-frame cadence lowers p95 from 15.994 to 4.889 ms and bank-change average
from 16.815 to 4.274 ms, with the same framebuffer hash. The translated CPU
cache also retains its established working set instead of periodically
discarding every compiled block.

Version 2.2.5 removes the general per-cycle overhead still visible in the
2.2.4 Mega Man 5 log. Generated CPU blocks now include guarded official
indirect accesses, immutable code starts are preclassified into three safety
classes, quiet APU channel clocks collapse arithmetically, complete visible
PPU lines batch between device events, and MMC3 IRQ projection is constant
time. A stronger 1,200-frame Mapper 4 workload moves sprites every frame and
periodically changes sprite palette and CHR state. Across three identical
runs, its median frame time falls from 5.102 ms in packaged 2.2.4 to 3.003 ms
in 2.2.5 (1.70x throughput) with the same framebuffer hash. Separate
900-frame Mapper 4 and active-IRQ worker/audio soaks complete with zero queue
starvation and zero audio underruns.

Version 2.2.6 fixes the stable-code-slot regression exposed specifically by
side-scrolling Mega Man 5 gameplay. Version 2.2.5 stopped checking MMC3's
classification gate once the four-bank tuple stopped changing; code reached
later through JSR/JMP could therefore remain on the literal CPU path for the
entire level even though title and stage-select code stayed fast. The current
PC slot now completes its two-observation gate independently of mapper writes.
On the new 300-frame cross-slot scrolling workload, packaged 2.2.5 improves
from a 33.91 FPS median to 318.64 FPS (9.40x) with identical pixels. A repeated
900-frame worker/audio soak averages 3.462 ms/frame with zero queue starvation
and zero audio underruns.

Version 2.2.7 targets the remaining scrolling variance in the 2.2.6 field
log. Sparse nametable updates retain unrelated content-keyed world rows,
complete visible-line batches share mapper/palette/signature work, and safe
CPU spans continue through already classified MMC3 helper slots. Optional
runtime translation and rewind compression now consume only buffered producer
headroom. Against packaged 2.2.6, the 900-frame cross-slot scrolling workload
uses 10.6% less core time with the same framebuffer hash. Its final 900-frame
exact-NTSC worker soak averages 3.328 ms with a 5.053 ms p99, zero queue
starvation, and zero audio underruns. Confirmed underruns also receive a short
fade to silence and a matching recovery fade, removing both discontinuities
without modifying uninterrupted PCM.

Version 2.2.8 corrects the 2.2.7 tail regression exposed by the next field
log. The old queue gate still allowed many optional Python blocks to compile
inside one frame while a single packet remained buffered; combined with broad
cross-slot recursion, this produced the reported 37.750/61.000 ms p95/p99
core tail. Runtime generation is now deterministic: at most four blocks in
each of the first two hidden reservoir-fill frames and one in every gameplay
frame. Cross-slot continuation accepts only an already-compiled destination,
and a one-frame cache sentinel makes repeated deferred starts inexpensive. A
1,024-start stress test lowers median p99 from packaged 2.2.7's 42.933 ms to
8.257 ms. Three 900-frame combined field soaks complete with zero queue
starvation and zero audio underruns.

Version 3.0.0 fixes the native audio transport itself. Version 2.2.8 could
observe `busy=False` during SDL_mixer's queued-Sound promotion, call a separate
start path, and stop valid PCM while trying to recover it. Cartaconda now uses
`Channel.queue()` exclusively, accepts a queued handoff as healthy, requires a
stable 12 ms idle window before declaring loss, and never injects a synthetic
Sound after a gap. The reported 318-underrun/278-concealment cycle is covered by
a lossless 202-chunk handoff regression. Mapper 4's periodic world-row cache
reset is also replaced by incremental LRU eviction and asymmetric two-pass map
admission, while rewind compression runs off the emulation owner thread. A
1,200-frame exact-NTSC combined field soak completes with zero queue
starvation, zero virtual audio underruns, at least 3,610 staged PCM samples,
intact rewind history, and clean shutdown.

Run the deterministic benchmark on your machine:

```bash
PYTHONPATH=src python tools/benchmark.py
```

For controlled release measurements, run isolated repeats and compare the
median plus p95 frame time rather than a single sample:

```bash
python tools/benchmark_repeats.py --repeats 7 --warmup-runs 1 \
  --cooldown-ms 500 --output-json benchmark_summary.json
```

The runner launches a fresh Python process per run, reports `median_fps`,
`p95_frame_ms`, and coefficient-of-variation (`cv_pct`) for each mode, and
writes machine-readable JSON when `--output-json` is provided.

It reports the optimized interactive path, the same path with idle batching
disabled, an instruction-synchronized control, the dot-by-dot reference path,
a rendered fastest-rate looping-DMC workload, and the NMI/OAM/scroll/tonal
gameplay workloads for NROM, Mapper 9, and Mapper 4. Pass
`--mmc3-irq-frames 900` for the active-IRQ/cooperative-scheduler regression.
Pass `--mmc3-sprite-frames 1200` for the moving-sprite, palette, and CHR-bank
cadence used by the 2.2.5 performance gate. Pass `--mmc3-slot-frames 900` for
the stable cross-slot scrolling path optimized through 2.2.7. Pass
`--mmc3-field-frames 1200` for the combined scrolling, sprite, sparse
nametable, palette, and CHR cadence used by the 2.2.8 tail gate.
Timing varies by host; matching deterministic results and the test suite are
the correctness checks.

Exercise the real worker queue, virtual audio reservoir, and rewind captures:

```bash
PYTHONPATH=src python tools/soak.py --workload nrom --frames 900 --strict
PYTHONPATH=src python tools/soak.py --workload mmc2 --frames 900 --strict
PYTHONPATH=src python tools/soak.py --workload mmc3 --frames 900 --strict
PYTHONPATH=src python tools/soak.py --workload mmc3-irq --frames 900 --strict
PYTHONPATH=src python tools/soak.py --workload mmc3-slot --frames 900 --strict
PYTHONPATH=src python tools/soak.py --workload mmc3-field --frames 900 --strict
```

Run workloads in separate processes for release measurements so one profile
cannot inherit the previous profile's allocator and host-scheduler state.

## Controls

| NES input | Player 1 default | Player 2 default | Typical gamepad |
| --- | --- | --- | --- |
| D-pad | Arrow keys | W, A, S, D | D-pad / left stick |
| A | Z | C | Bottom face button |
| B | X | V | Right face button |
| Select | Right Shift | Q | Back / Select |
| Start | Enter | E | Start |

Emulator controls:

| Key | Action |
| --- | --- |
| `0`–`9` | Select save-state slot |
| `F5` | Save current state |
| `F9` | Restore current state |
| `F11` | Toggle fullscreen |
| `F12` | Save screenshot |
| `Backspace` | Rewind about one second |
| `Shift` + `Backspace` | Rewind about five seconds |
| `P` | Open the pause menu |
| `R` | Reset console |
| `Esc` | Open / close the current menu |

Keyboard and per-player gamepad bindings can be changed from System Options →
Remap Controls. Gamepad capture accepts buttons, D-pad/hat directions, and
analog-axis directions; duplicate sources on the same player are swapped.
In menus, arrow keys or the controller D-pad/stick move focus, `Enter` or the
bottom face button accepts, and `Esc` or the right face button goes back. Use
the Guide button or the mapped Start+Select chord to open the menu from a
controller.

Save states, thumbnails, settings, and battery saves default to `.cartaconda`
inside the user's home directory. An existing legacy `.nes-from-scratch`
directory is reused automatically so upgrades keep their saves. `--state-dir`
and `--battery-dir` override the game-data locations.

## What is emulated

- Complete official NMOS 6502 instruction set as implemented by the 2A03
- Stable unofficial opcodes used by released software
- Addressing modes, stack, vectors, IRQ/NMI, branches, page penalties, and the
  indirect-jump page-wrap behavior
- 2 KiB mirrored work RAM, PPU register mirrors, open-bus retention, sprite DMA,
  controller serial ports, and DMC CPU stalls
- PPU background fetch/shifter pipeline, loopy scrolling registers, nametable
  mirroring, buffered VRAM reads, palettes, grayscale/emphasis, 8×8 and 8×16
  sprites, overflow, priority, sprite-zero hit, VBlank, odd frames, and NMI
- Both pulse channels, sweep and envelope units, triangle linear counter, noise
  LFSR, DMC sample reader, frame sequencer, length counters, IRQs, and the NES
  nonlinear audio mixer with 90 Hz/440 Hz high-pass and 14 kHz low-pass output
  stages
- iNES and common NES 2.0 headers
- Mapper 0 (NROM), 1 (MMC1), 2 (UxROM), 3 (CNROM), 4 (MMC3), 7 (AxROM),
  9 (MMC2/PxROM), 10 (MMC4/FxROM), 11 (Color Dreams), 13 (CPROM),
  34 (NINA-001/BNROM), 66 (GxROM), 71 (Camerica), 87 (Jaleco),
  94 (UN1ROM), 140 (Jaleco JF-11/JF-14), and 180 (Crazy Climber)
- Keyboard, two standard controllers, audio, atomic save states, bounded
  rewind, ROM identity checking, battery RAM, tracing, headless execution, and
  a persistent launcher
- A fast scanline presentation path for interactive CPython use, alongside the
  dot-by-dot reference path used for diagnostics

## Accuracy boundary

This is a serious first complete implementation, not a claim of universal
cartridge compatibility. The base NTSC console is represented end to end, but
the historical cartridge ecosystem contains hundreds of extra chips. A game
using a mapper outside the list above will stop with a clear error instead of
silently running with incorrect wiring.

Known next-stage accuracy work includes PAL/Dendy timing, revision-specific PPU
OAM corruption and long-timescale open-bus decay, explicit NEC/revision-A
MMC3 and MMC6 board behavior, expansion-audio chips, light guns, and additional
mappers. See [docs/ROADMAP_2.0.md](docs/ROADMAP_2.0.md) for the measured 2.0
gates, [docs/VALIDATION.md](docs/VALIDATION.md) for the validation ladder, and
[docs/RELEASE_2.0.md](docs/RELEASE_2.0.md) for the frozen 2.0 validation
record. [docs/RELEASE_3.0.0.md](docs/RELEASE_3.0.0.md) records the current
performance/audio gate, [docs/RELEASE_2.2.8.md](docs/RELEASE_2.2.8.md)
preserves the bounded-translation gate, [docs/RELEASE_2.2.7.md](docs/RELEASE_2.2.7.md)
preserves the scrolling-cache update, [docs/RELEASE_2.2.6.md](docs/RELEASE_2.2.6.md)
preserves the stable-slot correction,
[docs/RELEASE_2.2.4.md](docs/RELEASE_2.2.4.md)
preserves the Mapper 4 cache gate, and
[docs/RELEASE_2.2.1.md](docs/RELEASE_2.2.1.md) preserves the UI patch gate.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
describes the subsystem design.

## Native crash diagnostics

If SDL reports `pygame_parachute: Segmentation Fault`, the displayed Python
line is an interrupted Python thread's location, not necessarily the native
faulting subsystem. Start a reproducer with:

```bash
cartaconda game.nes --diagnostics
```

Use `--mute` and `--no-gamepad` separately to isolate the mixer and controller
driver boundaries. Cartaconda 0.8.1 and later avoid pygame-ce 2.5.7's unsafe
explicit `Channel.play()` failure path and derive gamepad state from SDL
events. It prints the application version and host-path markers at startup
and, after a clean exit, emulation wall and worker-CPU timing, draw and
active-loop timing, meaningful pacing lateness, presentation jitter,
bounded-worker queue high-water/starvation, audio counters, and gamepad event
count. Version 2.0.3 also reports rewind deferrals, unchanged-video frames,
PPU render/probe/invalidation/coalescing/deferred-write counts, forced-blank
CPU stream batches, and MMC3 code-bank compile/deferral counts. Version 2.0.4
adds unchanged-display skips, CPU literal/batch/device/stall counts, DMC
fetches, and cycle-weighted hot CPU span addresses. Version 2.1.2 also reports
the selected display backend/renderer, retained PPU scanlines, and DMC RAM-poll
batch/fetch counts. In 2.1.3, `PCM-chunk=2048` with
`PCM-prebuffer=4096` identifies the wider native underrun guard without extra
startup buffering. Version 2.2.0 makes an active MMC3 IRQ compatible with long
device spans: on a healthy Mapper 4 run, `cpu-device-flushes` should fall by an
order of magnitude, executed fixed banks should compile immediately, and
`cpu-poll-batches` can rise while a cooperative scheduler is idle.
Version 2.2.3 adds `cpu-translated-blocks`; it should rise as immutable hot
code passes the eight-observation gate and then stabilize rather than growing
every frame. Version 2.2.4 adds `ppu-mapper-background-preserves`; on Mapper 4
it counts mapper writes that safely retain the current background mapping.
When this rises, the broad mapper invalidation count can rise too without a
matching background-recomposition spike. Version 2.2.5 adds
`ppu-palette-background-preserves` for sprite-only palette changes; those
writes likewise should not trigger background reconstruction. Version 2.2.6
also ensures `cpu-bank-compiles` rises when gameplay first enters a previously
unused MMC3 code slot, even without a mapper write; `cpu-literal-instructions`
should no longer grow by thousands per level frame. Version 2.2.7 adds
`cpu-block-deferrals`, `rewind-captures`, `audio-concealments`, and
`audio-recovery-fades`. Version 2.2.8 makes block deferral deterministic and
adds `cpu-block-compile-peak`: the first two reservoir-fill frames may produce
a peak of four, while every gameplay frame is capped at one. Deferrals mean a
hot optional translation used the generic safe path and was spread into a
later frame; they are not an emulation fault. Version 3.0 adds world-row cache,
rewind-compression, and mixer-handoff counters. `audio-handoff-waits` is a
healthy native transition, `audio-grace-restarts` is a sub-12 ms idle window
that recovered without discarding PCM, and only `audio-underruns` denotes a
confirmed gap. Persistent queue failures remain in `audio-start-failures`.
`pacing-debt-ms` is retained for log compatibility and remains zero. See
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for the full Windows and
frozen-executable checklist.

## Tests

The test suite uses only Python's standard library and synthetic data. It also
compares batched APU clocks, deferred device clocks, direct CPU handlers, and
idle-loop execution against their explicit reference paths:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

The generated demo cartridge is also original to this repository. Press the A
button to change its background accent color; that exercises the controller
shift register, NMI path, CPU, PPU registers, and rendering pipeline together.

Public blargg-protocol hardware diagnostics can be run recursively from raw
ROMs or ZIP archives. The dot PPU is the default; JSON and JUnit reports make
the same gate usable locally and in CI:

```bash
PYTHONPATH=src python tools/accuracy_runner.py path/to/nes-test-roms \
  --json build/accuracy.json \
  --junit build/accuracy.xml
```

No external test ROMs are bundled. Use only diagnostics whose licenses permit
your copy and distribution.

## Technical references

The implementation was written from behavioral documentation rather than from
another emulator:

- [NESdev CPU addressing modes](https://www.nesdev.org/wiki/CPU_addressing_modes)
- [NESdev CPU memory map](https://www.nesdev.org/wiki/CPU_memory_map)
- [NESdev PPU registers](https://www.nesdev.org/wiki/PPU_registers)
- [NESdev PPU rendering](https://www.nesdev.org/wiki/PPU_rendering)
- [NESdev PPU scrolling](https://www.nesdev.org/wiki/PPU_scrolling)
- [NESdev APU](https://www.nesdev.org/wiki/APU)
- [NESdev APU mixer](https://www.nesdev.org/wiki/APU_Mixer)
- [NESdev standard controller](https://www.nesdev.org/wiki/Standard_controller)
- [NESdev iNES format](https://www.nesdev.org/wiki/INES)
- [NESdev NES 2.0 format](https://www.nesdev.org/wiki/NES_2.0)
- [NESdev mapper reference](https://www.nesdev.org/wiki/Mapper)
- [NESdev MMC1 reference](https://www.nesdev.org/wiki/MMC1)
- [NESdev MMC2 reference](https://www.nesdev.org/wiki/MMC2)
- [NESdev MMC4 reference](https://www.nesdev.org/wiki/MMC4)

Nintendo and Nintendo Entertainment System are trademarks of Nintendo. This
independent educational project is not affiliated with or endorsed by Nintendo.
