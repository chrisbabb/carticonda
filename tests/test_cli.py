from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from nes_from_scratch import __version__
from nes_from_scratch.cli import build_parser, main
from tools.make_demo_rom import build_rom


class CLIParserTests(unittest.TestCase):
    def test_version_flag_reports_release_version(self) -> None:
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with redirect_stdout(output):
                build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"cartaconda {__version__}")

    def test_interactive_launcher_does_not_require_a_rom(self) -> None:
        arguments = build_parser().parse_args([])
        self.assertIsNone(arguments.rom)
        self.assertIsNone(arguments.scale)
        self.assertIsNone(arguments.fullscreen)

    def test_interactive_overrides_are_parsed(self) -> None:
        arguments = build_parser().parse_args(
            [
                "game.nes",
                "--scale",
                "4",
                "--fullscreen",
                "--mute",
                "--no-gamepad",
                "--diagnostics",
            ]
        )
        self.assertEqual(arguments.rom.name, "game.nes")
        self.assertEqual(arguments.scale, 4)
        self.assertTrue(arguments.fullscreen)
        self.assertTrue(arguments.mute)
        self.assertTrue(arguments.no_gamepad)
        self.assertTrue(arguments.diagnostics)

    def test_windowed_override_is_available(self) -> None:
        arguments = build_parser().parse_args(["--no-fullscreen"])
        self.assertFalse(arguments.fullscreen)

    def test_zip_member_is_parsed(self) -> None:
        arguments = build_parser().parse_args(
            ["collection.zip", "--zip-member", "games/demo.nes"]
        )
        self.assertEqual(arguments.rom.name, "collection.zip")
        self.assertEqual(arguments.zip_member, "games/demo.nes")

    def test_main_opens_the_launcher_without_a_rom(self) -> None:
        with patch("nes_from_scratch.cli.PygameFrontend") as frontend:
            frontend.return_value.run.return_value = 0
            result = main([])
        self.assertEqual(result, 0)
        frontend.assert_called_once()
        self.assertIsNone(frontend.call_args.args[0])
        self.assertTrue(frontend.call_args.kwargs["fast_ppu"])
        self.assertTrue(frontend.call_args.kwargs["gamepads"])
        self.assertFalse(frontend.call_args.kwargs["diagnostics"])

    def test_interactive_collection_opens_the_member_picker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "collection.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one.nes", build_rom())
                archive.writestr("two.nes", build_rom())
            with patch("nes_from_scratch.cli.PygameFrontend") as frontend:
                frontend.return_value.run.return_value = 0
                result = main([str(archive_path)])
        self.assertEqual(result, 0)
        self.assertIsNone(frontend.call_args.args[0])
        self.assertEqual(
            frontend.call_args.kwargs["rom_path"],
            archive_path,
        )

    def test_headless_collection_accepts_an_explicit_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "collection.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one.nes", build_rom())
                archive.writestr("two.nes", build_rom())
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        str(archive_path),
                        "--zip-member",
                        "two.nes",
                        "--headless-frames",
                        "1",
                    ]
                )
        self.assertEqual(result, 0)
        self.assertIn("frames=1", output.getvalue())

    def test_noninteractive_collection_requires_a_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "collection.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("one.nes", build_rom())
                archive.writestr("two.nes", build_rom())
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = main([str(archive_path), "--info"])
        self.assertEqual(result, 2)
        self.assertIn("--zip-member", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
