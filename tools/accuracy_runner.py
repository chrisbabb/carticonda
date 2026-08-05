#!/usr/bin/env python3
"""Run public NES hardware diagnostics through a repeatable harness.

The runner understands the memory protocol used by blargg-style test ROMs:

* $6001-$6003 contains the signature DE B0 61;
* $6000 is $80 while running, $81 when a delayed reset is requested, and
  $00-$7F when complete; and
* a zero-terminated diagnostic message starts at $6004.

With ``--screen-console``, it also recognizes the older blargg ASCII console:
an exact ``Passed`` line is success and an exact ``Failed``/``Error n`` line
is failure.  Other on-screen output is preserved in timeout reports so CRC-only
diagnostics can still be compared with recorded hardware results.

No test ROMs are bundled. Point this tool at independently obtained,
freely-distributable diagnostics.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter

from nes_from_scratch.console import Console
from nes_from_scratch.rom_loader import (
    ArchiveRom,
    archive_roms,
    load_rom,
)


TEST_SIGNATURE = (0xDE, 0xB0, 0x61)
RUNNING = 0x80
RESET_REQUIRED = 0x81


@dataclass(frozen=True, slots=True)
class RomCase:
    source: Path
    member_name: str | None = None

    @property
    def name(self) -> str:
        if self.member_name is None:
            return str(self.source)
        return f"{self.source}::{self.member_name}"


@dataclass(frozen=True, slots=True)
class AccuracyResult:
    name: str
    outcome: str
    status: int | None
    message: str
    frames: int
    resets: int
    mapper: int | None
    submapper: int | None
    seconds: float


def _discover_path(path: Path) -> list[RomCase]:
    path = path.expanduser().resolve()
    if path.is_dir():
        cases = [
            RomCase(candidate)
            for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.suffix.casefold() == ".nes"
        ]
        for archive in (
            candidate
            for candidate in path.rglob("*")
            if candidate.is_file()
            and candidate.suffix.casefold() == ".zip"
        ):
            cases.extend(
                RomCase(archive, rom.member_name)
                for rom in archive_roms(archive)
            )
        return cases
    if path.suffix.casefold() == ".nes":
        return [RomCase(path)]
    if path.suffix.casefold() == ".zip":
        roms: list[ArchiveRom] = archive_roms(path)
        return [RomCase(path, rom.member_name) for rom in roms]
    raise ValueError(f"{path} is not a .nes file, .zip archive, or directory")


def discover_cases(paths: list[str | Path]) -> list[RomCase]:
    cases: list[RomCase] = []
    for raw_path in paths:
        cases.extend(_discover_path(Path(raw_path)))
    unique = {
        (str(case.source), case.member_name): case
        for case in cases
    }
    return sorted(
        unique.values(),
        key=lambda case: (
            str(case.source).casefold(),
            (case.member_name or "").casefold(),
        ),
    )


def _mapper_peek(console: Console, address: int) -> int:
    """Read test output without perturbing the emulated CPU's open bus."""
    return console.cartridge.mapper.cpu_read(address) & 0xFF


def _has_signature(console: Console) -> bool:
    return tuple(
        _mapper_peek(console, address)
        for address in range(0x6001, 0x6004)
    ) == TEST_SIGNATURE


def _message(console: Console, limit: int = 4096) -> str:
    data = bytearray()
    for offset in range(limit):
        value = _mapper_peek(console, 0x6004 + offset)
        if value == 0:
            break
        data.append(value)
    return data.decode("utf-8", errors="replace").strip()


def _screen_message(console: Console) -> str:
    """Decode printable rows from the diagnostic ASCII nametable console."""
    nametables = bytes(console.ppu.nametable_ram)
    lines: list[str] = []
    for page_start in range(0, len(nametables), 0x400):
        page = nametables[page_start:page_start + 0x400]
        if len(page) < 0x400:
            break
        # The final two rows are attribute bytes, not character cells.
        for row in range(30):
            cells = page[row * 32:(row + 1) * 32]
            text = "".join(
                chr(value) if 0x20 <= value < 0x7F else " "
                for value in cells
            ).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


