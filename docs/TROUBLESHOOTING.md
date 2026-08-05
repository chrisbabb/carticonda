# Native crash and audio troubleshooting

## What `pygame_parachute` means

`pygame_parachute: Segmentation Fault` is an SDL/Pygame native-process fault,
not a Python exception. The Python line shown under “Current thread” is where
one interpreter thread happened to be when SDL reported the signal. In 1.0 the
reported thread may be the pure-Python emulation worker even though every SDL
call remains on the main thread. A line such as
`opcode = self._code_read(self.pc)` contains ordinary Python operations and is
not, by itself, evidence that the CPU decoder caused the native memory fault.

Cartaconda 0.8.1 and later remove native-risk paths that can produce this class
of failure:

- one long-lived Pygame `Surface` now shares a main-thread framebuffer;
  gameplay no longer creates a temporary buffer-backed Surface or exposes the
  worker's concurrently mutable PPU buffer;
- version 3.0 submits both active and idle PCM through `Channel.queue()` and
  never enters either play path during a normal stream. pygame-ce 2.5.7 has an
  [open memory-corruption bug](https://github.com/pygame-community/pygame-ce/issues/3798)
  in explicit `Channel.play()` when SDL rejects a playback start;
- immutable PCM snapshots and `Sound` objects are retained explicitly across
  every current/queued native lifetime;
- ROM replacement, mute, pause, and state transitions stop audio before
  releasing retained sounds;
- game-controller state comes from SDL events instead of dozens of native
  handle queries each frame, while add/remove events open or close one exact
  instance on the main thread;
- the bounded emulation worker never imports or calls Pygame; video, audio,
  events, controllers, and ordered subsystem teardown all remain on the main
  thread; and
- mixer, controller, texture, renderer, window, font, Surface, and display
  resources are shut down in dependency order instead of relying on one broad
  `pygame.quit()` call.

## Capture useful diagnostics

Run the same game with:

```powershell
cartaconda game.nes --diagnostics
```

The first line records the Cartaconda version, Python, pygame-ce, SDL, video,
buffered exact-NTSC display mode, selected display backend/renderer, bounded
worker/queue settings, mixer driver, PCM buffer and prebuffer, safe audio-start
path, event-input path, controller count, and frozen-runtime status immediately
after initialization. A normal exit adds core wall average/p95/p99/max timing,
worker thread-CPU average/max, draw and total active-loop timing, frame-budget
and pacing misses, worker queue high-water/starvation/start counters, audio
underrun/resynchronization/start-failure counters, presented frames,
presentation jitter, compatibility debt/skip fields, gamepad event count, and
a clean-exit marker. Version 2.0.2 adds `rewind-deferrals`,
`ppu-scanline-renders`, `ppu-status-probes`, `ppu-mapper-invalidations`,
`cpu-bank-compiles`, and `cpu-bank-deferrals`. Version 2.0.3 adds
`ppu-cache-invalidations`, `ppu-cache-coalesces`,
`ppu-deferred-data-writes`, `cpu-ppu-stream-batches`, and
`unchanged-video-frames`. Version 2.0.4 adds `unchanged-display-skips`,
`cpu-literal-instructions`, CPU safe/poll/counter batch counts,
`cpu-device-flushes`, `cpu-device-flush-cycles`, `cpu-stall-spans`,
`dmc-fetches`, and `cpu-hot-spans`.
Version 2.1.2 adds `ppu-scanline-replays`, `cpu-dmc-poll-batches`, and
`cpu-dmc-poll-fetches`. Version 2.1.3 reports `PCM-chunk=2048` while retaining
`PCM-prebuffer=4096`; together those values identify the wider native audio
guard without extra startup buffering. In 2.2.0, existing
`cpu-device-flushes`, `cpu-poll-batches`, `cpu-bank-compiles`, and
`cpu-bank-deferrals` expose the active-IRQ Mapper 4 repair without adding a
new diagnostic format.
Version 3.0 reports `audio-start=channel-queue`,
`audio-handoff-waits`, `audio-grace-restarts`,
`rewind-compression-deferrals`, and bounded PPU world-cache activity. Handoff
waits and grace restarts preserve PCM and are not underruns.
`late-frames` excludes timer
noise below 1 ms. If a native
crash occurs, preserve the startup line and the fatal traceback; the missing
clean-exit line is expected.

Use the phase fields to classify slowdown:

- high `emulation-p95-ms` means the Python emulation core is missing deadlines;
- if `emulation-p95-ms` is high but `emulation-cpu-average-ms` remains low,
  the worker is being descheduled by the OS/GIL rather than spending all that
  wall time inside the core; compare the average and maximum CPU fields with
  the corresponding wall fields;
- low emulation time but high `draw-average-ms` points to host scaling/display;
- low emulation and draw time but high `active-average-ms` points to input,
  mixer submission, or event handling;
- high `late-frames` with low active time points to host scheduling or another
  process preempting Cartaconda; and
- high `presentation-jitter-p95-ms` means visible flip intervals are still
  varying; compare it with emulation and draw p95/max to identify the source.
- `frame-queue-high-water=2` shows that the variance reservoir filled;
  `frame-queue-starves` should normally remain zero. A nonzero value means the
  last complete frame had to be repeated because the core did not finish by
  its draw boundary.
- `pacing-debt-ms` is a compatibility field in 1.0 and must remain zero. A
  nonzero value means an older executable is running.

Cartaconda 1.0 presents every completed emulated frame, so
`presentation-skips` and `max-skip-streak` are retained only for diagnostic
compatibility and should remain zero. If they are nonzero, an older executable
is running.

## Mapper 9 performance

Mapper 9 games should use Cartaconda 2.0 or later. Version 1.1.0 applied its
bounded CPU instruction-span optimizer only to NROM; version 1.1.1 added mapped
PRG spans but still churned nested MMC2 fetch plans and full scrolling-line
caches. Game-logic-heavy MMC2 software could consequently exhaust the
two-frame queue even when the display and mixer were healthy.

After upgrading, confirm that the startup line says `Cartaconda=2.2.0` and
rerun the same game with `--diagnostics`. On a host capable of real-time
emulation, `emulation-average-ms` and `emulation-p95-ms` should remain below
the approximately 16.64 ms NTSC frame interval,
`frame-queue-starves` should remain zero after startup, and
`audio-underruns` should remain zero. An occasional maximum above one frame is
acceptable when the two-frame queue absorbs it without starvation. If the
average or p95 remains above budget, include the mapper number, ROM SHA-256,
and both diagnostic lines in the report.

### Punch-Out!! forced-blank transition

Mike Tyson's Punch-Out!! is a Mapper 9/MMC2 title. Version 2.1.0 directly
optimizes its CPU-driven black-screen bell routine and merges the preserved
DAC writes with host-sample boundaries in one APU event loop. The game writes
sampled levels to `$4011` from a cycle-counted 6502 loop, so earlier nametable
and indirect-stream changes did not address the dominant cost. In a diagnostic
run that covers this sequence, `cpu-pcm-batches`, `cpu-pcm-writes`, and
`cpu-ppu-wait-batches` should become nonzero; `unchanged-video-frames` and
`unchanged-display-skips` should also rise while the display remains black.
`cpu-pcm-writes` counts preserved hardware DAC writes and will be much larger
than `cpu-pcm-batches`; if the two counts are nearly equal, an older 2.0.5
executable is still running.
`ppu-deferred-data-writes` and `cpu-ppu-stream-batches` may rise later during
forced-blank scene setup. Version 2.1.0 commits adjacent safe PPUDATA values as
one ordered PPU transaction, so a high write count no longer implies the same
number of presentation-cache invalidations.
These counters must stay zero when DMC playback or visible rendering makes the
deferred path unsafe.

### Punch-Out!! visible first-fight slowdown

Version 2.1.1 also recognizes the game's visible
`LDA $2002 / AND #$40 / BNE|BEQ` sprite-zero synchronization loop. It batches
only still-taken iterations before the next PPU/APU/DMC boundary; the
transition read and scroll writes remain literal. During a fight,
`cpu-ppu-wait-batches` should now rise while the old `A0BC`/`A0BF` hot spans
shrink sharply.

The draw diagnostics now include `draw-prepare-average-ms` and
`flip-average-ms`. A high prepare value points to scaling/blitting; a high
flip value points to the SDL video driver or Windows compositor. Include both
fields with any report where emulation stays under budget but presentation
still feels uneven.

### Punch-Out!! active-DMC RAM wait

Version 2.1.2 fixes the second Mapper 9 regression reported after 2.1.1. During
the DMC-heavy interval, `$AF06` is a read-only RAM wait. Earlier builds stopped
at each sample fetch, producing tens of thousands of short device flushes and
stall spans. A current run should report nonzero `cpu-dmc-poll-batches` and
`cpu-dmc-poll-fetches`; the latter counts sample fetches handled inside the
cycle-accounted wait batch. Those values are expected only while DMC playback
and an admitted RAM poll overlap.

`ppu-scanline-replays` counts lines that were already correct in the retained
framebuffer. It can be high in static or alternating scenes and low during a
full-screen animation. It is an optimization counter, not a dropped-frame
counter.

Version 2.1.3 makes those retained-line dependencies row-specific for MMC2.
Nametable writes elsewhere on the screen no longer force a correct line to be
recomposed, while writes to a fetched tile/attribute row, palette, CHR,
mirroring, mapper registers, sprites, scrolling, or latch state still force an
exact miss.

## Audio crackle or a pop after a sound

Version 2.1.3 addresses two independent causes:

- SDL_mixer can hold one playing and one queued `Sound`. With 2,048 samples in
  each, the native side covers about 93 ms at 44.1 kHz instead of about 46 ms.
  The prebuffer remains 4,096 samples, so this does not add startup latency.
- The nonlinear APU mixer now feeds the documented 90 Hz and 440 Hz high-pass
  stages and 14 kHz low-pass stage. The high-pass chain removes the DC
  transition that can otherwise sound like a pop when a `$4011` sampled sound
  ends.

After upgrading, the startup line should contain `PCM-chunk=2048`,
`mixer-buffer=2048`, and `PCM-prebuffer=4096`. A rising `audio-underruns`
counter still means the Windows audio device ran completely dry. Compare
`emulation-max-ms` and `frame-queue-starves`: a rare maximum below roughly
93 ms should now be absorbed by the native queue, while sustained averages or
p95 values above the 16.64 ms frame budget still require reducing competing
host load or further core profiling.

## Display backend

On Windows, `display-backend=sdl-texture` means Cartaconda uploaded the native
256x240 frame and selected the accelerated renderer named by `display-driver`.
`display-backend=surface` means no compatible accelerated renderer was
available or the portable path was forced. Both routes preserve the same
pixels and exact-NTSC presentation schedule.

If a graphics driver produces a blank window, corruption, or a native crash,
force the portable path before launching:

```powershell
$env:CARTACONDA_SOFTWARE_DISPLAY = "1"
cartaconda game.nes --diagnostics
```

The startup line should then say `display-backend=surface`. Remove the
environment variable to try hardware presentation again:

```powershell
Remove-Item Env:CARTACONDA_SOFTWARE_DISPLAY
```

## Mapper 4 active-IRQ or transition slowdown

Mapper 4 games should use Cartaconda 2.0.2 or later. Version 2.0.0 recalculated
MMC3 PRG/CHR maps during hot reads,
rebuilt both offscreen nametables after transition writes, and stopped the CPU
worker at A12 phases even while mapper IRQ output was disabled.

Version 2.0.1 caches register-derived slots, batches code from independently
switched 8 KiB banks, renders only visible tiles for cold states, and retains
content-keyed rows when a transition state recurs. Version 2.0.2 prevents
sprite-zero status timing from recomposing a scanline after every CHR write
and compiles only stable 8 KiB banks that the CPU actually executes.

Version 2.2.0 fixes the separate active-IRQ regression exposed by Mega Man 5.
Version 2.1.3 stopped the worker at every possible MMC3 A12 edge even when the
counter could not assert, producing roughly 589 tiny device flushes per frame
in the reported run. The current scheduler projects the counter to its exact
asserting edge and can cross the earlier edges/scanlines safely. It also
compiles an executed bank on first use and collapses signature-proven idle
passes through a four-record cooperative scheduler. The original release
workload averages about 33 flushes per frame; the exact count varies with game
logic and IRQ latch values.

For Mega Man 5 or another active-IRQ title, verify `Cartaconda=3.0.0`, then
compare `cpu-device-flushes / frames` with the older run. `cpu-bank-compiles`
should be small and nonzero, `cpu-bank-deferrals` should stay close to it, and
`cpu-poll-batches` may be nonzero during an idle scheduler scan. If the p95
still exceeds 16.64 ms, compare the scrolling section's
`cpu-block-deferrals`, `cpu-block-compile-peak`, `ppu-scanline-replays`,
`frame-queue-starves`, and `audio-underruns`. Block deferrals are protective:
they mean optional host translation used the safe generic path for that frame.
A complete run may report a compile peak of four from the first two hidden
reservoir-fill frames, but gameplay itself is capped at one. Include the ROM
hash and diagnostics covering at least 30 seconds before and after the slow
section.

## Isolate a native subsystem

Run each comparison long enough to cover the original failure:

```powershell
cartaconda game.nes --diagnostics --mute
cartaconda game.nes --diagnostics --no-gamepad
cartaconda game.nes --diagnostics --mute --no-gamepad
```

- Stable only with `--mute` points to the host audio device, driver, or mixer
  boundary.
- Stable only with `--no-gamepad` points to a controller/HID driver or hot-plug
  path.
- A crash in every configuration points more strongly to display/Surface,
  packaging, or another native dependency.

These switches are diagnostic fallbacks; keyboard input remains available.
Cartaconda initializes SDL subsystems individually, so `--mute` does not open
the mixer and `--no-gamepad` does not initialize the joystick/HID subsystem.

## Keep the host environment unambiguous

Use a clean virtual environment and install the project normally:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip uninstall -y pygame
.\.venv\Scripts\python -m pip install -e .
```

Cartaconda 1.0 and later require pygame-ce 2.5.7 or newer. Do not install the separate
`pygame` distribution into the same environment because both distributions
provide a top-level module named `pygame`.

If a frozen executable crashes but the clean source environment does not, the
emulation state is not the differentiator. Rebuild the executable from that
known-good environment and ensure the packager collects pygame-ce's SDL
libraries from the same installation rather than mixing DLLs from another
Python or Pygame installation.

When reporting a reproducible failure, include the diagnostic lines, whether
mute/no-gamepad changed it, the ROM's mapper number and SHA-256 from `--info`,
and the exact action immediately before the crash. Do not send a copyrighted
ROM.
