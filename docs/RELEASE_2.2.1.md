# Cartaconda 2.2.1 validation record

Date: 2026-08-03

## Scope

This patch fixes two Windows launcher problems without changing CPU, PPU, APU,
mapper, input, state, worker, or frame-pacing behavior:

- text became garbled when the launcher was presented in a window; and
- the in-app ROM browser could not move above the drive on which it started.

## Windowed presentation

The menu is designed at 960x720 (4:3), but earlier windowed modes used the
NES image dimensions directly: 512x480, 768x720, and 1024x960. Fitting the
4:3 menu into those 16:15 windows reduced it to 512x384 or 768x576 at the two
smaller settings. Nearest-neighbor reduction then removed entire pixel rows
and columns from aliased font glyphs.

Windowed modes now use 4:3 host containers:

| Setting | Host window | Exact game viewport |
|---|---:|---:|
| 2x | 640x480 | 512x480 |
| 3x | 960x720 | 768x720 |
| 4x | 1280x960 | 1024x960 |

The game remains integer-scaled at every setting, with narrow side borders
that account for the NES image's 16:15 pixel aspect. The default 3x UI is now
one-to-one. Exact and integer UI scales retain nearest-neighbor presentation;
fractional scales use pygame-ce's filtered scaler so every glyph stroke
contributes to the result.

## Cross-drive ROM browsing

The browser now has a virtual `THIS PC` level and a persistent `DRIVES`
button. On Windows it obtains mounted roots from `os.listdrives()`. A Win32
`GetLogicalDrives` bitmask fallback preserves the package's Python 3.11
support. This avoids probing 26 paths and includes mounted drive letters
reported by Windows.

Each volume appears as a `DRV` entry. Selecting it opens that root; choosing
`UP` at the root returns to the drive picker. The picker remains reachable
from every directory, including an empty or inaccessible volume.

## Validation

- All 214 standard-library tests pass.
- Regression coverage verifies the 640x480, 960x720, and 1280x960 window
  geometry and confirms that each contains an exact game integer scale.
- Scaling coverage verifies filtered fractional reduction, a zero-copy exact
  UI presentation, and nearest-neighbor integer enlargement.
- Filesystem coverage verifies normalized, sorted, deduplicated Windows drive
  roots without relying on the test host's operating system.
- A pygame-ce 2.5.7 / SDL 2.32.10 dummy-display pass rendered the home,
  settings, controls, browser, and ZIP-member views at all three sizes.
- The 640x480, 960x720, and 1280x960 drive-picker frames were visually
  inspected for readable text, complete controls, correct focus state, and
  unclipped layout.

No ROM image, commercial asset, or game-specific data is included in the
source or release archives.