def _screen_terminal(console: Console) -> tuple[str, int, str] | None:
    message = _screen_message(console)
    for line in message.splitlines():
        normalized = line.strip().casefold()
        if normalized == "passed":
            return "pass", 0, message
        if (
            normalized == "failed"
            or normalized.startswith("failed ")
            or normalized.startswith("failed:")
            or (
            normalized.startswith("error ")
            and normalized[6:].isdigit()
            )
        ):
            return "fail", 1, message
    return None


def run_case(
    case: RomCase,
    *,
    max_frames: int = 3600,
    reset_delay_frames: int = 7,
    max_resets: int = 4,
    fast_ppu: bool = False,
    screen_console: bool = False,
) -> AccuracyResult:
    started = perf_counter()
    mapper: int | None = None
    submapper: int | None = None
    frames = 0
    resets = 0
    try:
        loaded = load_rom(case.source, case.member_name)
        mapper = loaded.cartridge.mapper_number
        submapper = loaded.cartridge.submapper
        console = Console(
            loaded.cartridge,
            sample_rate=0,
            fast_ppu=fast_ppu,
        )
        reset_requested_at: int | None = None
        waiting_for_reset_ack = False

        while frames < max_frames:
            console.run_frame(copy_frame=False)
            frames += 1
            if not _has_signature(console):
                reset_requested_at = None
                if screen_console:
                    terminal = _screen_terminal(console)
                    if terminal is not None:
                        outcome, status, message = terminal
                        return AccuracyResult(
                            name=case.name,
                            outcome=outcome,
                            status=status,
                            message=message,
                            frames=frames,
                            resets=resets,
                            mapper=mapper,
                            submapper=submapper,
                            seconds=perf_counter() - started,
                        )
                continue

            status = _mapper_peek(console, 0x6000)
            if waiting_for_reset_ack:
                if status == RESET_REQUIRED:
                    continue
                waiting_for_reset_ack = False
            if status == RUNNING:
                reset_requested_at = None
                continue
            if status == RESET_REQUIRED:
                if reset_requested_at is None:
                    reset_requested_at = frames
                elif frames - reset_requested_at >= reset_delay_frames:
                    if resets >= max_resets:
                        return AccuracyResult(
                            name=case.name,
                            outcome="error",
                            status=status,
                            message=(
                                "test requested more than "
                                f"{max_resets} resets"
                            ),
                            frames=frames,
                            resets=resets,
                            mapper=mapper,
                            submapper=submapper,
                            seconds=perf_counter() - started,
                        )
                    console.reset()
                    resets += 1
                    reset_requested_at = None
                    waiting_for_reset_ack = True
                continue

            return AccuracyResult(
                name=case.name,
                outcome="pass" if status == 0 else "fail",
                status=status,
                message=_message(console),
                frames=frames,
                resets=resets,
                mapper=mapper,
                submapper=submapper,
                seconds=perf_counter() - started,
            )

        has_signature = _has_signature(console)
        screen_message = (
            _screen_message(console)
            if screen_console and not has_signature
            else ""
        )
        return AccuracyResult(
            name=case.name,
            outcome="timeout",
            status=(
                _mapper_peek(console, 0x6000)
                if has_signature
                else None
            ),
            message=(
                _message(console)
                if has_signature
                else (
                    screen_message
                    or "test protocol signature was not observed"
                )
            ),
            frames=frames,
            resets=resets,
            mapper=mapper,
            submapper=submapper,
            seconds=perf_counter() - started,
        )
    except Exception as exc:
        return AccuracyResult(
            name=case.name,
            outcome="error",
            status=None,
            message=f"{type(exc).__name__}: {exc}",
            frames=frames,
            resets=resets,
            mapper=mapper,
            submapper=submapper,
            seconds=perf_counter() - started,
        )


