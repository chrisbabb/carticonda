# Cartaconda 2.2.3 validation record

Date: 2026-08-03

## Scope

This patch addresses the remaining Mega Man 5 slowdown reported from
Cartaconda 2.2.2. In the supplied 4,559-frame Windows run, drawing averaged
only 0.452 ms, but the emulation worker consumed 16.382 ms of CPU time per
frame and had a 36.750 ms wall-time p95. The two-frame queue starved 930 times,
which led to 456 audio underruns. The 1,085,390 rendered scanlines and 596,072
replays were close to one visible-frame pass each; SDL presentation was not the
bottleneck. The remaining target was repeated CPU/APU/PPU interpreter work.

No commercial ROM, title string, ROM hash, game data, or game-specific code is
included in or required by these optimizations.

## Bounded hot-block translation

The existing scheduler already proves when an instruction start can access
only mirrored CPU RAM, stack RAM, or immutable mapped PRG. Version 2.2.3 reuses
that proof after the same block start is observed eight times. It assembles a
fixed Python function from internal opcode templates and lets CPython compile
that function once. A/X/Y/P/SP, PC, open bus, and cycle count then remain in
locals across the block rather than returning through the opcode dispatcher
for every instruction.

The translated family includes the admitted official load/store, ALU,
compare, shift/rotate, increment/decrement, transfer, flag, stack,
branch, JSR/RTS, and absolute-jump operations. Short branch/call/return bridges
allow a hardware deadline landing at the end of one block to reconnect to the
next hot block. A self-backward branch checks the remaining device deadline
after each emulated instruction.

Safety does not depend on a game identity. Device accesses, mapper writes,
interrupt-unmasking instructions, indirect uncertainty, unofficial opcodes,
DMA, DMC boundaries, and mutable code remain on the ordinary executor. MMC3's
last byte in each 8 KiB slot is excluded when its implied opcode would perform
a discarded fetch in the adjacent mapped bank. The host-only cache is bounded
to 2,048 functions and is never serialized. ROM bytes choose only fixed
integer templates; no ROM text is evaluated. This is pure Python, not native
code and not another emulator core.

## APU, PPU, and host audio

- Long no-DMC-fetch APU sample runs retain timer, channel, nonlinear mixer, and
  all three analog-filter stages in locals, then publish once. Exact sample
  arithmetic order is unchanged. Runs shorter than four sample intervals keep
  the lower-overhead prior helper, preventing a regression under dense mapper
  and device traffic.
- Ordinary background and sprite pattern planes use one ordered mapper-pair
  boundary. Sprite rows cache only their compact nontransparent offset/pixel
  pairs; position, priority, palette, clipping, sprite-zero, and overflow stay
  live inputs. MMC2 latch side effects remain ordered.
- A transient Windows/WASAPI refusal to start an apparently free channel is
  retried once through `Sound.play()` after clearing only the stale idle
  channel. PCM remains staged. The frontend never calls the explicit
  `Channel.play()` path implicated in earlier pygame-ce native crashes.

## Differential correctness gates

- Every supported translated official opcode is forced past the hot threshold
  and compared with one literal CPU step from identical registers, flags,
  stack, RAM, open bus, and mapper state.
- RAM and immutable-PRG page crossings, NROM reads, MMC3 segmented reads, and
  the 8 KiB boundary case retain exact cycles and final state.
- Long local APU sampling is compared against advancing every reference CPU
  clock with all channel families and the complete output filter configured.
  PCM samples and serialized APU state match exactly.
- Synchronized NROM, Mapper 9, and active Mapper 4 runs retain complete-state
  and framebuffer parity. A transient audio-start refusal retains its PCM and
  recovers through the safe API.
- All 220 standard-library tests pass in 11.600 seconds on the validation host.

## Direct performance gate

Each result below is an isolated 900-frame process using the same original
synthetic workload and host. The baseline imports the packaged 2.2.2 wheel;
the candidate imports the 2.2.3 source. Final framebuffer SHA-256 values match
between versions for every row.

| Workload | 2.2.2 | 2.2.3 | Speedup |
|---|---:|---:|---:|
| NROM gameplay | 125.49 FPS | 236.36 FPS | 1.88x |
| Mapper 9 gameplay | 110.53 FPS | 192.70 FPS | 1.74x |
| Mapper 4 gameplay | 127.80 FPS | 218.14 FPS | 1.71x |
| Mapper 4 active IRQ | 230.08 FPS | 258.71 FPS | 1.12x |

## Threaded exact-NTSC gate

Each workload ran for 900 frames in a separate process through the real
two-frame worker queue, fractional-NTSC consumer, virtual PCM reservoir, and
default compressed rewind capture path. `--strict` requires zero queue
starvation, zero audio underruns, and a clean worker shutdown.

| Workload | Average | p95 | p99 | Maximum | Queue starves | Audio underruns |
|---|---:|---:|---:|---:|---:|---:|
| NROM | 4.349 ms | 4.933 ms | 7.521 ms | 18.060 ms | 0 | 0 |
| Mapper 9 | 4.999 ms | 9.115 ms | 11.684 ms | 17.676 ms | 0 | 0 |
| Mapper 4 | 4.636 ms | 5.294 ms | 7.404 ms | 23.194 ms | 0 | 0 |
| Mapper 4 active IRQ | 3.987 ms | 4.615 ms | 6.740 ms | 9.526 ms | 0 | 0 |

The isolated maximum outliers caused one, one, three, and zero emulation-budget
misses respectively, but the queue absorbed all of them and audio never
underran. Timing is host-dependent; exact pixels/state and the differential
suite are the correctness gates.

## Field-validation request

The release workloads verify the mechanisms but do not substitute for the
reporter's Windows 11, Python 3.14, pygame-ce, Direct3D, WASAPI, and cartridge
combination. Re-run the affected section with `--diagnostics`. The most useful
comparison fields are emulation average/p95/p99, budget misses,
`cpu-translated-blocks`, frame-queue starvation, audio-start failures, and
audio underruns.

No ROM image, save state, commercial asset, or game-specific data is present
in the source, wheel, sdist, or release archive.
