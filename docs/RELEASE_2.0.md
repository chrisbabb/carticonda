# Cartaconda 2.0 validation record

This record freezes the evidence used for the 2.0.0 source release candidate.
It does not convert host-local measurements into universal performance claims,
and no external diagnostic or commercial ROM is distributed with Cartaconda.

## Validation environment

- Python 3.12.13, 64-bit CPython
- Linux 6.12.13, x86-64, KVM
- Intel Xeon Platinum 8573C validation host
- Pygame intentionally absent for core/package tests; SDL/WASAPI qualification
  belongs to the target Windows build gate

## Automated results

| Gate | Result |
| --- | --- |
| Standard-library suite | 182 passed |
| Source-distribution suite | 182 passed after clean extraction |
| Compile pass | `src`, `tools`, and `tests` compile successfully |
| Wheel metadata | `cartaconda 2.0.0`, Python ≥3.11, pygame-ce ≥2.5.7,<3 |
| Wheel smoke | Version, cartridge info, raw-ROM headless, and ZIP-member headless pass |
| Packaged branding | PNG present with source SHA-256 `63ee27c1ad98df53a7fb0d8b9052a28f3da1598ac0ccfd0a2ac43a442974870c` |

The raw and ZIP wheel smokes each execute 60 dot-reference frames and finish
with framebuffer SHA-256
`689fa1cb64dcb722e6c16f1e92a0d331a0fd6541b550e216e75292ff701711c7`.

## Public hardware diagnostics

| Family | Result |
| --- | --- |
| CPU | 35/35 pass |
| PPU fast path | 24/24 pass |
| PPU dot path | Same open-bus, sprite-hit, sprite-overflow, and VBlank/NMI families pass |
| APU/DMC terminal tests | 17/17 pass |
| DMC CRC-only reads | Output/CRCs match `159A7A8F` and `D84F6815` |
| MMC3 common Sharp/new behavior | 10 applicable tests pass |

The old NEC/revision-A and common Sharp/new MMC3 IRQ behaviors are mutually
exclusive around a reload-to-zero edge. Generic legacy iNES Mapper 4 does not
identify the physical board revision, so 2.0 selects the common Sharp/new
behavior and records the old variant as unsupported rather than falsifying one
test to satisfy the other.

## Deterministic performance record

| Workload | Frames | FPS | NTSC factor | Final frame SHA-256 |
| --- | ---: | ---: | ---: | --- |
| Fast demo | 900 | 456.71 | 7.60x | `82870f0effe54d69adeb76cf6a9310615b925dd858276c9bf4bacd71d023451b` |
| Fast looping DMC | 600 | 229.11 | 3.81x | `a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c` |
| NROM gameplay | 1,200 | 142.40 | 2.37x | `a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c` |
| NROM gameplay + rewind | 1,200 | 131.87 | 2.19x | `a4260f3d0372c92bb8a8c8866b138d8d3e5bf783987aa842ffb31d2dac7c693c` |
| MMC2 gameplay | 1,200 | 125.37 | 2.09x | `8dd3eb48053a0e82d5ff6d940b14ee3f29f18968f0745a271889f6f43175d22a` |

Reproduce the benchmark shape with:

```bash
PYTHONPATH=src python tools/benchmark.py \
  --frames 900 \
  --control-frames 120 \
  --synchronized-frames 30 \
  --accurate-frames 3 \
  --dmc-frames 600 \
  --gameplay-frames 1200 \
  --rewind-frames 1200 \
  --mmc2-frames 1200 \
  --json
```

## Bounded-worker soaks

Each workload was run in a fresh process for 900 frames at exact NTSC
consumption rate with the real worker, two-frame queue, 4,096-sample virtual
PCM prebuffer, two-millisecond host service cadence, and rewind capture.

| Workload | Wall average | Wall p95 | Thread-CPU average | Starves | Underruns | Shutdown |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| NROM | 7.67 ms | 8.94 ms | 7.61 ms | 0 | 0 | Clean |
| MMC2 | 9.86 ms | 16.98 ms | 9.59 ms | 0 | 0 | Clean |

The two-frame queue absorbed the MMC2 tail without repeating a presentation or
draining the virtual audio reservoir. These are core/worker tests, not proof
about a particular SDL audio, video, or controller driver.

## Required native release gate

Before distributing a frozen Windows executable as the public production
build, run the two-hour WASAPI/controller/fullscreen soak and the repeated
load/reload/unload, save/restore/rewind, display-mode, hot-plug, audio-device
failure, and clean-shutdown cycles in
[ROADMAP_2.0.md](ROADMAP_2.0.md). Preserve both `--diagnostics` lines for each
run. Source and frozen artifacts must be qualified separately.