def result_document(results: list[AccuracyResult]) -> dict:
    counts = {
        outcome: sum(result.outcome == outcome for result in results)
        for outcome in ("pass", "fail", "timeout", "error")
    }
    return {
        "schema": 1,
        "summary": {
            "total": len(results),
            **counts,
            "all_passed": bool(results) and counts["pass"] == len(results),
        },
        "results": [asdict(result) for result in results],
    }


def write_junit(path: Path, results: list[AccuracyResult]) -> None:
    failures = sum(result.outcome == "fail" for result in results)
    errors = sum(
        result.outcome in ("timeout", "error") for result in results
    )
    suite = ET.Element(
        "testsuite",
        {
            "name": "Cartaconda NES accuracy",
            "tests": str(len(results)),
            "failures": str(failures),
            "errors": str(errors),
            "time": f"{sum(result.seconds for result in results):.6f}",
        },
    )
    for result in results:
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "name": result.name,
                "classname": "nes.hardware",
                "time": f"{result.seconds:.6f}",
            },
        )
        details = (
            f"status={result.status!r}; frames={result.frames}; "
            f"resets={result.resets}\n{result.message}"
        )
        if result.outcome == "fail":
            node = ET.SubElement(
                case,
                "failure",
                {"message": f"test status {result.status}"},
            )
            node.text = details
        elif result.outcome in ("timeout", "error"):
            node = ET.SubElement(
                case,
                "error",
                {"message": result.outcome},
            )
            node.text = details
        elif result.message:
            output = ET.SubElement(case, "system-out")
            output.text = result.message
    tree = ET.ElementTree(suite)
    ET.indent(tree, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run NES diagnostics from ROM files, ZIPs, or directories."
        )
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="one or more .nes files, .zip archives, or directories",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=3600,
        help="emulated-frame timeout per ROM (default: 3600)",
    )
    parser.add_argument(
        "--reset-delay-frames",
        type=int,
        default=7,
        help="wait this many frames before honoring status $81 (default: 7)",
    )
    parser.add_argument(
        "--max-resets",
        type=int,
        default=4,
        help="maximum delayed reset requests per ROM (default: 4)",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="use the optimized PPU; the dot path is the accuracy default",
    )
    parser.add_argument(
        "--screen-console",
        action="store_true",
        help=(
            "also recognize exact Passed/Failed lines from older on-screen "
            "blargg diagnostics"
        ),
    )
    parser.add_argument(
        "--json",
        type=Path,
        dest="json_path",
        help="write the complete result document to this path",
    )
    parser.add_argument(
        "--junit",
        type=Path,
        help="write JUnit XML for CI to this path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_frames <= 0:
        parser.error("--max-frames must be positive")
    if args.reset_delay_frames < 6:
        parser.error(
            "--reset-delay-frames must be at least 6 (100 ms at NTSC)"
        )
    if args.max_resets < 0:
        parser.error("--max-resets cannot be negative")

    try:
        cases = discover_cases(args.paths)
    except Exception as exc:
        parser.error(str(exc))
    if not cases:
        parser.error("no .nes ROMs were found")

    results: list[AccuracyResult] = []
    for case in cases:
        result = run_case(
            case,
            max_frames=args.max_frames,
            reset_delay_frames=args.reset_delay_frames,
            max_resets=args.max_resets,
            fast_ppu=args.fast,
            screen_console=args.screen_console,
        )
        results.append(result)
        status = "--" if result.status is None else f"{result.status:02X}"
        print(
            f"{result.outcome.upper():7} status={status} "
            f"frames={result.frames:4} mapper={result.mapper!s:>3} "
            f"{result.name}"
        )
        if result.message:
            for line in result.message.splitlines():
                print(f"         {line}")

    document = result_document(results)
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(
            json.dumps(document, indent=2) + "\n",
            encoding="utf-8",
        )
    if args.junit is not None:
        write_junit(args.junit, results)

    summary = document["summary"]
    print(
        "Summary: "
        f"{summary['pass']} passed, {summary['fail']} failed, "
        f"{summary['timeout']} timed out, {summary['error']} errors"
    )
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
