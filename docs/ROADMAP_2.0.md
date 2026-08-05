# Cartaconda 2.0 release status

Cartaconda 2.0 is a compatibility, correctness, and latency release—not a
version-number exercise. “Best” cannot be established honestly from a few
commercial games. The evidence here is therefore reproducible: deterministic
differential tests, independently authored hardware diagnostics, measured
performance workloads, bounded-worker soaks, and explicit platform limits.

## Validated 2.0 snapshot

| Area | 2.0 result | Interpretation |
| --- | --- | --- |
| Deterministic suite | 182 passing tests | Core, mapper, UI, audio, input, state, rewind, and ZIP regressions are green |
| Mapper coverage | 17 mapper numbers | Broad first-party/discrete coverage; major expansion ASICs remain future work |
| NROM gameplay stress | 142.4 FPS / 2.37x NTSC | 1,200 frames; deterministic hash unchanged |
| NROM plus rewind | 131.9 FPS / 2.19x NTSC | 1,200 frames with periodic compressed captures |
| MMC2 gameplay stress | 125.4 FPS / 2.09x NTSC | 1,200 latch-heavy scrolling frames; deterministic hash unchanged |
| CPU public gate | 35/35 pass | Semantics, timing, interrupts, reset, dummy reads, and execution-space families |
| PPU public gate | 24/24 pass | Fast and dot paths pass open bus, sprite hit/overflow, and VBlank/NMI families |
| APU/DMC public gate | 17/17 terminal pass; 2 CRC-only matches | CRC-only read tests produce `159A7A8F` and `D84F6815` |
| Bounded-worker soak | 900 NROM + 900 MMC2 frames | Each isolated run has zero queue starvation, zero virtual audio underrun, and clean shutdown |
| Cartridge format | iNES, legacy recovery, NES 2.0 sizing/RAM/NVRAM | Parser and persistence tranche complete |
| Region support | NTSC | PAL and Dendy remain explicit compatibility limits |
| Release status | Source release candidate | Target Windows SDL/WASAPI/controller/fullscreen soak still required for a frozen public binary |

Performance values are local regression signals, not promises for every host.
The final framebuffer hashes are the correctness guards when timing changes:

- NROM: `a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c`
- MMC2: `8dd3eb48053a0e82d5ff6d940b14ee3f29f18968f0745a271889f6f43175d22a`

The complete environment, command, package-smoke, and timing record is in
[RELEASE_2.0.md](RELEASE_2.0.md).

## Public accuracy matrix

External diagnostics are not distributed with Cartaconda. A reproducible
release record should retain each upstream revision and ROM SHA-256.

| Subsystem | Recorded result | Remaining boundary |
| --- | --- | --- |
| CPU semantics/timing | 35/35 pass | A future bus-cycle trace would extend coverage of rarer dummy-access races |
| PPU VBlank/NMI | 7/7 pass on fast and dot paths | Analog revision differences and OAM corruption are not modeled |
| PPU sprites | 11/11 hit and 5/5 overflow pass on fast and dot paths | Revision-specific OAM corruption remains |
| PPU bus | `ppu_open_bus` passes | Long-timescale open-bus decay is not modeled |
| APU/reset/channels | 14/14 pass in the recorded tranche | More analog waveform comparison remains useful |
| DMC DMA/input | 3 terminal tests pass; 2 CRC-only outputs match | Additional OAM/DMC collision phase cases remain useful |
| MMC3 IRQ | 10 applicable tests pass | Generic Mapper 4 selects common Sharp/new behavior; mutually exclusive NEC/revision-A behavior and MMC6 need board metadata |
| Supported discrete mappers | Deterministic bank, mirror, conflict, and state tests pass | More public board-specific cartridges should be added as licensed diagnostics become available |

Run compatible suites with:

```bash
PYTHONPATH=src python tools/accuracy_runner.py path/to/diagnostics \
  --json build/accuracy.json \
  --junit build/accuracy.xml
```

## Completed in 2.0

- Exact sprite-zero timing, the secondary-OAM overflow scan and diagonal bug,
  VBlank/NMI suppression, odd-frame timing, and the recorded DMC read-conflict
  cases now pass on the optimized gameplay path.
- Rewind uses a 30-second bounded compressed ring, restores only while the
  worker is stopped, checks ROM identity, flushes host audio, and never writes
  battery storage.
- Continuously scrolling NROM and MMC2 scenes no longer churn full-line cache
  objects. MMC2 fetch plans are packed, latch side effects remain ordered, and
  one complete horizontal scroll cycle remains resident.
- Diagnostics now separate wall duration from worker thread-CPU duration.
  `tools/soak.py` exercises the real two-frame worker queue, PCM reservoir,
  rewind captures, failure propagation, and shutdown.
- Save states remain atomic, portable, non-pickle files. Settings, controls,
  battery data, ZIP identity, and incompatible-ROM failures have deterministic
  coverage.

## Known limits and next-stage work

### Hardware coverage

- Add PAL and Dendy clock domains, palettes, APU tables, frame sequencers, and
  region-aware state identity.
- Separate MMC3 revisions and MMC6 RAM behavior using trustworthy NES 2.0
  board metadata. Add MMC5, VRC, FME-7, and Namco families, including expansion
  audio where present.
- Extend the reference scheduler toward bus-cycle traces for uncommon dummy
  access, DMA collision, and interrupt-race cases.
- Model revision-specific PPU OAM corruption and long-timescale open-bus decay.
- Add common peripherals after standard controllers: Zapper/light gun,
  Four Score, paddle, and region-appropriate devices.

### Player and developer tools

- Add deterministic movie input recording/playback for reproducible reports
  and tool-assisted regression cases.
- Add debugger essentials: breakpoints, disassembly, CPU/PPU memory, registers,
  mapper state, and frame/scanline/dot stepping.
- Add pixel-preserving video options such as overscan crop, aspect correction,
  selectable palettes, and optional filters.
- Add named controller profiles, explicit hot-plug reassignment, and a visible
  turbo policy.

### Platform qualification

- Complete a two-hour Windows WASAPI/controller/fullscreen soak with zero audio
  underruns, zero worker starvation, zero native crashes, and clean shutdown.
- Repeat load/reload/unload, save/restore, rewind, fullscreen, controller
  hot-plug, and audio-device failure cycles under diagnostics and memory
  monitoring.
- Smoke-test source packages on current Windows, macOS, and Linux. Qualify a
  frozen Windows executable separately because its bundled Python/SDL boundary
  differs from a source installation.

## Release rule

The source package is a 2.0 candidate only while its 182-test suite, public
acceptance record, deterministic frame/state comparisons, mapper-specific
tests, and clean per-workload soaks remain green. A frozen public binary also
requires the native target-host qualification above. Limitations must stay
visible in release notes; a smooth run of one title never replaces these
gates.
