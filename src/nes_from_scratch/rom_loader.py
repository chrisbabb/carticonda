"""Safe loading of raw NES images and ROMs stored inside ZIP archives."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .cartridge import Cartridge


# Safety cap based on the largest linear NES 2.0 image: 3839 PRG banks,
# 3839 CHR banks, a header, and an optional trainer. Exponent/multiplier sizes
# are supported when the resulting image remains below this practical limit.
MAX_ROM_BYTES = (0xEFF * 0x4000) + (0xEFF * 0x2000) + 528
MAX_ARCHIVE_ENTRIES = 4096


class RomLoadError(ValueError):
    """A ROM source could not be read safely."""


@dataclass(frozen=True, slots=True)
class ArchiveRom:
    member_name: str
    size_bytes: int
    compressed_size: int

    @property
    def label(self) -> str:
        normalized = self.member_name.replace("\\", "/")
        return PurePosixPath(normalized).name or self.member_name


class ArchiveSelectionRequired(RomLoadError):
    """A ZIP contains multiple ROMs and needs an explicit member choice."""

    def __init__(self, path: Path, roms: list[ArchiveRom]) -> None:
        self.path = path
        self.roms = tuple(roms)
        preview = ", ".join(rom.member_name for rom in self.roms[:5])
        if len(self.roms) > 5:
            preview += f", and {len(self.roms) - 5} more"
        super().__init__(
            f"{path.name} contains multiple NES ROMs ({preview}); "
            "choose one with --zip-member"
        )


@dataclass(frozen=True, slots=True)
class LoadedRom:
    cartridge: Cartridge
    source_path: Path
    member_name: str | None


def is_zip_source(path: str | Path) -> bool:
    return Path(path).suffix.casefold() == ".zip"


def _archive_infos(
    archive: zipfile.ZipFile,
    source: Path,
) -> tuple[list[zipfile.ZipInfo], list[ArchiveRom]]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise RomLoadError(
            f"{source.name} contains {len(infos)} entries; "
            f"the safety limit is {MAX_ARCHIVE_ENTRIES}"
        )

    rom_infos = [
        info
        for info in infos
        if not info.is_dir()
        and PurePosixPath(info.filename.replace("\\", "/")).suffix.casefold()
        == ".nes"
    ]
    rom_infos.sort(key=lambda info: info.filename.casefold())
    roms = [
        ArchiveRom(
            member_name=info.filename,
            size_bytes=info.file_size,
            compressed_size=info.compress_size,
        )
        for info in rom_infos
    ]
    if not roms:
        raise RomLoadError(f"{source.name} does not contain a .nes ROM")
    return rom_infos, roms


def archive_roms(path: str | Path) -> list[ArchiveRom]:
    """Return the selectable NES members without extracting the archive."""

    source = Path(path).expanduser().resolve()
    try:
        with zipfile.ZipFile(source, "r") as archive:
            _infos, roms = _archive_infos(archive, source)
            return roms
    except RomLoadError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RomLoadError(f"could not read ZIP archive {source.name}: {exc}") from exc


def _read_archive_member(
    archive: zipfile.ZipFile,
    source: Path,
    rom_infos: list[zipfile.ZipInfo],
    member_name: str,
) -> bytes:
    matches = [info for info in rom_infos if info.filename == member_name]
    if not matches:
        raise RomLoadError(
            f"{member_name!r} is not a .nes ROM in {source.name}"
        )
    if len(matches) > 1:
        raise RomLoadError(
            f"{source.name} contains duplicate entries named {member_name!r}"
        )
    info = matches[0]
    if info.flag_bits & 0x01:
        raise RomLoadError(
            f"{member_name!r} is encrypted; password-protected ZIPs are unsupported"
        )
    if info.file_size > MAX_ROM_BYTES:
        raise RomLoadError(
            f"{member_name!r} is {info.file_size} bytes; "
            f"the supported limit is {MAX_ROM_BYTES}"
        )
    try:
        with archive.open(info, "r") as handle:
            data = handle.read(MAX_ROM_BYTES + 1)
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
    ) as exc:
        raise RomLoadError(
            f"could not decompress {member_name!r} from {source.name}: {exc}"
        ) from exc
    if len(data) > MAX_ROM_BYTES:
        raise RomLoadError(
            f"{member_name!r} expands beyond the supported ROM size limit"
        )
    return data


def load_rom(
    path: str | Path,
    member_name: str | None = None,
) -> LoadedRom:
    """Load a raw image or one selected NES member from a ZIP archive."""

    source = Path(path).expanduser().resolve()
    if not is_zip_source(source):
        if member_name is not None:
            raise RomLoadError("--zip-member can only be used with a .zip file")
        return LoadedRom(
            cartridge=Cartridge.from_file(source),
            source_path=source,
            member_name=None,
        )

    try:
        with zipfile.ZipFile(source, "r") as archive:
            rom_infos, roms = _archive_infos(archive, source)
            selected = member_name
            if selected is None:
                if len(roms) > 1:
                    raise ArchiveSelectionRequired(source, roms)
                selected = roms[0].member_name
            data = _read_archive_member(
                archive,
                source,
                rom_infos,
                selected,
            )
    except RomLoadError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RomLoadError(f"could not read ZIP archive {source.name}: {exc}") from exc

    normalized = selected.replace("\\", "/")
    title = PurePosixPath(normalized).stem or source.stem
    return LoadedRom(
        cartridge=Cartridge.from_bytes(data, title=title),
        source_path=source,
        member_name=selected,
    )
