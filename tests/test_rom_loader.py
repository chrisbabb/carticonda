from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from nes_from_scratch.cartridge import Cartridge
from nes_from_scratch.rom_loader import (
    ArchiveSelectionRequired,
    RomLoadError,
    archive_roms,
    load_rom,
)
from support import make_ines
from tools.make_demo_rom import build_rom


class RomLoaderTests(unittest.TestCase):
    def test_single_rom_archive_loads_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "demo.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("games/My Demo.NES", build_rom())
                archive.writestr("readme.txt", "not a ROM")
            loaded = load_rom(archive_path)
        self.assertEqual(loaded.member_name, "games/My Demo.NES")
        self.assertEqual(loaded.cartridge.title, "My Demo")
        self.assertEqual(loaded.cartridge.mapper_number, 0)
        self.assertEqual(
            loaded.cartridge.sha256,
            Cartridge.from_bytes(build_rom()).sha256,
        )

    def test_mapper_9_rom_loads_directly_from_a_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "mmc2.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "homebrew/mmc2-test.nes",
                    make_ines(mapper=9, prg_banks=8, chr_banks=16),
                )
            loaded = load_rom(archive_path)

        self.assertEqual(loaded.member_name, "homebrew/mmc2-test.nes")
        self.assertEqual(loaded.cartridge.mapper_number, 9)
        self.assertEqual(loaded.cartridge.mapper.mapper_number, 9)

    def test_multiple_roms_require_and_honor_a_member_choice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "collection.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("Zeta.nes", build_rom())
                archive.writestr("folder/Alpha.NES", build_rom())

            with self.assertRaises(ArchiveSelectionRequired) as caught:
                load_rom(archive_path)
            choices = archive_roms(archive_path)
            selected = load_rom(archive_path, "Zeta.nes")

        self.assertEqual(
            [choice.member_name for choice in caught.exception.roms],
            ["folder/Alpha.NES", "Zeta.nes"],
        )
        self.assertEqual(
            [choice.member_name for choice in choices],
            ["folder/Alpha.NES", "Zeta.nes"],
        )
        self.assertEqual(selected.member_name, "Zeta.nes")
        self.assertEqual(selected.cartridge.title, "Zeta")

    def test_archive_without_a_rom_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "empty.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("notes.txt", "hello")
            with self.assertRaisesRegex(RomLoadError, "does not contain"):
                load_rom(archive_path)

    def test_corrupt_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "broken.zip"
            archive_path.write_bytes(b"this is not a zip")
            with self.assertRaisesRegex(RomLoadError, "could not read ZIP"):
                load_rom(archive_path)

    def test_expanded_rom_size_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "large.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("demo.nes", build_rom())
            with patch("nes_from_scratch.rom_loader.MAX_ROM_BYTES", 1024):
                with self.assertRaisesRegex(RomLoadError, "supported limit"):
                    load_rom(archive_path)

    def test_zip_member_is_rejected_for_raw_roms(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rom_path = Path(directory) / "demo.nes"
            rom_path.write_bytes(build_rom())
            with self.assertRaisesRegex(RomLoadError, "only be used"):
                load_rom(rom_path, "demo.nes")

    def test_member_paths_are_read_without_filesystem_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "paths.zip"
            outside = root.parent / f"{root.name}-escaped.nes"
            self.assertFalse(outside.exists())
            member_name = f"../../{outside.name}"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(member_name, build_rom())
            loaded = load_rom(archive_path)
            self.assertEqual(loaded.member_name, member_name)
            self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
