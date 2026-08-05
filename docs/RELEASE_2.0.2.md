# Cartaconda 2.0.2 validation record

This patch release removed two Mapper 4 transition regressions that remained
in 2.0.1. The original report attributed the Mike Tyson's Punch-Out!! fight
transition to this path; 2.0.3 corrected that diagnosis because Punch-Out!!
uses Mapper 9/MMC2.

## Automated results

| Gate | Result |
| --- | --- |
| Standard-library suite | 189 passed |
| Compile pass | `src`, `tools`, and `tests` compile successfully |
| MMC3 PRG churn | One executed 8 KiB bank compiled; complete state matches literal execution |
| MMC3 CHR churn | 240 visible scanline renders despite more than 500 mapper invalidations |
| Sprite-zero probe | Exact hit cycle matches full scanline composition |
| Shutdown | Bounded worker stops cleanly |

## Same-host 2.0.1 comparison

The packaged 2.0.1 wheel and the 2.0.2 source were run sequentially against
the same original synthetic cartridges on the same loaded host.

| Workload | 2.0.1 first frame | 2.0.2 first frame | Change |
| --- | ---: | ---: | ---: |
| Continuous MMC3 PRG-bank switching | 160.8 ms | 92.8 ms | -42.3% |
| Continuous MMC3 CHR-register writes | 162.8 ms | 95.1 ms | -41.6% |

These are deliberately pathological regression cartridges, not expected game
frame times. Their purpose is to force the two transition failure modes
without distributing commercial data.

## Correctness boundaries

- Sprite-zero status is still evaluated at the exact candidate dot. The
  optimized probe fetches the same background and sprite-zero pixels but does
  not compose unrelated sprites or presentation pixels.
- A mapper change after all eight sprite-zero pixels have passed cannot alter
  that scanline's hit decision.
- MMC3 CPU optimization is admitted per immutable 8 KiB bank and CPU slot only
  after the PC remains there long enough to repay classification.
- A batch cannot cross its admitted 8 KiB slot; mapper writes and all timed
  device accesses return to the literal scheduler.
- Rewind capture is optional work and is deferred whenever the completed-frame
  reservoir needs another frame.

A frozen Windows build should still be tested with the affected ROM through
the first-fight transition, controller hot-plug, save/restore/rewind,
fullscreen changes, and a long `--diagnostics` run.
