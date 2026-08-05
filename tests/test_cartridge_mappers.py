from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nes_from_scratch.cartridge import (
    BATTERY_MAGIC,
    Cartridge,
    CartridgeError,
)
from nes_from_scratch.console import Console
from nes_from_scratch.constants import Mirroring
from nes_from_scratch.mappers import MapperError
from nes_from_scratch.ppu import PPU
from support import make_ines


def make_mmc2_cartridge() -> Cartridge:
    prg = b"".join(bytes((bank,)) * 0x2000 for bank in range(16))
    chr_data = b"".join(bytes((bank,)) * 0x1000 for bank in range(32))
    return Cartridge(
        prg_rom=prg,
        chr_data=chr_data,
        chr_is_ram=False,
        mapper_number=9,
        mirroring=Mirroring.VERTICAL,
    )


def make_mmc4_cartridge() -> Cartridge:
    prg = b"".join(bytes((bank,)) * 0x4000 for bank in range(16))
    chr_data = b"".join(bytes((bank,)) * 0x1000 for bank in range(32))
    return Cartridge(
        prg_rom=prg,
        chr_data=chr_data,
        chr_is_ram=False,
        mapper_number=10,
        mirroring=Mirroring.VERTICAL,
    )


def filled_banks(size: int, count: int) -> bytes:
    return b"".join(bytes((bank,)) * size for bank in range(count))


