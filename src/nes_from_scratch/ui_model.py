"""Persistent frontend preferences and filesystem-backed UI models."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_KEY_BINDINGS = {
    "p1_a": "z",
    "p1_b": "x",
    "p1_select": "right shift",
    "p1_start": "return",
    "p1_up": "up",
    "p1_down": "down",
    "p1_left": "left",
    "p1_right": "right",
    "p2_a": "c",
    "p2_b": "v",
    "p2_select": "q",
    "p2_start": "e",
    "p2_up": "w",
    "p2_down": "s",
    "p2_left": "a",
    "p2_right": "d",
}

_DEFAULT_GAMEPAD_PLAYER_BINDINGS = {
    "a": ("button:0",),
    "b": ("button:1",),
    "select": ("button:6", "button:8"),
    "start": ("button:7", "button:9"),
    "up": ("hat:0:up", "axis:1:-", "button:11"),
    "down": ("hat:0:down", "axis:1:+", "button:12"),
    "left": ("hat:0:left", "axis:0:-", "button:13"),
    "right": ("hat:0:right", "axis:0:+", "button:14"),
}
DEFAULT_GAMEPAD_BINDINGS = {
    f"p{player}_{action}": list(sources)
    for player in (1, 2)
    for action, sources in _DEFAULT_GAMEPAD_PLAYER_BINDINGS.items()
}

WINDOW_SCALES = (2, 3, 4)
SETTINGS_VERSION = 2


def valid_gamepad_source(source: object) -> bool:
    """Return whether *source* is a bounded, portable SDL input descriptor."""
    if not isinstance(source, str):
        return False
    parts = source.split(":")
    try:
        if len(parts) == 2 and parts[0] == "button":
            return 0 <= int(parts[1]) < 64 and str(int(parts[1])) == parts[1]
        if len(parts) == 3 and parts[0] == "axis":
            return (
                0 <= int(parts[1]) < 16
                and str(int(parts[1])) == parts[1]
                and parts[2] in ("-", "+")
            )
        if len(parts) == 3 and parts[0] == "hat":
            return (
                0 <= int(parts[1]) < 16
                and str(int(parts[1])) == parts[1]
                and parts[2] in ("up", "down", "left", "right")
            )
    except ValueError:
        return False
    return False


def default_gamepad_bindings() -> dict[str, list[str]]:
    """Return an independent copy suitable for mutable settings."""
    return {
        action: sources.copy()
        for action, sources in DEFAULT_GAMEPAD_BINDINGS.items()
    }


@dataclass(slots=True)
class FrontendSettings:
    """User-facing settings that are independent of emulated machine state."""

    version: int = SETTINGS_VERSION
    volume: float = 0.75
    muted: bool = False
    scale: int = 3
    fullscreen: bool = False
    rom_directory: str = ""
    recent_roms: list[str] = field(default_factory=list)
    key_bindings: dict[str, str] = field(
        default_factory=lambda: DEFAULT_KEY_BINDINGS.copy()
    )
    gamepad_bindings: dict[str, list[str]] = field(
        default_factory=default_gamepad_bindings
    )

    @classmethod
    def load(cls, path: str | Path) -> "FrontendSettings":
        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return cls()
        if not isinstance(payload, dict):
            return cls()
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FrontendSettings":
        settings = cls()
        try:
            settings.volume = min(1.0, max(0.0, float(payload.get("volume", 0.75))))
        except (TypeError, ValueError):
            pass
        settings.muted = bool(payload.get("muted", False))
        try:
            requested_scale = int(payload.get("scale", 3))
        except (TypeError, ValueError):
            requested_scale = 3
        settings.scale = requested_scale if requested_scale in WINDOW_SCALES else 3
        settings.fullscreen = bool(payload.get("fullscreen", False))

        directory = payload.get("rom_directory", "")
        if isinstance(directory, str):
            settings.rom_directory = directory

        recent = payload.get("recent_roms", [])
        if isinstance(recent, list):
            settings.recent_roms = [
                item for item in recent if isinstance(item, str) and item
            ][:8]

        bindings = payload.get("key_bindings", {})
        if isinstance(bindings, dict):
            for action in DEFAULT_KEY_BINDINGS:
                value = bindings.get(action)
                if isinstance(value, str) and value.strip():
                    settings.key_bindings[action] = value.strip().lower()

        gamepad_bindings = payload.get("gamepad_bindings", {})
        if isinstance(gamepad_bindings, dict):
            for action in DEFAULT_GAMEPAD_BINDINGS:
                values = gamepad_bindings.get(action)
                if not isinstance(values, list):
                    continue
                cleaned: list[str] = []
                for value in values:
                    if (
                        valid_gamepad_source(value)
                        and value not in cleaned
                    ):
                        cleaned.append(value)
                    if len(cleaned) >= 8:
                        break
                if cleaned:
                    settings.gamepad_bindings[action] = cleaned
        return settings

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            asdict(self),
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        file_descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=destination.parent,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def remember_rom(self, path: str | Path) -> None:
        resolved = str(Path(path).expanduser().resolve())
        self.recent_roms = [
            item for item in self.recent_roms if item != resolved
        ]
        self.recent_roms.insert(0, resolved)
        del self.recent_roms[8:]
        self.rom_directory = str(Path(resolved).parent)

    def reset_controls(self) -> None:
        self.key_bindings = DEFAULT_KEY_BINDINGS.copy()

    def reset_gamepad_controls(self) -> None:
        self.gamepad_bindings = default_gamepad_bindings()


@dataclass(frozen=True, slots=True)
class StateSlotInfo:
    slot: int
    state_path: Path
    thumbnail_path: Path
    exists: bool
    modified_at: float | None
    size_bytes: int

    @property
    def timestamp_label(self) -> str:
        if self.modified_at is None:
            return "Empty slot"
        return datetime.fromtimestamp(self.modified_at).strftime(
            "%b %d, %Y · %I:%M %p"
        )


def state_slots(
    state_directory: str | Path,
    rom_sha256: str,
    count: int = 10,
) -> list[StateSlotInfo]:
    directory = Path(state_directory) / rom_sha256[:16]
    result: list[StateSlotInfo] = []
    for slot in range(max(0, count)):
        state_path = directory / f"slot-{slot}.fnes"
        thumbnail_path = directory / f"slot-{slot}.png"
        try:
            stat = state_path.stat()
        except OSError:
            result.append(
                StateSlotInfo(
                    slot=slot,
                    state_path=state_path,
                    thumbnail_path=thumbnail_path,
                    exists=False,
                    modified_at=None,
                    size_bytes=0,
                )
            )
        else:
            result.append(
                StateSlotInfo(
                    slot=slot,
                    state_path=state_path,
                    thumbnail_path=thumbnail_path,
                    exists=True,
                    modified_at=stat.st_mtime,
                    size_bytes=stat.st_size,
                )
            )
    return result


@dataclass(frozen=True, slots=True)
class BrowserEntry:
    path: Path
    is_directory: bool
    display_label: str | None = None

    @property
    def label(self) -> str:
        return self.display_label or self.path.name or str(self.path)

    @property
    def is_archive(self) -> bool:
        return not self.is_directory and self.path.suffix.casefold() == ".zip"


def browser_entries(directory: str | Path) -> list[BrowserEntry]:
    root = Path(directory).expanduser()
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    directories: list[BrowserEntry] = []
    roms: list[BrowserEntry] = []
    for item in children:
        if item.name.startswith("."):
            continue
        try:
            if item.is_dir():
                directories.append(BrowserEntry(path=item, is_directory=True))
            elif item.is_file() and item.suffix.casefold() in (".nes", ".zip"):
                roms.append(BrowserEntry(path=item, is_directory=False))
        except OSError:
            continue
    directories.sort(key=lambda entry: entry.label.casefold())
    roms.sort(key=lambda entry: entry.label.casefold())
    return directories + roms


def _windows_drive_names() -> list[str]:
    """Return mounted Windows drive roots without probing every letter."""
    list_drives = getattr(os, "listdrives", None)
    if callable(list_drives):
        try:
            return [str(drive) for drive in list_drives()]
        except OSError:
            return []

    # os.listdrives() was added in Python 3.12. Keep the Python 3.11 support
    # promised by the package by using the same Win32 bitmask on older hosts.
    try:
        import ctypes

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except (AttributeError, OSError, TypeError, ValueError):
        return []
    return [
        f"{chr(ord('A') + index)}:\\"
        for index in range(26)
        if mask & (1 << index)
    ]


def browser_roots(
    *,
    platform_name: str | None = None,
    drive_names: Iterable[str] | None = None,
) -> list[BrowserEntry]:
    """List selectable filesystem roots for the in-app ROM browser.

    Windows has no single parent above a drive root, so the browser exposes a
    small virtual "This PC" level. ``drive_names`` is injectable to keep the
    behavior deterministic on non-Windows test hosts.
    """
    platform_name = os.name if platform_name is None else platform_name
    if platform_name == "nt":
        candidates = _windows_drive_names() if drive_names is None else drive_names
        normalized: dict[str, str] = {}
        for candidate in candidates:
            label = str(candidate).strip()
            if len(label) >= 2 and label[1] == ":":
                label = f"{label[0].upper()}:\\"
            if label:
                normalized.setdefault(label.casefold(), label)
        return [
            BrowserEntry(
                path=Path(label),
                is_directory=True,
                display_label=label,
            )
            for label in sorted(normalized.values(), key=str.casefold)
        ]

    root = Path(Path.cwd().anchor or os.sep)
    return [
        BrowserEntry(
            path=root,
            is_directory=True,
            display_label=str(root),
        )
    ]
