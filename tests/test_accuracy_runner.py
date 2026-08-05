from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tools.accuracy_runner import (
    AccuracyResult,
    RomCase,
    discover_cases,
    result_document,
    run_case,
    write_junit,
)

from support import make_ines


def protocol_program(status: int, message: bytes = b"") -> bytes:
    program = bytearray()

    def store(address: int, value: int) -> None:
        program.extend(
            (0xA9, value & 0xFF, 0x8D, address & 0xFF, address >> 8)
        )

    for address, value in zip(
        range(0x6001, 0x6004),
        (0xDE, 0xB0, 0x61),
    ):
        store(address, value)
    for offset, value in enumerate(message):
        store(0x6004 + offset, value)
    store(0x6004 + len(message), 0)
    store(0x6000, status)
    loop = 0x8000 + len(program)
    program.extend((0x4C, loop & 0xFF, loop >> 8))
    return bytes(program)


def screen_program(message: bytes) -> bytes:
    program = bytearray()

    def store(address: int, value: int) -> None:
        program.extend(
            (0xA9, value & 0xFF, 0x8D, address & 0xFF, address >> 8)
        )

    store(0x2006, 0x20)
    store(0x2006, 0x00)
    for value in message:
        store(0x2007, value)
    loop = 0x8000 + len(program)
    program.extend((0x4C, loop & 0xFF, loop >> 8))
    return bytes(program)


class AccuracyRunnerTests(unittest.TestCase):
    def test_blargg_protocol_pass_and_failure_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            passed = root / "passed.nes"
            failed = root / "failed.nes"
            passed.write_bytes(
                make_ines(
                    battery=True,
                    program=protocol_program(0, b"Passed"),
                )
            )
            failed.write_bytes(
                make_ines(
                    battery=True,
                    program=protocol_program(3, b"Failure 3"),
                )
            )

            pass_result = run_case(RomCase(passed), max_frames=2)
            fail_result = run_case(RomCase(failed), max_frames=2)

        self.assertEqual(pass_result.outcome, "pass")
        self.assertEqual(pass_result.status, 0)
        self.assertEqual(pass_result.message, "Passed")
        self.assertEqual(fail_result.outcome, "fail")
        self.assertEqual(fail_result.status, 3)
        self.assertEqual(fail_result.message, "Failure 3")

    def test_legacy_screen_console_pass_and_failure_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            passed = root / "screen-pass.nes"
            failed = root / "screen-fail.nes"
            passed.write_bytes(
                make_ines(chr_banks=0, program=screen_program(b"Passed"))
            )
            failed.write_bytes(
                make_ines(chr_banks=0, program=screen_program(b"Failed"))
            )

            without_fallback = run_case(
                RomCase(passed),
                max_frames=2,
            )
            pass_result = run_case(
                RomCase(passed),
                max_frames=2,
                screen_console=True,
            )
            fail_result = run_case(
                RomCase(failed),
                max_frames=2,
                screen_console=True,
            )

        self.assertEqual(without_fallback.outcome, "timeout")
        self.assertEqual(pass_result.outcome, "pass")
        self.assertIn("Passed", pass_result.message)
        self.assertEqual(fail_result.outcome, "fail")
        self.assertIn("Failed", fail_result.message)

    def test_legacy_screen_console_recognizes_numbered_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "screen-fail-code.nes"
            path.write_bytes(
                make_ines(
                    chr_banks=0,
                    program=screen_program(b"FAILED: #7"),
                )
            )
            result = run_case(
                RomCase(path),
                max_frames=2,
                screen_console=True,
            )
        self.assertEqual(result.outcome, "fail")
        self.assertIn("FAILED: #7", result.message)

    def test_discovery_is_recursive_deduplicated_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            (root / "b.nes").write_bytes(b"")
            (nested / "A.NES").write_bytes(b"")
            cases = discover_cases([root, root / "b.nes"])
        self.assertEqual(
            [case.source.name for case in cases],
            ["b.nes", "A.NES"],
        )

    def test_json_and_junit_outputs_preserve_outcomes(self) -> None:
        results = [
            AccuracyResult(
                name="pass.nes",
                outcome="pass",
                status=0,
                message="Passed",
                frames=2,
                resets=0,
                mapper=0,
                submapper=0,
                seconds=0.1,
            ),
            AccuracyResult(
                name="fail.nes",
                outcome="fail",
                status=2,
                message="Timing error",
                frames=3,
                resets=0,
                mapper=0,
                submapper=0,
                seconds=0.2,
            ),
        ]
        document = result_document(results)
        self.assertEqual(document["summary"]["pass"], 1)
        self.assertEqual(document["summary"]["fail"], 1)
        self.assertFalse(document["summary"]["all_passed"])
        json.dumps(document)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "results.xml"
            write_junit(path, results)
            root = ET.parse(path).getroot()
        self.assertEqual(root.attrib["tests"], "2")
        self.assertEqual(root.attrib["failures"], "1")
        self.assertEqual(root.attrib["errors"], "0")


if __name__ == "__main__":
    unittest.main()
