"""Command-line entry point."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from . import __version__
from .cartridge import Cartridge, CartridgeError
from .console import Console
from .constants import DEFAULT_SAMPLE_RATE
from .emulation_worker import EmulationWorkerError
from .frontend import FrontendUnavailable, PygameFrontend
from .mappers import MapperError
from .rom_loader import ArchiveSelectionRequired, RomLoadError, load_rom


def _size(value: int) -> str:
    if value % 1024 == 0:
        return f"{value // 1024} KiB"
    return f"{value} bytes"


def _print_info(cartridge: Cartridge) -> None:
    info = cartridge.info
    print(f"Title:       {info.title}")
    print(f"Format:      {info.format_name}")
    print(f"Mapper:      {info.mapper} (submapper {info.submapper})")
    print(f"PRG ROM:     {_size(info.prg_rom_size)}")
    print(f"CHR:         {_size(info.chr_size)} ({'RAM' if info.chr_is_ram else 'ROM'})")
    print(f"PRG RAM:     {_size(info.prg_ram_size)}")
    if info.prg_nvram_size:
        print(f"PRG NVRAM:   {_size(info.prg_nvram_size)}")
    if not info.chr_is_ram and info.chr_ram_size:
        print(f"CHR RAM:     {_size(info.chr_ram_size)}")
    if info.chr_nvram_size:
        print(f"CHR NVRAM:   {_size(info.chr_nvram_size)}")
    print(f"Mirroring:   {info.mirroring.value}")
    print(f"Battery:     {'yes' if info.battery else 'no'}")
    print(f"SHA-256:     {info.sha256}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cartaconda",
        description=(
            "Cartaconda - Python-powered 8-bit emulation. Run an NTSC NES "
            "cartridge image using the original Python emulation core. Use "
            "only ROM images you are legally permitted to use."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "rom",
        nargs="?",
        type=Path,
        help="optional path to a .nes image or .zip ROM collection",
    )
    parser.add_argument("--info", action="store_true", help="show cartridge metadata and exit")
    parser.add_argument(
        "--zip-member",
        metavar="NAME",
        help="ROM member to use when a ZIP contains multiple .nes files",
    )
    parser.add_argument(
        "--scale",
        type=int,
        choices=(2, 3, 4),
        help="window scale; otherwise use the saved preference",
    )
    parser.add_argument(
        "--fullscreen",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override the saved fullscreen preference",
    )
    parser.add_argument("--mute", action="store_true", help="disable audio output")
    parser.add_argument(
        "--no-gamepad",
        action="store_true",
        help=(
            "disable SDL game-controller discovery (useful when isolating "
            "a native driver crash)"
        ),
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help=(
            "print SDL/mixer configuration and clean-run timing counters "
            "to stderr"
        ),
    )
    parser.add_argument(
        "--cycle-accurate",
        action="store_true",
        help="use the slower dot-by-dot PPU path in the interactive frontend",
    )
    parser.add_argument("--state-dir", type=Path, help="override save-state directory")
    parser.add_argument("--battery-dir", type=Path, help="override battery-save directory")
    parser.add_argument(
        "--headless-frames",
        type=int,
        metavar="N",
        help="emulate N frames without opening a window, then print a frame hash",
    )
    parser.add_argument(
        "--trace",
        type=int,
        metavar="N",
        help="print and execute N CPU instructions without opening a window",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    needs_rom = (
        args.info
        or args.trace is not None
        or args.headless_frames is not None
    )
    if args.rom is None and needs_rom:
        parser.error("a ROM path is required with --info, --trace, or --headless-frames")
    if args.zip_member is not None and args.rom is None:
        parser.error("--zip-member requires a ROM path")

    try:
        interactive = (
            not args.info
            and args.trace is None
            and args.headless_frames is None
        )
        loaded = None
        startup_path = None
        if args.rom is not None:
            try:
                loaded = load_rom(args.rom, args.zip_member)
            except ArchiveSelectionRequired:
                if not interactive:
                    raise
                startup_path = args.rom
        cartridge = loaded.cartridge if loaded is not None else None
        if args.info and cartridge is not None:
            _print_info(cartridge)
            return 0
        console = (
            Console(
                cartridge,
                sample_rate=(
                    DEFAULT_SAMPLE_RATE
                    if interactive and not args.mute
                    else 0
                ),
                fast_ppu=interactive and not args.cycle_accurate,
            )
            if cartridge is not None
            else None
        )
        if args.trace is not None:
            assert console is not None
            for _ in range(max(0, args.trace)):
                line, _length = console.cpu.disassemble_at(console.cpu.pc)
                print(
                    f"{line:<34} "
                    f"A:{console.cpu.a:02X} X:{console.cpu.x:02X} "
                    f"Y:{console.cpu.y:02X} P:{console.cpu.p:02X} "
                    f"SP:{console.cpu.sp:02X}"
                )
                console.step_instruction()
            return 0
        if args.headless_frames is not None:
            assert console is not None
            frame = b""
            for _ in range(max(0, args.headless_frames)):
                frame = console.run_frame()
            print(
                f"frames={max(0, args.headless_frames)} "
                f"cpu_cycles={console.bus.cpu_cycles} "
                f"frame_sha256={hashlib.sha256(frame).hexdigest()}"
            )
            return 0
        frontend = PygameFrontend(
            console,
            rom_path=loaded.source_path if loaded is not None else startup_path,
            rom_member=loaded.member_name if loaded is not None else None,
            scale=args.scale,
            fullscreen=args.fullscreen,
            audio=not args.mute,
            state_directory=args.state_dir,
            battery_directory=args.battery_dir,
            fast_ppu=not args.cycle_accurate,
            gamepads=not args.no_gamepad,
            diagnostics=args.diagnostics,
        )
        return frontend.run()
    except (
        OSError,
        CartridgeError,
        MapperError,
        RomLoadError,
        FrontendUnavailable,
        EmulationWorkerError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
