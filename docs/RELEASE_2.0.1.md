# Cartaconda 2.0.1 validation record

This patch release targets the Mapper 4 transition slowdown reported in
Mike Tyson's Punch-Out!! while preserving the 2.0 public accuracy gates.

## Automated results

| Gate | Result |
| --- | --- |
| Standard-library suite | 186 passed |
| Compile pass | `src`, `tools`, and `tests` compile successfully |
| MMC3 synchronized control | Frames and complete serialized state match |
| MMC3 transition benchmark | 82.71 FPS, 1.38x NTSC, 240 frames |
| MMC3 bounded-worker soak | 300 frames with rewind, 0 queue starves, 0 audio underruns |
| Shutdown | Clean |

The soak used the real bounded emulation worker, queue depth 2, exact NTSC
consumer, 4,096-sample virtual PCM prebuffer, and active rewind capture. It
measured 12.83 ms average emulation time, 17.37 ms p95, 25.24 ms p99, and
26.97 ms maximum. The queue absorbed the isolated tail without repeating a
presentation or draining the audio reservoir; 51 rewind snapshots were
captured successfully.

## Correctness boundaries

- MMC3 PRG and CHR maps are cached only after mapper register writes.
- CPU spans remain bounded by the same PPU/APU/device deadlines.
- A12 edges are coalesced only while IRQ output is disabled; `step_fast`
  continues to clock every edge, and a mapper write synchronizes devices
  before enabling IRQ.
- Transition rows are keyed by CHR mapping, mirroring, palette, mask, scroll
  plan, and nametable content lifetime. CPU/PPU memory writes still invalidate
  incompatible cached data.
- The optimized MMC3 workload matches the instruction-synchronized frame and
  complete console state.

These host-local core results do not replace the Windows SDL/WASAPI release
gate. A frozen Windows build should still be tested with the affected ROM,
controller hot-plug, save/restore/rewind, fullscreen changes, and a long
diagnostics run.