class CartridgeTests(unittest.TestCase):
    def test_rejects_non_ines_data(self) -> None:
        with self.assertRaises(CartridgeError):
            Cartridge.from_bytes(b"not a cartridge")

    def test_parses_header_and_nrom_mirror(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(vertical=True, battery=True, program=b"\xA5")
        )
        self.assertEqual(cart.mapper_number, 0)
        self.assertEqual(cart.mapper.mirroring, Mirroring.VERTICAL)
        self.assertTrue(cart.battery)
        self.assertEqual(cart.cpu_read(0x8000), 0xA5)
        self.assertEqual(cart.cpu_read(0xC000), 0xA5)

    def test_chr_ram_when_header_has_no_chr_banks(self) -> None:
        cart = Cartridge.from_bytes(make_ines(chr_banks=0))
        self.assertTrue(cart.chr_is_ram)
        cart.ppu_write(0x0123, 0x77)
        self.assertEqual(cart.ppu_read(0x0123), 0x77)

    def test_clean_ines_zero_prg_ram_units_infers_8k_for_compatibility(
        self,
    ) -> None:
        image = make_ines(mapper=0)
        self.assertEqual(image[8], 0)
        cart = Cartridge.from_bytes(image)
        self.assertEqual(len(cart.prg_ram), 0x2000)
        cart.cpu_write(0x6000, 0xA5)
        self.assertEqual(cart.cpu_read(0x6000), 0xA5)

    def test_nes2_zero_prg_ram_shifts_declares_no_prg_ram(self) -> None:
        header = bytearray(16)
        header[:4] = b"NES\x1a"
        header[4] = 1
        header[7] = 0x08
        cart = Cartridge.from_bytes(bytes(header) + bytes(0x4000))
        self.assertEqual(len(cart.prg_ram), 0)

    def test_nes2_exponent_multiplier_rom_sizes(self) -> None:
        # 2^13 * 3 = 24 KiB PRG, plus one linear 8 KiB CHR bank.
        header = bytearray(16)
        header[:4] = b"NES\x1a"
        header[4] = (13 << 2) | 1
        header[5] = 1
        header[7] = 0x08
        header[9] = 0x0F
        prg = bytes(24 * 1024)
        chr_rom = bytes(8 * 1024)
        cart = Cartridge.from_bytes(bytes(header) + prg + chr_rom)
        self.assertEqual(cart.info.format_name, "NES 2.0")
        self.assertEqual(cart.info.prg_rom_size, len(prg))
        self.assertEqual(cart.info.chr_size, len(chr_rom))

    def test_nes2_tracks_volatile_and_battery_ram_separately(self) -> None:
        header = bytearray(16)
        header[:4] = b"NES\x1a"
        header[4] = 1
        header[6] = 0x02
        header[7] = 0x08
        header[10] = 0x67  # 8 KiB PRG RAM + 4 KiB PRG NVRAM
        header[11] = 0x78  # 16 KiB CHR RAM + 8 KiB CHR NVRAM
        cart = Cartridge.from_bytes(bytes(header) + bytes(0x4000))
        self.assertEqual(len(cart.prg_ram), 0x3000)
        self.assertEqual(cart.prg_nvram_size, 0x1000)
        self.assertEqual(len(cart.chr_memory), 0x6000)
        self.assertEqual(cart.chr_nvram_size, 0x2000)
        self.assertTrue(cart.battery)

    def test_nes2_prg_and_chr_nvram_round_trip(self) -> None:
        cart = Cartridge(
            prg_rom=bytes(0x4000),
            chr_data=b"",
            chr_is_ram=True,
            mapper_number=0,
            battery=True,
            prg_ram_size=12,
            prg_nvram_size=4,
            chr_ram_size=8,
            chr_nvram_size=4,
        )
        cart.prg_ram[:] = b"volatilePRG!"
        cart.chr_memory[:] = b"volatileCHR!"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mixed.sav"
            self.assertTrue(cart.save_battery(path))
            self.assertTrue(path.read_bytes().startswith(BATTERY_MAGIC))
            cart.prg_ram[:] = b"x" * len(cart.prg_ram)
            cart.chr_memory[:] = b"y" * len(cart.chr_memory)
            self.assertTrue(cart.load_battery(path))

        self.assertEqual(cart.prg_ram[:-4], b"x" * 8)
        self.assertEqual(cart.prg_ram[-4:], b"PRG!")
        self.assertEqual(cart.chr_memory[:-4], b"y" * 8)
        self.assertEqual(cart.chr_memory[-4:], b"CHR!")

    def test_legacy_battery_save_remains_raw_and_compatible(self) -> None:
        cart = Cartridge(
            prg_rom=bytes(0x4000),
            chr_data=bytes(0x2000),
            chr_is_ram=False,
            mapper_number=0,
            battery=True,
            prg_ram_size=8,
        )
        cart.prg_ram[:] = b"legacy!!"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sav"
            cart.save_battery(path)
            self.assertEqual(path.read_bytes(), b"legacy!!")
            cart.prg_ram[:] = bytes(8)
            self.assertTrue(cart.load_battery(path))
        self.assertEqual(cart.prg_ram, b"legacy!!")

    def test_archaic_diskdude_header_ignores_corrupt_upper_mapper(self) -> None:
        image = bytearray(make_ines(mapper=2, chr_banks=0))
        image[7:16] = b"DiskDude!"
        cart = Cartridge.from_bytes(bytes(image))
        self.assertEqual(cart.mapper_number, 2)
        self.assertEqual(cart.info.format_name, "Archaic iNES")
        self.assertEqual(len(cart.prg_ram), 0)

    def test_trainer_is_mapped_into_7000_ram_window(self) -> None:
        base = make_ines(chr_banks=0)
        header = bytearray(base[:16])
        header[6] |= 0x04
        trainer = bytes((index & 0xFF for index in range(512)))
        cart = Cartridge.from_bytes(bytes(header) + trainer + base[16:])
        self.assertEqual(cart.cpu_read(0x7000), 0)
        self.assertEqual(cart.cpu_read(0x71FF), 0xFF)

    def test_uxrom_switches_lower_and_fixes_last_bank(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(mapper=2, prg_banks=4, chr_banks=0, fill_banks=True)
        )
        self.assertEqual(cart.cpu_read(0x8000), 0)
        self.assertEqual(cart.cpu_read(0xC000), 3)
        cart.cpu_write(0x8000, 2)
        self.assertEqual(cart.cpu_read(0x8000), 2)
        self.assertEqual(cart.cpu_read(0xC000), 3)

    def test_mmc1_serial_register_and_mirroring(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(mapper=1, prg_banks=4, chr_banks=1, fill_banks=True)
        )
        value = 0x02  # vertical mirroring, 32 KiB PRG mode
        for bit in range(5):
            cart.cpu_write(0x8000, (value >> bit) & 1)
        self.assertEqual(cart.mapper.mirroring, Mirroring.VERTICAL)

        bank_value = 0x02
        for bit in range(5):
            cart.cpu_write(0xE000, (bank_value >> bit) & 1)
        self.assertEqual(cart.cpu_read(0x8000), 2)

    def test_mmc1_bus_swaps_direct_prg_window_after_serial_commit(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(
                mapper=1,
                prg_banks=4,
                chr_banks=0,
                fill_banks=True,
            )
        )
        console = Console(cart, sample_rate=0, fast_ppu=True)
        self.assertIsNotNone(console.bus._mapped_prg_window)
        self.assertEqual(console.bus.read(0x8000), 0)
        for bit in range(5):
            console.bus.write(0xE000, (2 >> bit) & 1)
            console.bus.cpu_cycles += 1
        self.assertEqual(console.bus.read(0x8000), 2)
        self.assertEqual(console.bus.read(0xC000), 3)

    def test_mmc1_ignores_second_rmw_data_write(self) -> None:
        image = make_ines(
            mapper=1,
            prg_banks=2,
            chr_banks=0,
            program=bytes((
                0xEE, 0x00, 0xE0,  # INC $E000: dummy $00, then $01
                0xA9, 0x01,        # LDA #$01
                0x8D, 0x00, 0xE0,  # STA $E000 on a later instruction
            )),
        )
        console = Console(
            Cartridge.from_bytes(image),
            sample_rate=0,
            fast_ppu=False,
        )
        console.step_instruction()
        self.assertEqual(console.cartridge.mapper.shift, 0x08)
        console.step_instruction()
        console.step_instruction()
        self.assertEqual(console.cartridge.mapper.shift, 0x14)

    def test_explicit_bus_conflict_submapper_ands_rom_and_cpu_data(self) -> None:
        prg = bytearray(4 * 0x4000)
        prg[0] = 0x01
        conflict = Cartridge(
            prg_rom=bytes(prg),
            chr_data=bytes(0x2000),
            chr_is_ram=False,
            mapper_number=2,
            submapper=2,
        )
        conflict.cpu_write(0x8000, 0x03)
        self.assertEqual(conflict.mapper.bank, 1)

        no_conflict = Cartridge(
            prg_rom=bytes(prg),
            chr_data=bytes(0x2000),
            chr_is_ram=False,
            mapper_number=2,
            submapper=1,
        )
        no_conflict.cpu_write(0x8000, 0x03)
        self.assertEqual(no_conflict.mapper.bank, 3)

    def test_mmc3_has_fixed_last_bank_and_switchable_register(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(mapper=4, prg_banks=4, chr_banks=2, fill_banks=True)
        )
        self.assertEqual(cart.cpu_read(0xE000), 3)
        cart.cpu_write(0x8000, 0x06)
        cart.cpu_write(0x8001, 0x03)
        # PRG test data is filled per 16 KiB, while MMC3 banks are 8 KiB.
        self.assertEqual(cart.cpu_read(0x8000), 1)

    def test_mmc3_cached_prg_chr_slots_follow_registers_and_state(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x2000, 8),
            chr_data=filled_banks(0x0400, 16),
            chr_is_ram=False,
            mapper_number=4,
            mirroring=Mirroring.VERTICAL,
        )
        mapper = cart.mapper
        mapper.cpu_write(0x8000, 0x06)
        mapper.cpu_write(0x8001, 0x03)
        mapper.cpu_write(0x8000, 0x02)
        mapper.cpu_write(0x8001, 0x09)
        state = mapper.get_state()

        self.assertEqual(mapper.cpu_code_banks()[0][0], 3)
        self.assertEqual(mapper.ppu_read_pair(0x1000), (9, 9))
        mapper.cpu_write(0x8000, 0x46)
        mapper.cpu_write(0x8001, 0x05)
        self.assertEqual(mapper.cpu_code_banks()[2][0], 5)

        mapper.set_state(state)
        self.assertEqual(mapper.cpu_code_banks()[0][0], 3)
        self.assertEqual(mapper.cpu_read(0x8000), 3)
        self.assertEqual(mapper.ppu_read_pair(0x1000), (9, 9))

    def test_mmc3_pattern_tokens_isolate_the_two_chr_halves(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x2000, 8),
            chr_data=filled_banks(0x0400, 16),
            chr_is_ram=False,
            mapper_number=4,
            mirroring=Mirroring.VERTICAL,
        )
        mapper = cart.mapper
        lower_before = mapper.ppu_pattern_cache_token(0)
        upper_before = mapper.ppu_pattern_cache_token(0x1000)

        mapper.cpu_write(0x8000, 0x02)
        mapper.cpu_write(0x8001, 0x09)

        self.assertEqual(mapper.ppu_pattern_cache_token(0), lower_before)
        self.assertNotEqual(
            mapper.ppu_pattern_cache_token(0x1000),
            upper_before,
        )

    def test_mmc3_sprite_bank_change_retains_background_rows(self) -> None:
        def make_console(title: str) -> Console:
            console = Console(
                Cartridge(
                    prg_rom=filled_banks(0x2000, 8),
                    chr_data=filled_banks(0x0400, 16),
                    chr_is_ram=False,
                    mapper_number=4,
                    mirroring=Mirroring.VERTICAL,
                    title=title,
                ),
                sample_rate=0,
                fast_ppu=True,
            )
            ppu = console.ppu
            ppu.cpu_write_register(0x2000, 0x08)
            ppu.mask = 0x1E
            ppu.palette_ram[0x00] = 0x0F
            ppu.palette_ram[0x01] = 0x30
            ppu.palette_ram[0x11] = 0x21
            ppu.oam[0:4] = bytes((0, 0, 0, 0))
            ppu._render_scanline_fast(0)
            ppu._render_scanline_fast(0)
            ppu._render_scanline_fast(1)
            return console

        optimized = make_console("mmc3-granular-cache")
        reference = make_console("mmc3-conservative-cache")
        optimized.ppu._render_scanline_fast(20)
        replay_count = optimized.ppu.diagnostic_scanline_replays
        optimized_background = dict(optimized.ppu._fast_background_cache)
        optimized_world = dict(optimized.ppu._fast_background_world_cache)
        self.assertTrue(optimized_background)
        self.assertTrue(optimized_world)

        for console in (optimized, reference):
            console.bus.write(0x8000, 0x02)
            console.bus.write(0x8001, 0x09)
        reference.ppu.invalidate_fast_cache(preserve_mapper_viewports=True)

        self.assertEqual(
            optimized.ppu._fast_background_cache,
            optimized_background,
        )
        self.assertEqual(
            optimized.ppu._fast_background_world_cache,
            optimized_world,
        )
        self.assertEqual(
            optimized.ppu.diagnostic_mapper_background_preserves,
            1,
        )
        self.assertFalse(reference.ppu._fast_background_cache)

        # A sprite-bank write cannot affect a line with no selected sprites,
        # so its already-retained framebuffer row remains a direct replay.
        optimized.ppu._render_scanline_fast(20)
        self.assertEqual(
            optimized.ppu.diagnostic_scanline_replays,
            replay_count + 1,
        )

        optimized.ppu._render_scanline_fast(1)
        reference.ppu._render_scanline_fast(1)
        self.assertEqual(
            bytes(optimized.ppu.framebuffer[:2 * 256 * 3]),
            bytes(reference.ppu.framebuffer[:2 * 256 * 3]),
        )

    def test_mmc3_a12_filter_and_stable_irq_timestamp(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(mapper=4, prg_banks=2, chr_banks=1)
        )
        mapper = cart.mapper
        mapper.irq_latch = 1
        mapper.irq_reload = True
        mapper.irq_enabled = True

        mapper.notify_ppu_address(0x1000, 90)
        self.assertEqual(mapper.irq_counter, 1)
        mapper.notify_ppu_address(0x0000, 100)
        mapper.notify_ppu_address(0x1000, 108)
        self.assertEqual(mapper.irq_counter, 1)
        self.assertFalse(mapper.irq_pending)

        mapper.notify_ppu_address(0x0000, 109)
        mapper.notify_ppu_address(0x1000, 118)
        self.assertEqual(mapper.irq_counter, 0)
        self.assertTrue(mapper.irq_pending)
        self.assertEqual(mapper.irq_assert_cycle, 40)

        mapper.cpu_write(0xE000, 0)
        self.assertFalse(mapper.irq_pending)
        self.assertIsNone(mapper.irq_assert_cycle)

    def test_mmc3_ppudata_increment_can_clock_a12(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(mapper=4, prg_banks=2, chr_banks=1)
        )
        ppu = PPU(cart)
        mapper = cart.mapper
        mapper.irq_counter = 1
        mapper.irq_enabled = True
        mapper.last_a12 = False
        mapper.a12_low_since = 0
        ppu.v = 0x0FFF
        ppu.master_clock = 90

        ppu.cpu_read_register(0x2007)

        self.assertEqual(ppu.v, 0x1000)
        self.assertEqual(mapper.irq_counter, 0)
        self.assertTrue(mapper.irq_pending)

    def test_mmc3_ppuaddr_write_can_clock_a12(self) -> None:
        cart = Cartridge.from_bytes(
            make_ines(mapper=4, prg_banks=2, chr_banks=1)
        )
        ppu = PPU(cart)
        mapper = cart.mapper
        mapper.irq_counter = 1
        mapper.irq_enabled = True
        ppu.master_clock = 90

        ppu.cpu_write_register(0x2006, 0x10)

        self.assertEqual(ppu.t & 0x3F00, 0x1000)
        self.assertEqual(mapper.irq_counter, 0)
        self.assertTrue(mapper.irq_pending)

    def test_mmc2_switches_one_prg_bank_and_fixes_the_last_three(self) -> None:
        cart = make_mmc2_cartridge()
        self.assertEqual(cart.cpu_read(0x8000), 0)
        self.assertEqual(cart.cpu_read(0xA000), 13)
        self.assertEqual(cart.cpu_read(0xC000), 14)
        self.assertEqual(cart.cpu_read(0xE000), 15)

        cart.cpu_write(0xA123, 5)
        self.assertEqual(cart.cpu_read(0x8000), 5)
        self.assertEqual(cart.cpu_read(0xA000), 13)

    def test_mmc2_chr_latches_switch_after_the_triggering_read(self) -> None:
        cart = make_mmc2_cartridge()
        mapper = cart.mapper
        cart.cpu_write(0xB000, 1)
        cart.cpu_write(0xC000, 2)
        cart.cpu_write(0xD000, 3)
        cart.cpu_write(0xE000, 4)

        # Both latches reset to FE. Trigger bytes still come from the old bank.
        self.assertEqual(cart.ppu_read(0x0000), 2)
        self.assertEqual(cart.ppu_read(0x0FD8), 2)
        self.assertFalse(mapper.latch_0000_fe)
        self.assertEqual(cart.ppu_read(0x0000), 1)

        self.assertEqual(cart.ppu_read(0x0FE8), 1)
        self.assertTrue(mapper.latch_0000_fe)
        self.assertEqual(cart.ppu_read(0x0000), 2)

        self.assertEqual(cart.ppu_read(0x1FDF), 4)
        self.assertFalse(mapper.latch_1000_fe)
        self.assertEqual(cart.ppu_read(0x1000), 3)
        self.assertEqual(cart.ppu_read(0x1FEF), 3)
        self.assertTrue(mapper.latch_1000_fe)
        self.assertEqual(cart.ppu_read(0x1000), 4)

    def test_mmc2_lower_latch_uses_exact_addresses_and_reads_only(self) -> None:
        cart = make_mmc2_cartridge()
        mapper = cart.mapper
        cart.cpu_write(0xB000, 1)
        cart.cpu_write(0xC000, 2)

        cart.ppu_read(0x0FD9)
        self.assertTrue(mapper.latch_0000_fe)
        cart.ppu_write(0x0FD8, 0xFF)
        self.assertTrue(mapper.latch_0000_fe)
        cart.ppu_read(0x0FD8)
        self.assertFalse(mapper.latch_0000_fe)

    def test_mmc2_pattern_pair_matches_two_literal_ppu_reads(self) -> None:
        for address in (
            0x0000,
            0x0FD0,
            0x0FD1,
            0x0FE0,
            0x1000,
            0x1FD0,
            0x1FD7,
            0x1FE0,
            0x1FE7,
        ):
            with self.subTest(address=f"{address:04X}"):
                paired = make_mmc2_cartridge()
                literal = make_mmc2_cartridge()
                for cart in (paired, literal):
                    cart.cpu_write(0xB000, 1)
                    cart.cpu_write(0xC000, 2)
                    cart.cpu_write(0xD000, 3)
                    cart.cpu_write(0xE000, 4)

                pair_value = paired.mapper.ppu_read_pair(address)
                literal_value = (
                    literal.mapper.ppu_read(address),
                    literal.mapper.ppu_read(address + 8),
                )
                self.assertEqual(pair_value, literal_value)
                self.assertEqual(
                    paired.mapper.get_state(),
                    literal.mapper.get_state(),
                )

    def test_mmc2_controls_mirroring_and_preserves_four_screen(self) -> None:
        cart = make_mmc2_cartridge()
        cart.cpu_write(0xF000, 1)
        self.assertEqual(cart.mapper.mirroring, Mirroring.HORIZONTAL)
        cart.cpu_write(0xFABC, 0)
        self.assertEqual(cart.mapper.mirroring, Mirroring.VERTICAL)

        four_screen = Cartridge(
            prg_rom=bytes(0x20000),
            chr_data=bytes(0x20000),
            chr_is_ram=False,
            mapper_number=9,
            mirroring=Mirroring.FOUR_SCREEN,
        )
        four_screen.cpu_write(0xF000, 1)
        self.assertEqual(four_screen.mapper.mirroring, Mirroring.FOUR_SCREEN)

    def test_mmc2_mapper_state_round_trip_restores_every_register(self) -> None:
        cart = make_mmc2_cartridge()
        cart.cpu_write(0xA000, 7)
        cart.cpu_write(0xB000, 8)
        cart.cpu_write(0xC000, 9)
        cart.cpu_write(0xD000, 10)
        cart.cpu_write(0xE000, 11)
        cart.cpu_write(0xF000, 1)
        cart.ppu_read(0x0FD8)
        cart.ppu_read(0x1FD8)
        saved = cart.get_state()

        cart.cpu_write(0xA000, 1)
        cart.cpu_write(0xB000, 2)
        cart.cpu_write(0xF000, 0)
        cart.ppu_read(0x0FE8)
        cart.ppu_read(0x1FE8)
        cart.set_state(saved)

        mapper = cart.mapper
        self.assertEqual(mapper.prg_bank, 7)
        self.assertEqual(
            (
                mapper.chr_fd_0000,
                mapper.chr_fe_0000,
                mapper.chr_fd_1000,
                mapper.chr_fe_1000,
            ),
            (8, 9, 10, 11),
        )
        self.assertFalse(mapper.latch_0000_fe)
        self.assertFalse(mapper.latch_1000_fe)
        self.assertEqual(mapper.mirroring, Mirroring.HORIZONTAL)

    def test_mmc4_switches_16k_prg_and_reuses_exact_chr_latches(self) -> None:
        cart = make_mmc4_cartridge()
        mapper = cart.mapper
        self.assertEqual(cart.cpu_read(0x8000), 14)
        self.assertEqual(cart.cpu_read(0xC000), 15)
        cart.cpu_write(0xA000, 5)
        self.assertEqual(cart.cpu_read(0x8000), 5)
        self.assertEqual(cart.cpu_read(0xC000), 15)

        cart.cpu_write(0xB000, 1)
        cart.cpu_write(0xC000, 2)
        self.assertEqual(cart.ppu_read(0x0FD8), 2)
        self.assertFalse(mapper.latch_0000_fe)
        self.assertEqual(cart.ppu_read(0x0000), 1)

        saved = cart.get_state()
        cart.cpu_write(0xA000, 3)
        cart.ppu_read(0x0FE8)
        cart.set_state(saved)
        self.assertEqual(cart.cpu_read(0x8000), 5)
        self.assertFalse(mapper.latch_0000_fe)

    def test_color_dreams_switches_32k_prg_and_8k_chr(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x8000, 4),
            chr_data=filled_banks(0x2000, 16),
            chr_is_ram=False,
            mapper_number=11,
            submapper=1,
        )
        cart.cpu_write(0x9000, 0xB2)
        self.assertEqual(cart.cpu_read(0x8000), 2)
        self.assertEqual(cart.ppu_read(0x0000), 11)

        saved = cart.get_state()
        cart.cpu_write(0x9000, 0x10)
        cart.set_state(saved)
        self.assertEqual(cart.cpu_read(0x8000), 2)
        self.assertEqual(cart.ppu_read(0x1000), 11)

    def test_cprom_has_16k_chr_ram_and_banks_only_the_upper_half(self) -> None:
        parsed = Cartridge.from_bytes(
            make_ines(mapper=13, chr_banks=0)
        )
        self.assertEqual(len(parsed.chr_memory), 0x4000)

        cart = Cartridge(
            prg_rom=bytes(0x8000),
            chr_data=b"",
            chr_is_ram=True,
            mapper_number=13,
            submapper=1,
            chr_ram_size=0x4000,
        )
        cart.ppu_write(0x0000, 0x11)
        cart.cpu_write(0x8000, 1)
        cart.ppu_write(0x1000, 0x22)
        cart.cpu_write(0x8000, 2)
        cart.ppu_write(0x1000, 0x33)
        self.assertEqual(cart.ppu_read(0x0000), 0x11)
        self.assertEqual(cart.ppu_read(0x1000), 0x33)
        cart.cpu_write(0x8000, 1)
        self.assertEqual(cart.ppu_read(0x1000), 0x22)

    def test_mapper34_nina_registers_overlap_ram_and_bank_prg_chr(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x8000, 2),
            chr_data=filled_banks(0x1000, 16),
            chr_is_ram=False,
            mapper_number=34,
            submapper=1,
            prg_ram_size=0x2000,
        )
        self.assertTrue(cart.mapper.nina)
        self.assertEqual(cart.ppu_read(0x1000), 1)
        cart.cpu_write(0x7FFD, 1)
        cart.cpu_write(0x7FFE, 4)
        cart.cpu_write(0x7FFF, 7)
        self.assertEqual(cart.cpu_read(0x8000), 1)
        self.assertEqual(cart.ppu_read(0x0000), 4)
        self.assertEqual(cart.ppu_read(0x1000), 7)
        self.assertEqual(cart.prg_ram[0x1FFD:0x2000], bytes((1, 4, 7)))

        legacy = Cartridge.from_bytes(
            make_ines(mapper=34, prg_banks=4, chr_banks=1)
        )
        self.assertTrue(legacy.mapper.nina)
        self.assertEqual(len(legacy.prg_ram), 0x2000)

    def test_mapper34_bnrom_switches_the_complete_32k_window(self) -> None:
        prg = bytearray(filled_banks(0x8000, 4))
        # BNROM has bus conflicts. A safe bankswitch writes through a ROM byte
        # containing the same value.
        prg[0] = 2
        cart = Cartridge(
            prg_rom=bytes(prg),
            chr_data=b"",
            chr_is_ram=True,
            mapper_number=34,
            submapper=2,
            chr_ram_size=0x2000,
        )
        self.assertFalse(cart.mapper.nina)
        cart.cpu_write(0x8000, 2)
        self.assertEqual(cart.cpu_read(0x8001), 2)

    def test_camerica_switches_prg_without_conflicts_and_fire_hawk_mirroring(
        self,
    ) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x4000, 4),
            chr_data=b"",
            chr_is_ram=True,
            mapper_number=71,
            submapper=1,
            chr_ram_size=0x2000,
        )
        cart.cpu_write(0xC000, 2)
        self.assertEqual(cart.cpu_read(0x8000), 2)
        self.assertEqual(cart.cpu_read(0xC000), 3)
        cart.cpu_write(0x8000, 0x10)
        self.assertEqual(cart.mapper.mirroring, Mirroring.SINGLE_UPPER)
        cart.cpu_write(0x8000, 0)
        self.assertEqual(cart.mapper.mirroring, Mirroring.SINGLE_LOWER)

    def test_mapper87_reverses_chr_register_bit_order(self) -> None:
        cart = Cartridge(
            prg_rom=bytes(0x4000),
            chr_data=filled_banks(0x2000, 4),
            chr_is_ram=False,
            mapper_number=87,
        )
        self.assertEqual(cart.cpu_read(0x8000), cart.cpu_read(0xC000))
        cart.cpu_write(0x6000, 1)
        self.assertEqual(cart.ppu_read(0), 2)
        cart.cpu_write(0x6000, 2)
        self.assertEqual(cart.ppu_read(0), 1)

    def test_un1rom_uses_shifted_uxrom_bank_bits(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x4000, 8),
            chr_data=b"",
            chr_is_ram=True,
            mapper_number=94,
            submapper=1,
            chr_ram_size=0x2000,
        )
        cart.cpu_write(0x8000, 5 << 2)
        self.assertEqual(cart.cpu_read(0x8000), 5)
        self.assertEqual(cart.cpu_read(0xC000), 7)

    def test_jaleco_jf11_moves_gxrom_register_to_6000(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x8000, 4),
            chr_data=filled_banks(0x2000, 4),
            chr_is_ram=False,
            mapper_number=140,
        )
        cart.cpu_write(0x6000, 0x23)
        self.assertEqual(cart.cpu_read(0x8000), 2)
        self.assertEqual(cart.ppu_read(0), 3)

    def test_mapper180_fixes_first_bank_and_switches_upper_16k(self) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x4000, 8),
            chr_data=b"",
            chr_is_ram=True,
            mapper_number=180,
            submapper=1,
            chr_ram_size=0x2000,
        )
        cart.cpu_write(0x8000, 3)
        self.assertEqual(cart.cpu_read(0x8000), 0)
        self.assertEqual(cart.cpu_read(0xC000), 3)

    def test_mapper_register_below_8000_invalidates_only_visible_changes(
        self,
    ) -> None:
        cart = Cartridge(
            prg_rom=filled_banks(0x8000, 2),
            chr_data=filled_banks(0x1000, 16),
            chr_is_ram=False,
            mapper_number=34,
            submapper=1,
            prg_ram_size=0x2000,
        )
        console = Console(cart, sample_rate=0, fast_ppu=True)
        invalidations = 0
        invalidate = console.ppu.invalidate_fast_cache

        def counted_invalidate() -> None:
            nonlocal invalidations
            invalidations += 1
            invalidate()

        console.ppu.invalidate_fast_cache = counted_invalidate
        console.bus.write(0x6000, 0x55)
        self.assertEqual(invalidations, 0)
        console.bus.write(0x7FFD, 1)
        self.assertEqual(console.bus.read(0x8000), 1)
        self.assertEqual(invalidations, 0)
        console.bus.write(0x7FFE, 3)
        self.assertEqual(invalidations, 1)

    def test_mmc2_fast_ppu_replays_cached_latch_side_effects(self) -> None:
        cart = make_mmc2_cartridge()
        mapper = cart.mapper
        cart.cpu_write(0xB000, 1)
        cart.cpu_write(0xC000, 2)
        # The first tile ($FD) is blank in FE bank 2 and trips the latch.
        # Tile zero in FD bank 1 is opaque, so the following tile must differ.
        cart.chr_memory[0x1000:0x2000] = bytes(0x1000)
        cart.chr_memory[0x1000] = 0xFF
        cart.chr_memory[0x2000:0x3000] = bytes(0x1000)

        ppu = PPU(cart, fast_mode=True)
        ppu.mask = 0x0A
        ppu.palette_ram[0] = 0x0F
        ppu.palette_ram[1] = 0x30
        ppu.nametable_ram[0] = 0xFD
        ppu.nametable_ram[1] = 0x00
        pair_reads = 0
        read_pair = ppu._mapper_ppu_read_pair

        def counted_read_pair(address: int) -> tuple[int, int]:
            nonlocal pair_reads
            pair_reads += 1
            return read_pair(address)

        ppu._mapper_ppu_read_pair = counted_read_pair
        ppu._render_scanline_fast(0)

        self.assertFalse(mapper.latch_0000_fe)
        self.assertNotEqual(
            bytes(ppu.framebuffer[0:3]),
            bytes(ppu.framebuffer[8 * 3:9 * 3]),
        )
        self.assertFalse(ppu._fast_background_cache)
        self.assertFalse(ppu._fast_background_world_cache)
        self.assertFalse(ppu._fast_scanline_cache)

        first_pixels = bytes(ppu.framebuffer[:256 * 3])
        mapper.set_ppu_latch_state((True, mapper.latch_1000_fe))
        ppu._render_scanline_fast(0)
        self.assertTrue(ppu._fast_latched_background_cache)
        self.assertTrue(ppu._fast_latched_scanline_cache)
        first_pair_reads = pair_reads

        mapper.set_ppu_latch_state((True, mapper.latch_1000_fe))
        ppu.framebuffer[:256 * 3] = bytes(256 * 3)
        ppu._render_scanline_fast(0)
        self.assertEqual(bytes(ppu.framebuffer[:256 * 3]), first_pixels)
        self.assertFalse(mapper.latch_0000_fe)
        self.assertEqual(pair_reads, first_pair_reads)

        # A register change and fresh FE trigger must affect the next render;
        # the production bus invalidates these caches after the mapper write.
        cart.cpu_write(0xB000, 3)
        ppu.invalidate_fast_cache()
        mapper.set_ppu_latch_state((True, mapper.latch_1000_fe))
        ppu._render_scanline_fast(0)
        self.assertEqual(
            bytes(ppu.framebuffer[0:3]),
            bytes(ppu.framebuffer[8 * 3:9 * 3]),
        )

    def test_mmc2_fast_ppu_reuses_tile_run_across_fine_scroll(self) -> None:
        cart = make_mmc2_cartridge()
        mapper = cart.mapper
        cart.cpu_write(0xB000, 1)
        cart.cpu_write(0xC000, 2)
        cart.chr_memory[0x1000:0x2000] = bytes(0x1000)
        cart.chr_memory[0x1000] = 0xFF
        cart.chr_memory[0x2000:0x3000] = bytes(0x1000)

        ppu = PPU(cart, fast_mode=True)
        ppu.mask = 0x0A
        ppu.palette_ram[0] = 0x0F
        ppu.palette_ram[1] = 0x30
        ppu.nametable_ram[0] = 0xFD
        ppu.nametable_ram[1] = 0x00
        mapper.set_ppu_latch_state((True, True))
        ppu._render_scanline_fast(0)
        unscrolled = bytes(ppu.framebuffer[:256 * 3])

        self.assertFalse(mapper.latch_0000_fe)
        self.assertEqual(len(ppu._fast_latched_tile_run_cache), 1)

        mapper.set_ppu_latch_state((True, True))
        ppu.fine_x = 1
        ppu._render_scanline_fast(0)
        scrolled = bytes(ppu.framebuffer[:256 * 3])

        self.assertFalse(mapper.latch_0000_fe)
        self.assertEqual(len(ppu._fast_latched_tile_run_cache), 1)
        self.assertEqual(scrolled[:255 * 3], unscrolled[3:])

    def test_mmc2_retained_line_survives_unrelated_nametable_write(
        self,
    ) -> None:
        cart = make_mmc2_cartridge()
        ppu = PPU(cart, fast_mode=True)
        ppu.mask = 0x0A
        ppu.palette_ram[0] = 0x0F
        ppu.v = 0

        ppu._render_scanline_fast(12)
        ppu._render_scanline_fast(12)
        self.assertEqual(ppu.diagnostic_scanline_replays, 1)

        # Tile row five is not fetched by the retained row-zero viewport.
        ppu.write_memory(0x2000 + 5 * 32, 1)
        ppu._render_scanline_fast(12)
        self.assertEqual(ppu.diagnostic_scanline_replays, 2)

        # A write in row zero changes a real dependency and must compose.
        ppu.write_memory(0x2000, 1)
        ppu._render_scanline_fast(12)
        self.assertEqual(ppu.diagnostic_scanline_replays, 2)

    def test_unsupported_mapper_is_explicit(self) -> None:
        with self.assertRaises(MapperError):
            Cartridge.from_bytes(make_ines(mapper=5))


if __name__ == "__main__":
    unittest.main()
