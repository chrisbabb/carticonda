from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

from nes_from_scratch.frontend import (
    BRAND_LOGO_PATH,
    BRAND_NAME,
    BRAND_TAGLINE,
)
from nes_from_scratch.ui_model import (
    DEFAULT_GAMEPAD_BINDINGS,
    DEFAULT_KEY_BINDINGS,
    FrontendSettings,
    browser_entries,
    browser_roots,
    state_slots,
)


class BrandingTests(unittest.TestCase):
    def test_cartaconda_identity_and_packaged_logo(self) -> None:
        self.assertEqual(BRAND_NAME, "Cartaconda")
        self.assertEqual(
            BRAND_TAGLINE,
            "Python-powered 8-bit emulation",
        )
        image = BRAND_LOGO_PATH.read_bytes()
        self.assertEqual(image[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", image[16:24]), (1935, 813))


class FrontendSettingsTests(unittest.TestCase):
    def test_settings_round_trip_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = FrontendSettings(
                volume=0.42,
                muted=True,
                scale=4,
                fullscreen=True,
            )
            settings.key_bindings["p1_a"] = "space"
            settings.gamepad_bindings["p1_a"] = ["button:4"]
            settings.save(path)
            restored = FrontendSettings.load(path)
        self.assertEqual(restored.volume, 0.42)
        self.assertTrue(restored.muted)
        self.assertEqual(restored.scale, 4)
        self.assertTrue(restored.fullscreen)
        self.assertEqual(restored.key_bindings["p1_a"], "space")
        self.assertEqual(restored.gamepad_bindings["p1_a"], ["button:4"])

    def test_invalid_settings_fall_back_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "volume": 12,
                        "scale": 99,
                        "recent_roms": ["valid.nes", 42],
                        "key_bindings": {"p1_a": "", "p1_b": "space"},
                        "gamepad_bindings": {
                            "p1_a": ["button:5", "button:5", "invalid"],
                            "p1_b": ["button:-1"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            settings = FrontendSettings.load(path)
        self.assertEqual(settings.volume, 1.0)
        self.assertEqual(settings.scale, 3)
        self.assertEqual(settings.recent_roms, ["valid.nes"])
        self.assertEqual(settings.key_bindings["p1_a"], DEFAULT_KEY_BINDINGS["p1_a"])
        self.assertEqual(settings.key_bindings["p1_b"], "space")
        self.assertEqual(settings.gamepad_bindings["p1_a"], ["button:5"])
        self.assertEqual(
            settings.gamepad_bindings["p1_b"],
            DEFAULT_GAMEPAD_BINDINGS["p1_b"],
        )

    def test_gamepad_reset_uses_independent_default_lists(self) -> None:
        settings = FrontendSettings()
        settings.gamepad_bindings["p1_a"].append("button:5")
        self.assertNotIn("button:5", DEFAULT_GAMEPAD_BINDINGS["p1_a"])
        settings.reset_gamepad_controls()
        self.assertEqual(
            settings.gamepad_bindings["p1_a"],
            DEFAULT_GAMEPAD_BINDINGS["p1_a"],
        )

    def test_recent_roms_are_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "one.nes"
            second = Path(directory) / "two.nes"
            settings = FrontendSettings()
            settings.remember_rom(first)
            settings.remember_rom(second)
            settings.remember_rom(first)
        self.assertEqual(
            settings.recent_roms,
            [str(first.resolve()), str(second.resolve())],
        )


class UIFileModelTests(unittest.TestCase):
    def test_state_slot_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rom_hash = "a" * 64
            state_path = root / rom_hash[:16] / "slot-3.fnes"
            state_path.parent.mkdir(parents=True)
            state_path.write_bytes(b"state")
            slots = state_slots(root, rom_hash)
        self.assertEqual(len(slots), 10)
        self.assertFalse(slots[0].exists)
        self.assertTrue(slots[3].exists)
        self.assertEqual(slots[3].size_bytes, 5)

    def test_browser_lists_directories_then_rom_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Games").mkdir()
            (root / "zeta.nes").write_bytes(b"")
            (root / "alpha.NES").write_bytes(b"")
            (root / "bundle.ZIP").write_bytes(b"")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            entries = browser_entries(root)
        self.assertEqual(
            [
                (entry.label, entry.is_directory, entry.is_archive)
                for entry in entries
            ],
            [
                ("Games", True, False),
                ("alpha.NES", False, False),
                ("bundle.ZIP", False, True),
                ("zeta.nes", False, False),
            ],
        )

    def test_browser_lists_all_windows_drives_from_virtual_root(self) -> None:
        entries = browser_roots(
            platform_name="nt",
            drive_names=("D:\\", "c:\\", "D:\\"),
        )
        self.assertEqual([entry.label for entry in entries], ["C:\\", "D:\\"])
        self.assertTrue(all(entry.is_directory for entry in entries))


if __name__ == "__main__":
    unittest.main()
