"""Ricoh 2C02 picture processing unit."""

from __future__ import annotations

from collections import OrderedDict, deque

from .cartridge import Cartridge
from .constants import Mirroring, NES_PALETTE, PPU_HEIGHT, PPU_WIDTH


FAST_SCANLINE_CACHE_LIMIT = 2048
# A full horizontally scrolling MMC2 scene has 32 coarse-X positions across
# 240 rows. The compact key below lets one complete cycle fit here, replacing
# sustained 1 KiB row churn with stable reuse after the first scroll lap.
FAST_LATCHED_TILE_RUN_CACHE_LIMIT = 8192
FAST_LATCHED_FETCH_PLAN_CACHE_LIMIT = 8192
# Mapper 4 scenes commonly rotate through a small set of CHR/mirroring maps.
# Retaining two complete frames of compact visible tile runs avoids rebuilding
# the same transition states while keeping the cache near two MiB.
FAST_VIEWPORT_TILE_RUN_CACHE_LIMIT = 2048
# A side-scroller touches a compact set of 512-pixel world rows repeatedly and
# adds stale row-version keys as level data streams into CIRAM. A FIFO reset at
# 1,024 entries discarded every hot row at once and made one periodic MMC3
# frame decode all 15,360 background tiles again. A 2,048-entry LRU retains
# the active rows for several recurring bank maps while evicting stale content
# incrementally. The byte payload is bounded to roughly four MiB.
FAST_BACKGROUND_WORLD_CACHE_LIMIT = 2048
# Building a 512-pixel world row reads 64 tiles, roughly twice the work needed
# for the visible 33-tile viewport. Populate one third on a recurring state's
# first appearance and every remaining miss on its second. The first pass's
# exact 33-tile fallbacks and second pass's wider builds have nearly equal work,
# preventing one cold all-row frame without paying for a third appearance.
FAST_BACKGROUND_WORLD_INITIAL_ADMISSION_DIVISOR = 3
# Sprite pattern rows are only sixteen bits plus one horizontal-flip bit.  A
# game can move OAM every frame without changing those rows, so decode the
# transparent pixels once instead of repeating eight Python shifts for every
# sprite on every recomposed scanline.
FAST_SPRITE_PATTERN_CACHE_LIMIT = 4096
# The 2C02 CPU-interface latch is dynamic. Individual hardware bits vary with
# chip and temperature; 600 ms is the conservative documented compatibility
# value used by the public open-bus diagnostic.
OPEN_BUS_DECAY_DOTS = 3_221_591
# Reads of $2002 can shorten the PPU's active-low NMI pulse enough that the
# CPU does not recognize it on either of the following two PPU dots.
NMI_SUPPRESSION_DOTS = 1
# PPUMASK's rendering gates do not reach the pixel pipeline instantaneously.
# A two-dot propagation delay matches the 2C02's late pre-render enable and
# disable behavior around the odd-frame shortened scanline.
RENDERING_MASK_DELAY_DOTS = 2
EMPTY_OPAQUE_SCANLINE = bytes(PPU_WIDTH)
EMPTY_OPAQUE_LEFT_EDGE = bytes(8)


class PPU:
    def __init__(self, cartridge: Cartridge, *, fast_mode: bool = False) -> None:
        self.cartridge = cartridge
        self.mapper = cartridge.mapper
        self._mapper_ppu_read = self.mapper.ppu_read
        self._mapper_ppu_read_pair = self.mapper.ppu_read_pair
        self._mapper_ppu_pattern_cache_token = (
            self.mapper.ppu_pattern_cache_token
        )
        latch_state = getattr(self.mapper, "ppu_latch_state", None)
        set_latch_state = getattr(self.mapper, "set_ppu_latch_state", None)
        if callable(latch_state) and callable(set_latch_state):
            self._mapper_latch_state = latch_state
            self._mapper_set_latch_state = set_latch_state
        else:
            self._mapper_latch_state = None
            self._mapper_set_latch_state = None
        begin_latched_pairs = getattr(
            self.mapper,
            "begin_fast_latched_pairs",
            None,
        )
        end_latched_pairs = getattr(
            self.mapper,
            "end_fast_latched_pairs",
            None,
        )
        if callable(begin_latched_pairs) and callable(end_latched_pairs):
            self._mapper_begin_latched_pairs = begin_latched_pairs
            self._mapper_end_latched_pairs = end_latched_pairs
        else:
            self._mapper_begin_latched_pairs = None
            self._mapper_end_latched_pairs = None
        self._ppu_reads_have_side_effects = bool(
            self.mapper.ppu_reads_have_side_effects
        )
        self._mmc3_mapper = (
            self.mapper if cartridge.mapper_number == 4 else None
        )
        self._direct_chr = (
            cartridge.chr_memory if cartridge.mapper_number == 0 else None
        )
        self.fast_mode = bool(fast_mode)
        self._disable_fast_visible_line_batch = False
        self.nametable_ram = bytearray(0x1000)
        self.palette_ram = bytearray(0x20)
        self._fast_palette_signature = bytes(self.palette_ram)
        self._fast_palette_parts_source = self._fast_palette_signature
        self._fast_background_palette_signature = (
            self._fast_palette_signature[:0x10]
        )
        self._fast_sprite_palette_signature = (
            self._fast_palette_signature[0x10:]
        )
        self.oam = bytearray(0x100)
        self.framebuffer = bytearray(PPU_WIDTH * PPU_HEIGHT * 3)

        self.ctrl = 0
        self.mask = 0
        self._rendering_active = False
        self._rendering_target = False
        self._rendering_change_clock: int | None = None
        self.status = 0
        self.oam_addr = 0
        self.open_bus = 0
        self._open_bus_deadlines = [0] * 8
        self.read_buffer = 0

        # Loopy scrolling registers: current VRAM address, temporary address,
        # fine-X scroll, and first/second-write latch.
        self.v = 0
        self.t = 0
        self.fine_x = 0
        self.write_toggle = False

        self.bg_pattern_low = 0
        self.bg_pattern_high = 0
        self.bg_attribute_low = 0
        self.bg_attribute_high = 0
        self.next_tile_id = 0
        self.next_tile_attribute = 0
        self.next_tile_low = 0
        self.next_tile_high = 0

        self.sprites: list[tuple[int, int, int, int, int]] = []
        self.scanline = 261
        self.cycle = 0
        self.master_clock = 0
        self.odd_frame = False
        self.frame_complete = False
        self.frame_number = 0
        self.nmi_line = False
        self.nmi_pending = False
        self.nmi_pending_clock: int | None = None
        self.nmi_pending_cancellable = False
        self.suppress_vblank = False
        self._fast_row_cache: dict[tuple, tuple[bytes, bytes]] = {}
        self._fast_row_cache_order: deque[tuple] = deque()
        self._fast_background_cache: dict[
            tuple[int, ...], tuple[bytes, bytes]
        ] = {}
        self._fast_background_cache_order: deque[tuple[int, ...]] = deque()
        self._fast_background_frame_signature: tuple[int, ...] | None = None
        self._fast_background_frame_repeats = 0
        self._fast_background_cache_active = False
        self._fast_latched_background_cache: dict[
            tuple, tuple[bytes, bytes, bool, bool]
        ] = {}
        self._fast_latched_background_cache_order: deque[tuple] = deque()
        self._fast_latched_scanline_cache: dict[
            tuple, tuple[bytes, int, bool, bool]
        ] = {}
        self._fast_latched_fetch_plan_cache: dict[tuple, bytes] = {}
        self._fast_latched_fetch_plan_cache_order: deque[tuple] = deque()
        self._fast_viewport_tile_run_cache: dict[
            tuple, tuple[bytes, bytes]
        ] = {}
        self._fast_viewport_tile_run_cache_order: deque[tuple] = deque()
        self._fast_latched_tile_run_cache: dict[
            tuple, tuple[bytes, bytes, int, int, bool, bool]
        ] = {}
        self._fast_latched_tile_run_cache_order: deque[tuple] = deque()
        self._fast_latched_frame_signature: tuple | None = None
        self._fast_latched_frame_repeats = 0
        self._fast_latched_cache_active = False
        self._fast_background_world_cache: OrderedDict[
            tuple, tuple[bytes, bytes]
        ] = OrderedDict()
        self._fast_world_admission_states: dict[tuple, tuple[int, int]] = {}
        self._fast_world_generation = 0
        # MMC2 games commonly update only a handful of nametable rows each
        # frame. A single global generation made those writes invalidate all
        # 240 retained scanlines, even though most rows still referenced the
        # same tiles. Track tile and attribute rows independently so the
        # retained framebuffer can keep every unaffected scanline.
        self._fast_nametable_row_versions = [0] * (4 * 30)
        self._fast_attribute_row_versions = [0] * (4 * 8)
        self._fast_nametable_generation = 0
        self._fast_latched_replay_signature_cache: dict[tuple, tuple] = {}
        self._fast_replay_fallback_generation = 0
        self._fast_chr_generation = 0
        self._fast_world_frame_generation = -1
        self._fast_world_frame_repeats = 0
        self._fast_world_cache_active = False
        self._fast_world_mapper_tokens_seen: set[tuple] = set()
        self._fast_world_recurring_mapper_state = False
        self._fast_background_mapper_token = (
            self._background_mapper_dependency()
        )
        self._fast_sprite_mapper_token = self._sprite_mapper_dependency()
        self._fast_background_mapper_generation = 0
        self._fast_sprite_mapper_generation = 0
        self._fast_scanline_cache: dict[tuple[int, ...], tuple[bytes, int]] = {}
        self._fast_sprite_generation = 0
        self._fast_composed_frame_signature: tuple[int, ...] | None = None
        self._fast_composed_frame_repeats = 0
        self._fast_composed_cache_active = False
        self._fast_palette_cache: dict[
            tuple[bytes, int], tuple[tuple[int, int, int], ...]
        ] = {}
        self._fast_sprite_lines: tuple[
            tuple[tuple[int, int, int, int], ...], ...
        ] | None = None
        self._fast_sprite_pattern_cache: dict[int, tuple[int, ...]] = {}
        self._fast_sprite_overflow_cycles = [-1] * PPU_HEIGHT
        self._fast_sprite_overflow_candidates: bytes | None = None
        self._fast_sprite_hit_plan_key: tuple[int, int] | None = None
        self._fast_sprite_hit_cycle = 0
        # The framebuffer is retained between frames.  Keep the complete
        # state that produced each line so a matching line in the following
        # frame can remain in place instead of being copied out of a cache and
        # composed again.  Mapper latch side effects are replayed separately
        # at the original dot-256 boundary.
        self._fast_frame_replay_lines: list[tuple | None] = [
            None
        ] * PPU_HEIGHT
        self.diagnostic_scanline_replays = 0
        self._accurate_sprite_overflow_cycle = 0
        # Repeated $2007 writes while rendering is disabled must not clear
        # the same already-empty presentation caches thousands of times.
        self._fast_cache_invalidation_level = 0
        self.diagnostic_scanline_renders = 0
        self.diagnostic_status_probes = 0
        self.diagnostic_mapper_invalidations = 0
        self.diagnostic_mapper_background_preserves = 0
        self.diagnostic_palette_background_preserves = 0
        self.diagnostic_cache_invalidations = 0
        self.diagnostic_cache_coalesces = 0
        self.diagnostic_deferred_data_writes = 0
        self.diagnostic_world_cache_misses = 0
        self.diagnostic_world_cache_evictions = 0
        self.diagnostic_world_cache_admission_deferrals = 0

    @property
    def rendering_enabled(self) -> bool:
        mask_target = bool(self.mask & 0x18)
        if (
            self._rendering_change_clock is None
            and mask_target != self._rendering_target
        ):
            # Tests and debugger tooling may assign mask directly.
            self._rendering_target = mask_target
            self._rendering_active = mask_target
        self._apply_rendering_change()
        return self._rendering_active

    def _apply_rendering_change(self) -> None:
        deadline = self._rendering_change_clock
        if deadline is not None and self.master_clock >= deadline:
            self._rendering_active = self._rendering_target
            self._rendering_change_clock = None

    def dots_to_rendering_change(self) -> int | None:
        deadline = self._rendering_change_clock
        if deadline is None:
            return None
        return max(0, deadline - self.master_clock)

    def _line_length(self, prerender: bool, rendering: bool) -> int:
        if (
            prerender
            and self.odd_frame
            and rendering
            and self.cycle < 340
        ):
            return 340
        return 341

    def reset(self) -> None:
        self.ctrl = 0
        self._fast_background_mapper_token = (
            self._background_mapper_dependency()
        )
        self._fast_sprite_mapper_token = self._sprite_mapper_dependency()
        self._fast_background_mapper_generation = 0
        self._fast_sprite_mapper_generation = 0
        self.mask = 0
        self._rendering_active = False
        self._rendering_target = False
        self._rendering_change_clock = None
        self.status = 0
        self.oam_addr = 0
        self.open_bus = 0
        self._open_bus_deadlines = [0] * 8
        self.read_buffer = 0
        self.v = self.t = self.fine_x = 0
        self.write_toggle = False
        self.bg_pattern_low = self.bg_pattern_high = 0
        self.bg_attribute_low = self.bg_attribute_high = 0
        self.scanline = 261
        self.cycle = 0
        self.master_clock = 0
        self.odd_frame = False
        self.frame_complete = False
        self.frame_number = 0
        self.nmi_line = False
        self.nmi_pending = False
        self.nmi_pending_clock = None
        self.nmi_pending_cancellable = False
        self.suppress_vblank = False
        self._fast_row_cache.clear()
        self._fast_row_cache_order.clear()
        self._fast_background_cache.clear()
        self._fast_background_cache_order.clear()
        self._fast_background_frame_signature = None
        self._fast_background_frame_repeats = 0
        self._fast_background_cache_active = False
        self._fast_latched_background_cache.clear()
        self._fast_latched_background_cache_order.clear()
        self._fast_latched_scanline_cache.clear()
        self._fast_latched_fetch_plan_cache.clear()
        self._fast_latched_fetch_plan_cache_order.clear()
        self._fast_viewport_tile_run_cache.clear()
        self._fast_viewport_tile_run_cache_order.clear()
        self._fast_latched_tile_run_cache.clear()
        self._fast_latched_tile_run_cache_order.clear()
        self._fast_latched_frame_signature = None
        self._fast_latched_frame_repeats = 0
        self._fast_latched_cache_active = False
        self._fast_background_world_cache.clear()
        self._fast_world_admission_states.clear()
        self._fast_world_generation = 0
        self._fast_nametable_row_versions = [0] * (4 * 30)
        self._fast_attribute_row_versions = [0] * (4 * 8)
        self._fast_nametable_generation = 0
        self._fast_latched_replay_signature_cache.clear()
        self._fast_replay_fallback_generation = 0
        self._fast_chr_generation = 0
        self._fast_world_frame_generation = -1
        self._fast_world_frame_repeats = 0
        self._fast_world_cache_active = False
        self._fast_world_mapper_tokens_seen.clear()
        self._fast_world_recurring_mapper_state = False
        self._fast_scanline_cache.clear()
        self._fast_sprite_generation = 0
        self._fast_composed_frame_signature = None
        self._fast_composed_frame_repeats = 0
        self._fast_composed_cache_active = False
        self._fast_palette_cache.clear()
        self._fast_sprite_lines = None
        self._fast_sprite_overflow_cycles = [-1] * PPU_HEIGHT
        self._fast_sprite_overflow_candidates = None
        self._fast_sprite_hit_plan_key = None
        self._fast_sprite_hit_cycle = 0
        self._fast_frame_replay_lines = [None] * PPU_HEIGHT
        self.diagnostic_scanline_replays = 0
        self._accurate_sprite_overflow_cycle = 0
        self._fast_cache_invalidation_level = 0
        self.diagnostic_scanline_renders = 0
        self.diagnostic_status_probes = 0
        self.diagnostic_mapper_invalidations = 0
        self.diagnostic_mapper_background_preserves = 0
        self.diagnostic_palette_background_preserves = 0
        self.diagnostic_cache_invalidations = 0
        self.diagnostic_cache_coalesces = 0
        self.diagnostic_deferred_data_writes = 0
        self.diagnostic_world_cache_misses = 0
        self.diagnostic_world_cache_evictions = 0
        self.diagnostic_world_cache_admission_deferrals = 0

    def invalidate_fast_cache(
        self,
        *,
        preserve_mapper_viewports: bool = False,
        replay_content_tracked: bool = False,
    ) -> None:
        """Discard presentation-only data after render-visible memory changes."""
        if not replay_content_tracked:
            self._fast_replay_fallback_generation += 1
        requested_level = 1 if preserve_mapper_viewports else 2
        already_invalid = (
            self._fast_cache_invalidation_level >= requested_level
        )
        if already_invalid:
            self.diagnostic_cache_coalesces += 1
            # Status planning remains timing-sensitive even when the
            # presentation caches were already discarded.
            sprite_end_cycle = min(256, self.oam[3] + 8)
            if self.cycle < sprite_end_cycle:
                self._fast_sprite_hit_plan_key = None
                self._fast_sprite_hit_cycle = 0
            return
        self._fast_cache_invalidation_level = requested_level
        self.diagnostic_cache_invalidations += 1
        self._fast_background_cache.clear()
        self._fast_background_cache_order.clear()
        self._fast_background_frame_signature = None
        self._fast_background_frame_repeats = 0
        self._fast_background_cache_active = False
        self._fast_latched_background_cache.clear()
        self._fast_latched_background_cache_order.clear()
        self._fast_latched_scanline_cache.clear()
        if not preserve_mapper_viewports:
            self._fast_latched_fetch_plan_cache.clear()
            self._fast_latched_fetch_plan_cache_order.clear()
            self._fast_viewport_tile_run_cache.clear()
            self._fast_viewport_tile_run_cache_order.clear()
        # Ordered latch-mapper tile runs are content-addressed by the complete
        # fetch plan, palette, and CHR bank bases. Keep those immutable rows
        # across unrelated nametable/palette invalidations so recurring scene
        # states can reuse them. CHR-RAM writes clear the cache explicitly.
        self._fast_latched_frame_signature = None
        self._fast_latched_frame_repeats = 0
        self._fast_latched_cache_active = False
        if not preserve_mapper_viewports:
            self._fast_background_world_cache.clear()
        if not replay_content_tracked:
            self._fast_world_generation += 1
            self._fast_world_cache_active = False
        self._fast_scanline_cache.clear()
        self._fast_composed_frame_signature = None
        self._fast_composed_frame_repeats = 0
        self._fast_composed_cache_active = False
        # A mapper change cannot retroactively create a sprite-zero hit after
        # all eight sprite pixels have passed. Retain that status decision
        # while still invalidating the speculative framebuffer line.
        sprite_end_cycle = min(256, self.oam[3] + 8)
        if self.cycle < sprite_end_cycle:
            self._fast_sprite_hit_plan_key = None
            self._fast_sprite_hit_cycle = 0

    def invalidate_fast_mapper_cache(self) -> None:
        """Invalidate only the MMC3 pixels affected by its new CHR map."""
        self.diagnostic_mapper_invalidations += 1
        token = self._background_mapper_dependency()
        sprite_token = self._sprite_mapper_dependency()
        if token == self._fast_background_mapper_token:
            # MMC3 independently maps eight 1 KiB CHR slots. A sprite-table
            # bank change cannot alter background pixels, yet clearing the
            # background used to rebuild up to 7,920 tile rows on the next
            # scrolling frame. Retain both viewport and 512-pixel world rows;
            # sprite composition remains invalid because its full mapper token
            # is part of each retained-frame key.
            self.diagnostic_mapper_background_preserves += 1
            if sprite_token != self._fast_sprite_mapper_token:
                self._fast_sprite_mapper_token = sprite_token
                self._fast_sprite_mapper_generation += 1
                self.invalidate_fast_sprite_pattern_cache()
            return
        self._fast_background_mapper_token = token
        self._fast_background_mapper_generation += 1
        if sprite_token != self._fast_sprite_mapper_token:
            self._fast_sprite_mapper_generation += 1
        self._fast_sprite_mapper_token = sprite_token
        recurring = (
            token is not None
            and token in self._fast_world_mapper_tokens_seen
        )
        if token is not None:
            if len(self._fast_world_mapper_tokens_seen) >= 128:
                self._fast_world_mapper_tokens_seen.clear()
            self._fast_world_mapper_tokens_seen.add(token)
        self.invalidate_fast_cache(preserve_mapper_viewports=True)
        self._fast_world_recurring_mapper_state = recurring

    def _background_mapper_dependency(self) -> tuple:
        """Return every mapper input used by the current background table."""
        pattern_base = 0x1000 if self.ctrl & 0x10 else 0
        return (
            pattern_base,
            self.mapper.mirroring,
            self._mapper_ppu_pattern_cache_token(pattern_base),
        )

    def _sprite_mapper_dependency(self) -> tuple:
        """Return the CHR mapping reachable by the current sprite mode."""
        if self.ctrl & 0x20:
            return (
                16,
                self._mapper_ppu_pattern_cache_token(0),
                self._mapper_ppu_pattern_cache_token(0x1000),
            )
        pattern_base = 0x1000 if self.ctrl & 0x08 else 0
        return (
            8,
            pattern_base,
            self._mapper_ppu_pattern_cache_token(pattern_base),
        )

    def invalidate_fast_sprite_cache(self) -> None:
        """Discard composed lines while retaining unchanged backgrounds."""
        self._fast_scanline_cache.clear()
        self._fast_latched_scanline_cache.clear()
        self._fast_sprite_generation += 1
        self._fast_sprite_lines = None
        self._fast_sprite_overflow_cycles = [-1] * PPU_HEIGHT
        self._fast_sprite_overflow_candidates = None
        self._fast_sprite_hit_plan_key = None
        self._fast_sprite_hit_cycle = 0

    def invalidate_fast_sprite_pattern_cache(self) -> None:
        """Discard sprite pixels after CHR changes while retaining OAM plans."""
        # A mapper write changes pattern bytes, not sprite positions, overflow,
        # or tile addresses. Keep the already-indexed OAM scanlines and the
        # content-addressed raw-pattern decoder; only composed pixels and the
        # sprite-zero overlap plan depend on the newly selected CHR bank.
        self._fast_scanline_cache.clear()
        self._fast_latched_scanline_cache.clear()
        self._fast_sprite_hit_plan_key = None
        self._fast_sprite_hit_cycle = 0

    def invalidate_fast_sprite_palette_cache(self) -> None:
        """Discard colors without invalidating background or hit timing."""
        # Opaque sprite coverage, priority, and sprite-zero timing do not
        # depend on RGB palette values. Retained scanline keys carry the
        # sprite half of palette RAM only on lines that select a sprite.
        self.diagnostic_palette_background_preserves += 1
        self._fast_scanline_cache.clear()
        self._fast_latched_scanline_cache.clear()

    def invalidate_fast_palette_cache(self, index: int) -> None:
        """Invalidate only the pixel layer reachable by a palette write."""
        if index >= 0x10:
            self.invalidate_fast_sprite_palette_cache()
            return
        self.invalidate_fast_cache(
            preserve_mapper_viewports=True,
            replay_content_tracked=True,
        )

    def _mark_fast_nametable_write(self, index: int) -> None:
        """Advance the smallest retained-line dependency touched by a write."""
        self._fast_nametable_generation += 1
        self._fast_latched_replay_signature_cache.clear()
        physical_table, offset = divmod(int(index), 0x400)
        if offset < 0x3C0:
            row = offset >> 5
            if row < 30:
                position = physical_table * 30 + row
                self._fast_nametable_row_versions[position] += 1
            return
        attribute_row = (offset - 0x3C0) >> 3
        if attribute_row < 8:
            position = physical_table * 8 + attribute_row
            self._fast_attribute_row_versions[position] += 1

    def _fast_nametable_replay_signature(
        self,
        start_v: int,
        mirroring: Mirroring | None = None,
    ) -> tuple:
        """Identify only the physical nametable rows fetched by one scanline."""
        if mirroring is None:
            mirroring = self.mapper.mirroring
        cache_key = (start_v, mirroring)
        cached = self._fast_latched_replay_signature_cache.get(cache_key)
        if cached is not None:
            return cached
        coarse_y = (start_v >> 5) & 0x1F
        base_nt_x = (start_v >> 10) & 1
        base_nt_y = (start_v >> 11) & 1
        effective_nt_y = (base_nt_y + coarse_y // 30) & 1
        tile_y = coarse_y % 30
        if mirroring == Mirroring.VERTICAL:
            physical_tables = (0, 1, 0, 1)
        elif mirroring == Mirroring.HORIZONTAL:
            physical_tables = (0, 0, 1, 1)
        elif mirroring == Mirroring.SINGLE_LOWER:
            physical_tables = (0, 0, 0, 0)
        elif mirroring == Mirroring.SINGLE_UPPER:
            physical_tables = (1, 1, 1, 1)
        else:
            physical_tables = (0, 1, 2, 3)

        # The ordered MMC2 renderer fetches 33 tiles. Even with fine X zero,
        # the final offscreen fetch can change a latch, so both horizontal
        # nametables are dependencies of the retained line.
        first_table = (effective_nt_y << 1) | base_nt_x
        second_table = (effective_nt_y << 1) | (base_nt_x ^ 1)
        first_physical = physical_tables[first_table]
        second_physical = physical_tables[second_table]
        attribute_y = tile_y >> 2
        row_versions = self._fast_nametable_row_versions
        attribute_versions = self._fast_attribute_row_versions
        first_row_version = row_versions[first_physical * 30 + tile_y]
        second_row_version = row_versions[second_physical * 30 + tile_y]
        first_attribute_version = attribute_versions[
            first_physical * 8 + attribute_y
        ]
        second_attribute_version = attribute_versions[
            second_physical * 8 + attribute_y
        ]
        if (
            first_row_version
            or second_row_version
            or first_attribute_version
            or second_attribute_version
        ):
            nametable_signature = (
                mirroring,
                first_row_version,
                second_row_version,
                first_attribute_version,
                second_attribute_version,
            )
        else:
            # Match the zero-write fast path so the first sparse update keeps
            # every retained scanline whose physical tile/attribute rows are
            # still untouched.
            nametable_signature = ()
        if len(self._fast_latched_replay_signature_cache) >= 2048:
            self._fast_latched_replay_signature_cache.clear()
        self._fast_latched_replay_signature_cache[cache_key] = (
            nametable_signature
        )
        return nametable_signature

    def _fast_latched_replay_world_signature(
        self,
        start_v: int,
    ) -> tuple:
        """Return complete content identity for one retained scanline."""
        return (
            self._fast_replay_fallback_generation,
            self._fast_chr_generation,
            (
                self._fast_nametable_replay_signature(start_v)
                if self._fast_nametable_generation
                else ()
            ),
        )

    def _nametable_index(self, address: int) -> int:
        relative = (address - 0x2000) & 0x0FFF
        table, offset = divmod(relative, 0x400)
        mirroring = self.cartridge.mapper.mirroring
        if mirroring == Mirroring.VERTICAL:
            physical = table & 1
        elif mirroring == Mirroring.HORIZONTAL:
            physical = table >> 1
        elif mirroring == Mirroring.SINGLE_LOWER:
            physical = 0
        elif mirroring == Mirroring.SINGLE_UPPER:
            physical = 1
        else:
            physical = table
        return physical * 0x400 + offset

    @staticmethod
    def _palette_index(address: int) -> int:
        index = address & 0x1F
        if index in (0x10, 0x14, 0x18, 0x1C):
            index -= 0x10
        return index

    def read_memory(self, address: int) -> int:
        address &= 0x3FFF
        if address < 0x2000:
            return self.cartridge.ppu_read(address, self.master_clock)
        self.cartridge.mapper.notify_ppu_address(address, self.master_clock)
        if address < 0x3F00:
            return self.nametable_ram[self._nametable_index(address)]
        return self.palette_ram[self._palette_index(address)] & 0x3F

    def write_memory(self, address: int, value: int) -> None:
        address &= 0x3FFF
        value &= 0xFF
        if address < 0x2000:
            self.cartridge.ppu_write(address, value, self.master_clock)
            if self.cartridge.chr_is_ram:
                self._fast_latched_tile_run_cache.clear()
                self._fast_latched_tile_run_cache_order.clear()
                self._fast_chr_generation += 1
                self._fast_latched_replay_signature_cache.clear()
            self.invalidate_fast_cache(
                replay_content_tracked=self.cartridge.chr_is_ram,
            )
            return
        self.cartridge.mapper.notify_ppu_address(address, self.master_clock)
        if address < 0x3F00:
            index = self._nametable_index(address)
            if self.nametable_ram[index] != value:
                self.nametable_ram[index] = value
                self._mark_fast_nametable_write(index)
                self.invalidate_fast_cache(
                    preserve_mapper_viewports=True,
                    replay_content_tracked=True,
                )
        else:
            index = self._palette_index(address)
            palette_value = value & 0x3F
            if self.palette_ram[index] != palette_value:
                self.palette_ram[index] = palette_value
                self.invalidate_fast_palette_cache(index)

    def _update_nmi(
        self,
        event_clock: int | None = None,
        *,
        cancellable: bool = True,
    ) -> None:
        new_line = bool((self.ctrl & 0x80) and (self.status & 0x80))
        clock = self.master_clock if event_clock is None else int(event_clock)
        if new_line and not self.nmi_line:
            self.nmi_pending = True
            self.nmi_pending_clock = clock
            self.nmi_pending_cancellable = bool(cancellable)
        elif (
            not new_line
            and self.nmi_line
            and self.nmi_pending
            and self.nmi_pending_cancellable
            and self.nmi_pending_clock is not None
            and 0 <= clock - self.nmi_pending_clock <= (
                NMI_SUPPRESSION_DOTS + int(not self.fast_mode)
            )
        ):
            self.nmi_pending = False
            self.nmi_pending_clock = None
            self.nmi_pending_cancellable = False
        self.nmi_line = new_line

    def poll_nmi(self) -> bool:
        pending = self.nmi_pending
        self.nmi_pending = False
        self.nmi_pending_clock = None
        self.nmi_pending_cancellable = False
        return pending

    def take_nmi_event(self) -> int | None:
        """Consume a pending edge and return its PPU master-clock time."""
        if not self.nmi_pending:
            return None
        clock = (
            self.master_clock
            if self.nmi_pending_clock is None
            else self.nmi_pending_clock
        )
        self.nmi_pending = False
        self.nmi_pending_clock = None
        self.nmi_pending_cancellable = False
        return clock

    def _decayed_open_bus(self) -> int:
        value = self.open_bus
        if value:
            clock = self.master_clock
            for bit, deadline in enumerate(self._open_bus_deadlines):
                mask = 1 << bit
                if value & mask and clock >= deadline:
                    value &= ~mask
            self.open_bus = value
        return value

    def _refresh_open_bus(self, value: int, mask: int = 0xFF) -> None:
        value &= 0xFF
        mask &= 0xFF
        self.open_bus = (
            (self._decayed_open_bus() & ~mask) | (value & mask)
        )
        deadline = self.master_clock + OPEN_BUS_DECAY_DOTS
        for bit in range(8):
            if mask & (1 << bit):
                self._open_bus_deadlines[bit] = deadline

    def _status_read_suppresses_vblank(self) -> bool:
        """Return whether a $2002 read lands on the vblank-set race.

        The dot renderer processes the cycle-1 event at the start of that
        cycle, while the fast renderer processes it when advancing from
        cycle 0 to cycle 1. Consequently the still-pending edge has a
        different internal cycle label in the two stepping representations.
        """
        if self.scanline != 241:
            return False
        return self.cycle == (0 if self.fast_mode else 1)

    def _control_write_loses_vblank_clear_race(self) -> bool:
        """Whether an NMI-enable write coincides with pre-render flag clear."""
        if self.scanline != 261:
            return False
        # The fast stepper coalesces the losing phase into cycle 0. The dot
        # stepper represents it as a short, cancellable edge at cycle 1.
        return self.fast_mode and self.cycle == 0

    def cpu_read_register(self, address: int) -> int:
        register = 0x2000 | (address & 7)
        if register == 0x2002:
            value = (
                (self.status & 0xE0)
                | (self._decayed_open_bus() & 0x1F)
            )
            # A status read immediately before vblank suppresses the upcoming
            # flag transition (and consequently its NMI).
            if self._status_read_suppresses_vblank():
                self.suppress_vblank = True
            self.status &= ~0x80
            self.write_toggle = False
            self._refresh_open_bus(value, 0xE0)
            self._update_nmi()
            return value
        if register == 0x2004:
            value = self.oam[self.oam_addr]
            if (self.oam_addr & 3) == 2:
                value &= 0xE3
            self._refresh_open_bus(value)
            return value
        if register == 0x2007:
            address_v = self.v & 0x3FFF
            fetched = self.read_memory(address_v)
            if address_v < 0x3F00:
                value = self.read_buffer
                self.read_buffer = fetched
                self._refresh_open_bus(value)
            else:
                value = (
                    (self._decayed_open_bus() & 0xC0)
                    | (fetched & 0x3F)
                )
                self.read_buffer = self.read_memory((address_v - 0x1000) & 0x3FFF)
                self._refresh_open_bus(value, 0x3F)
            self.v = (self.v + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF
            # PPUDATA leaves the incremented VRAM address on the PPU address
            # bus. Crossing $0FFF->$1000 here is another real MMC3 A12 edge.
            self.cartridge.mapper.notify_ppu_address(
                self.v & 0x3FFF,
                self.master_clock,
            )
            return value
        return self._decayed_open_bus()

    def cpu_write_register(self, address: int, value: int) -> None:
        register = 0x2000 | (address & 7)
        value &= 0xFF
        self._refresh_open_bus(value)
        if register == 0x2000:
            old_ctrl = self.ctrl
            self.ctrl = value
            if old_ctrl != value:
                self._fast_sprite_hit_plan_key = None
                self._fast_sprite_hit_cycle = 0
            self.t = (self.t & 0xF3FF) | ((value & 0x03) << 10)
            if (old_ctrl ^ value) & 0x28:
                self.invalidate_fast_sprite_cache()
                self._fast_sprite_mapper_token = (
                    self._sprite_mapper_dependency()
                )
            if (old_ctrl ^ value) & 0x10:
                self._fast_background_mapper_token = (
                    self._background_mapper_dependency()
                )
            enable_rise = not (old_ctrl & 0x80) and bool(value & 0x80)
            if not (
                enable_rise
                and (self.status & 0x80)
                and self._control_write_loses_vblank_clear_race()
            ):
                clear_race_pulse = (
                    enable_rise
                    and not self.fast_mode
                    and self.scanline == 261
                    and self.cycle == 1
                )
                self._update_nmi(cancellable=clear_race_pulse)
        elif register == 0x2001:
            if self.mask != value:
                self._fast_sprite_hit_plan_key = None
                self._fast_sprite_hit_cycle = 0
            self.mask = value
            rendering_target = bool(value & 0x18)
            if rendering_target != self._rendering_target:
                self._rendering_target = rendering_target
                self._rendering_change_clock = (
                    self.master_clock
                    + (
                        RENDERING_MASK_DELAY_DOTS
                        if self.fast_mode
                        else RENDERING_MASK_DELAY_DOTS - 1
                    )
                )
        elif register == 0x2003:
            self.oam_addr = value
        elif register == 0x2004:
            if self.oam[self.oam_addr] != value:
                self.oam[self.oam_addr] = value
                self.invalidate_fast_sprite_cache()
            self.oam_addr = (self.oam_addr + 1) & 0xFF
        elif register == 0x2005:
            self._fast_sprite_hit_plan_key = None
            self._fast_sprite_hit_cycle = 0
            if not self.write_toggle:
                self.fine_x = value & 0x07
                self.t = (self.t & 0x7FE0) | (value >> 3)
            else:
                self.t = (
                    (self.t & 0x0C1F)
                    | ((value & 0x07) << 12)
                    | ((value & 0xF8) << 2)
                )
            self.write_toggle = not self.write_toggle
        elif register == 0x2006:
            self._fast_sprite_hit_plan_key = None
            self._fast_sprite_hit_cycle = 0
            if not self.write_toggle:
                self.t = (self.t & 0x00FF) | ((value & 0x3F) << 8)
            else:
                self.t = (self.t & 0x7F00) | value
                self.v = self.t
            # The MMC3 observes PPU A12 on the external address bus, not
            # merely pattern-table reads.  CPU writes to PPUADDR place the
            # partially assembled VRAM address on that bus, so software can
            # deliberately clock the scanline counter while rendering is
            # disabled.
            self.cartridge.mapper.notify_ppu_address(
                self.t & 0x3FFF,
                self.master_clock,
            )
            self.write_toggle = not self.write_toggle
        elif register == 0x2007:
            self.write_memory(self.v & 0x3FFF, value)
            self.v = (self.v + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF
            self.cartridge.mapper.notify_ppu_address(
                self.v & 0x3FFF,
                self.master_clock,
            )

    def write_data_deferred(self, value: int, event_clock: int) -> None:
        """Apply one forced-blank PPUDATA write at a future logical clock.

        This entry point is intentionally narrower than
        :meth:`cpu_write_register`: the Console admits it only for MMC2
        nametable/palette writes which cannot clock a mapper latch. Devices
        are still advanced to ``event_clock`` at the scheduler deadline.
        """
        value &= 0xFF
        address = self.v & 0x3FFF

        # A full-byte PPU write replaces every decaying open-bus bit, so no
        # read/modify decay pass is required. Stamp the replacement with the
        # access's exact future master clock rather than the PPU's currently
        # deferred clock.
        self.open_bus = value
        deadline = int(event_clock) + OPEN_BUS_DECAY_DOTS
        deadlines = self._open_bus_deadlines
        deadlines[0] = deadline
        deadlines[1] = deadline
        deadlines[2] = deadline
        deadlines[3] = deadline
        deadlines[4] = deadline
        deadlines[5] = deadline
        deadlines[6] = deadline
        deadlines[7] = deadline

        if address < 0x3F00:
            index = self._nametable_index(address)
            if self.nametable_ram[index] != value:
                self.nametable_ram[index] = value
                self._mark_fast_nametable_write(index)
                self.invalidate_fast_cache(
                    preserve_mapper_viewports=True,
                    replay_content_tracked=True,
                )
        else:
            index = self._palette_index(address)
            palette_value = value & 0x3F
            if self.palette_ram[index] != palette_value:
                self.palette_ram[index] = palette_value
                self.invalidate_fast_palette_cache(index)

        self.v = (self.v + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF
        self.diagnostic_deferred_data_writes += 1

    def write_data_deferred_many(
        self,
        values: bytes | bytearray,
        final_event_clock: int,
    ) -> None:
        """Apply a forced-blank MMC2 PPUDATA stream as one cache transaction.

        No CPU-visible PPU event occurs inside the scheduler-approved span and
        production MMC2 cartridges use CHR ROM, so pattern-space writes are
        electrically ignored. Nametable/palette bytes and ``v`` still update
        in exact program order. Since every full-byte write replaces all PPU
        open-bus bits, only the final value and its exact clock survive after
        the stream. Presentation caches are invalidated once if any visible
        byte changed instead of once per decompressed byte.
        """
        if not values:
            return
        if self.cartridge.chr_is_ram:
            raise AssertionError("deferred MMC2 stream requires CHR ROM")

        final_value = int(values[-1]) & 0xFF
        self.open_bus = final_value
        deadline = int(final_event_clock) + OPEN_BUS_DECAY_DOTS
        deadlines = self._open_bus_deadlines
        deadlines[0] = deadline
        deadlines[1] = deadline
        deadlines[2] = deadline
        deadlines[3] = deadline
        deadlines[4] = deadline
        deadlines[5] = deadline
        deadlines[6] = deadline
        deadlines[7] = deadline

        mirroring = self.mapper.mirroring
        if mirroring == Mirroring.VERTICAL:
            table_bases = (0, 0x400, 0, 0x400)
        elif mirroring == Mirroring.HORIZONTAL:
            table_bases = (0, 0, 0x400, 0x400)
        elif mirroring == Mirroring.SINGLE_LOWER:
            table_bases = (0, 0, 0, 0)
        elif mirroring == Mirroring.SINGLE_UPPER:
            table_bases = (0x400, 0x400, 0x400, 0x400)
        else:
            table_bases = (0, 0x400, 0x800, 0xC00)

        v = self.v
        increment = 32 if self.ctrl & 0x04 else 1
        nametable = self.nametable_ram
        palette = self.palette_ram
        background_changed = False
        sprite_palette_changed = False
        for value in values:
            address = v & 0x3FFF
            if 0x2000 <= address < 0x3F00:
                relative = (address - 0x2000) & 0x0FFF
                index = (
                    table_bases[relative >> 10]
                    + (relative & 0x03FF)
                )
                if nametable[index] != value:
                    nametable[index] = value
                    self._mark_fast_nametable_write(index)
                    background_changed = True
            elif address >= 0x3F00:
                index = address & 0x1F
                if index in (0x10, 0x14, 0x18, 0x1C):
                    index -= 0x10
                palette_value = value & 0x3F
                if palette[index] != palette_value:
                    palette[index] = palette_value
                    if index >= 0x10:
                        sprite_palette_changed = True
                    else:
                        background_changed = True
            # $0000-$1FFF is CHR ROM on the admitted board and ignores writes.
            v = (v + increment) & 0x7FFF

        self.v = v
        self.diagnostic_deferred_data_writes += len(values)
        if background_changed:
            self.invalidate_fast_cache(
                preserve_mapper_viewports=True,
                replay_content_tracked=True,
            )
        elif sprite_palette_changed:
            self.invalidate_fast_sprite_palette_cache()

    def oam_dma(self, page: bytes | bytearray) -> None:
        if len(page) == 0x100:
            start = self.oam_addr
            if start == 0:
                changed = self.oam != page
                if changed:
                    self.oam[:] = page
            else:
                split = 0x100 - start
                changed = (
                    self.oam[start:] != page[:split]
                    or self.oam[:start] != page[split:]
                )
                if changed:
                    self.oam[start:] = page[:split]
                    self.oam[:start] = page[split:]
            # A 256-byte transfer wraps OAMADDR back to its original value.
            self.oam_addr = start
        else:
            changed = False
            for value in page:
                if self.oam[self.oam_addr] != value:
                    self.oam[self.oam_addr] = value
                    changed = True
                self.oam_addr = (self.oam_addr + 1) & 0xFF
        if changed:
            self.invalidate_fast_sprite_cache()

    def _increment_x(self) -> None:
        if (self.v & 0x001F) == 31:
            self.v &= ~0x001F
            self.v ^= 0x0400
        else:
            self.v += 1

    def _increment_y(self) -> None:
        if (self.v & 0x7000) != 0x7000:
            self.v += 0x1000
            return
        self.v &= ~0x7000
        coarse_y = (self.v & 0x03E0) >> 5
        if coarse_y == 29:
            coarse_y = 0
            self.v ^= 0x0800
        elif coarse_y == 31:
            coarse_y = 0
        else:
            coarse_y += 1
        self.v = (self.v & ~0x03E0) | (coarse_y << 5)

    def _copy_x(self) -> None:
        self.v = (self.v & ~0x041F) | (self.t & 0x041F)

    def _copy_y(self) -> None:
        self.v = (self.v & ~0x7BE0) | (self.t & 0x7BE0)

    def _shift_background(self) -> None:
        if self.mask & 0x08:
            self.bg_pattern_low = (self.bg_pattern_low << 1) & 0xFFFF
            self.bg_pattern_high = (self.bg_pattern_high << 1) & 0xFFFF
            self.bg_attribute_low = (self.bg_attribute_low << 1) & 0xFFFF
            self.bg_attribute_high = (self.bg_attribute_high << 1) & 0xFFFF

    def _load_background(self) -> None:
        self.bg_pattern_low = (self.bg_pattern_low & 0xFF00) | self.next_tile_low
        self.bg_pattern_high = (self.bg_pattern_high & 0xFF00) | self.next_tile_high
        self.bg_attribute_low = (
            (self.bg_attribute_low & 0xFF00)
            | (0xFF if self.next_tile_attribute & 1 else 0)
        )
        self.bg_attribute_high = (
            (self.bg_attribute_high & 0xFF00)
            | (0xFF if self.next_tile_attribute & 2 else 0)
        )

    def _fetch_background(self) -> None:
        phase = (self.cycle - 1) & 7
        if phase == 0:
            self._load_background()
            self.next_tile_id = self.read_memory(0x2000 | (self.v & 0x0FFF))
        elif phase == 2:
            address = (
                0x23C0
                | (self.v & 0x0C00)
                | ((self.v >> 4) & 0x38)
                | ((self.v >> 2) & 0x07)
            )
            attribute = self.read_memory(address)
            shift = ((self.v >> 4) & 4) | (self.v & 2)
            self.next_tile_attribute = (attribute >> shift) & 3
        elif phase == 4:
            fine_y = (self.v >> 12) & 7
            table = 0x1000 if self.ctrl & 0x10 else 0
            self.next_tile_low = self.read_memory(
                table + self.next_tile_id * 16 + fine_y
            )
        elif phase == 6:
            fine_y = (self.v >> 12) & 7
            table = 0x1000 if self.ctrl & 0x10 else 0
            self.next_tile_high = self.read_memory(
                table + self.next_tile_id * 16 + fine_y + 8
            )
        elif phase == 7:
            self._increment_x()

    def _sprite_pattern(self, tile: int, attributes: int, row: int) -> tuple[int, int]:
        height = 16 if self.ctrl & 0x20 else 8
        if attributes & 0x80:
            row = height - 1 - row
        if height == 16:
            table = (tile & 1) * 0x1000
            tile &= 0xFE
            if row >= 8:
                tile += 1
                row -= 8
        else:
            table = 0x1000 if self.ctrl & 0x08 else 0
        address = table + tile * 16 + row
        if self._mmc3_mapper is not None:
            # Sprite evaluation is intentionally coalesced at dot 257 for
            # rendering speed. MMC3 A12 timing cannot use those early data
            # reads; its real address-bus phases are emitted at dots 260 and
            # 324 by the scanline steppers below.
            return (
                self._mapper_ppu_read(address),
                self._mapper_ppu_read(address + 8),
            )
        return self.read_memory(address), self.read_memory(address + 8)

    def _notify_mmc3_scanline_bus(
        self,
        cycle: int,
        event_clock: int,
    ) -> None:
        """Emit the scanline-level A12 transitions seen by MMC3.

        The first background pattern address is present at dot 4, sprite
        fetch addresses begin at dot 260, and the prefetched background for
        the following line begins at dot 324. The selected pattern tables and
        MMC3's A12 low-pass filter determine which rising edge qualifies. This
        also models all eight dummy sprite fetch slots when no sprites match.
        """
        mapper = self._mmc3_mapper
        if mapper is None:
            return
        if cycle == 4:
            address = 0x1000 if self.ctrl & 0x10 else 0
        elif cycle == 260:
            address = 0x1000 if self.ctrl & 0x08 else 0
        elif cycle == 324:
            address = 0x1000 if self.ctrl & 0x10 else 0
        else:
            return
        mapper.notify_ppu_address(address, event_clock)

    def dots_to_mmc3_irq_assertion(self) -> int | None:
        """Predict the next scanline edge that can assert an MMC3 IRQ.

        Fast rendering already advances every modeled A12 transition inside
        :meth:`step_fast`.  The CPU scheduler only needs to regain control at
        the transition that can *assert* IRQ, not at the hundreds of earlier
        counter clocks.  Simulate the mapper's tiny counter/filter state here
        without mutating it and return a distance bounded by the current
        frame. CPU writes to PPUCTRL or the MMC3 registers are synchronized
        bus accesses and therefore invalidate this prediction naturally.
        """
        mapper = self._mmc3_mapper
        if (
            mapper is None
            or not mapper.irq_enabled
            or mapper.irq_pending
            or not self.rendering_enabled
        ):
            return None

        scanline = self.scanline
        cycle = self.cycle
        # This scheduler optimization is needed during the rendered portion
        # of a frame. Vblank and pre-render have other CPU-visible edges and
        # retain their existing short deadlines.
        if not 0 <= scanline < PPU_HEIGHT:
            return None

        ctrl = self.ctrl
        background_address = 0x1000 if ctrl & 0x10 else 0
        sprite_address = 0x1000 if ctrl & 0x08 else 0
        last_a12 = bool(mapper.last_a12)
        low_since = int(mapper.a12_low_since)
        counter = int(mapper.irq_counter)
        latch = int(mapper.irq_latch)
        reload_counter = bool(mapper.irq_reload)
        start_clock = self.master_clock
        # With no intervening register write, the counter reaches zero after
        # a fixed number of qualified edges. Determine that count directly;
        # the former predictor simulated up to 720 candidate phases every
        # time the CPU scheduler refreshed a device deadline.
        if counter == 0 or reload_counter:
            edges_remaining = 1 if latch == 0 else latch + 1
        else:
            edges_remaining = counter

        def inspect_line(line_offset: int, first_cycle: int) -> int | None:
            nonlocal edges_remaining, last_a12, low_since
            line_start = line_offset * 341 - cycle
            for event_cycle, address in (
                (4, background_address),
                (260, sprite_address),
                (324, background_address),
            ):
                if event_cycle <= first_cycle:
                    continue
                event_elapsed = line_start + event_cycle
                event_clock = start_clock + event_elapsed
                a12 = bool(address & 0x1000)
                if not a12:
                    if last_a12:
                        low_since = event_clock
                elif (
                    not last_a12
                    and event_clock - low_since >= 9
                ):
                    edges_remaining -= 1
                    if edges_remaining == 0:
                        return event_elapsed
                last_a12 = a12
            return None

        assertion = inspect_line(0, cycle)
        if assertion is not None:
            return assertion

        # One complete following line normalizes even a restored or synthetic
        # filter state. Thereafter unequal pattern tables have exactly one
        # qualified rise per line (dot 260 or 324); equal tables remain at a
        # constant A12 level and cannot produce another rise.
        if scanline + 1 >= PPU_HEIGHT:
            return None
        assertion = inspect_line(1, -1)
        if assertion is not None:
            return assertion
        if background_address == sprite_address:
            return None

        stable_lines = PPU_HEIGHT - scanline - 2
        if edges_remaining > stable_lines:
            return None
        edge_cycle = 260 if sprite_address else 324
        return (edges_remaining + 1) * 341 - cycle + edge_cycle

    def _evaluate_sprites(self, target_scanline: int) -> None:
        self.sprites = []
        if not 0 <= target_scanline < PPU_HEIGHT:
            return
        height = 16 if self.ctrl & 0x20 else 8
        for index in range(64):
            base = index * 4
            y = self.oam[base] + 1
            row = target_scanline - y
            if 0 <= row < height:
                if len(self.sprites) < 8:
                    tile = self.oam[base + 1]
                    attributes = self.oam[base + 2]
                    x = self.oam[base + 3]
                    low, high = self._sprite_pattern(tile, attributes, row)
                    self.sprites.append((index, x, attributes, low, high))

    def _sprite_overflow_cycle(self, scanline: int) -> int:
        """Return the PPU dot where the sprite-overflow latch is raised.

        Sprite evaluation reads one primary-OAM byte on each odd dot and
        consumes it on the following even dot. Before secondary OAM fills,
        an in-range sprite therefore costs eight dots and an out-of-range
        sprite costs two. Once eight sprites have been copied, the 2C02's
        disabled secondary-OAM write advances the byte index diagonally:
        Y of sprite N, tile of N+1, attributes of N+2, X of N+3, and so on.
        Comparing those non-Y bytes is the silicon overflow bug.

        ``scanline`` is the raw visible line doing the evaluation. Its result
        supplies sprites to the following line, so comparing the raw OAM Y
        byte directly is equivalent to the usual displayed-Y-plus-one rule.
        """
        if not 0 <= scanline < PPU_HEIGHT:
            return 0

        height = 16 if self.ctrl & 0x20 else 8
        oam = self.oam
        sprite_index = 0
        byte_index = 0
        sprites_found = 0
        compare_cycle = 66

        while sprite_index < 64 and compare_cycle <= 256:
            candidate = oam[sprite_index * 4 + byte_index]
            in_range = 0 <= scanline - candidate < height
            if sprites_found < 8:
                if in_range:
                    sprites_found += 1
                    # Y comparison plus the remaining three byte copies.
                    compare_cycle += 8
                else:
                    compare_cycle += 2
                sprite_index += 1
                continue

            if in_range:
                return compare_cycle

            # With secondary OAM full, both counters increment together.
            # The low counter wraps every four sprites while the high counter
            # continues to the end of primary OAM.
            sprite_index += 1
            byte_index = (byte_index + 1) & 3
            compare_cycle += 2

        return 0

    def _fast_overflow_cycle(self, scanline: int) -> int:
        cached = self._fast_sprite_overflow_cycles[scanline]
        if cached < 0:
            candidates = self._fast_sprite_overflow_candidates
            if candidates is None:
                height = 16 if self.ctrl & 0x20 else 8
                coverage_delta = [0] * (PPU_HEIGHT + 1)
                oam = self.oam
                for base in range(0, 0x100, 4):
                    first_line = oam[base]
                    if first_line >= PPU_HEIGHT:
                        continue
                    end_line = min(PPU_HEIGHT, first_line + height)
                    coverage_delta[first_line] += 1
                    coverage_delta[end_line] -= 1
                coverage = 0
                candidate_flags = bytearray(PPU_HEIGHT)
                for line in range(PPU_HEIGHT):
                    coverage += coverage_delta[line]
                    candidate_flags[line] = int(coverage >= 8)
                candidates = bytes(candidate_flags)
                self._fast_sprite_overflow_candidates = candidates
            cached = (
                self._sprite_overflow_cycle(scanline)
                if candidates[scanline]
                else 0
            )
            self._fast_sprite_overflow_cycles[scanline] = cached
        return cached

    def _background_pixel(self, x: int) -> tuple[int, int]:
        if not (self.mask & 0x08) or (x < 8 and not (self.mask & 0x02)):
            return 0, 0
        selector = 0x4000 >> self.fine_x
        low = 1 if self.bg_pattern_low & selector else 0
        high = 2 if self.bg_pattern_high & selector else 0
        pixel = low | high
        attr_low = 1 if self.bg_attribute_low & selector else 0
        attr_high = 2 if self.bg_attribute_high & selector else 0
        return pixel, attr_low | attr_high

    def _sprite_pixel(self, x: int) -> tuple[int, int, bool, bool]:
        if not (self.mask & 0x10) or (x < 8 and not (self.mask & 0x04)):
            return 0, 0, False, False
        for index, sprite_x, attributes, low, high in self.sprites:
            offset = x - sprite_x
            if not 0 <= offset < 8:
                continue
            bit = offset if attributes & 0x40 else 7 - offset
            pixel = ((low >> bit) & 1) | (((high >> bit) & 1) << 1)
            if pixel:
                return pixel, attributes & 3, bool(attributes & 0x20), index == 0
        return 0, 0, False, False

    def _sprite_zero_overlaps(self, x: int) -> bool:
        if x >= 255 or self.status & 0x40:
            return False
        bg_pixel, _ = self._background_pixel(x)
        if not bg_pixel:
            return False
        sp_pixel, _, _, sprite_zero = self._sprite_pixel(x)
        return bool(sp_pixel and sprite_zero)

    def _color(self, palette_address: int) -> tuple[int, int, int]:
        # Palette selection is internal to the PPU and does not perform a CHR
        # bus transaction. Reading the local palette array directly is both
        # closer to the hardware path and substantially cheaper per pixel.
        index = self.palette_ram[self._palette_index(palette_address)] & 0x3F
        if self.mask & 1:
            index &= 0x30
        red, green, blue = NES_PALETTE[index]
        emphasis = (self.mask >> 5) & 7
        if emphasis:
            factors = [1.0, 1.0, 1.0]
            # The real 2C02 changes composite phase/amplitude. This compact
            # RGB approximation preserves the visible emphasis semantics.
            if emphasis & 1:
                factors[1] *= 0.78
                factors[2] *= 0.78
            if emphasis & 2:
                factors[0] *= 0.78
                factors[2] *= 0.78
            if emphasis & 4:
                factors[0] *= 0.78
                factors[1] *= 0.78
            red, green, blue = (
                int(red * factors[0]),
                int(green * factors[1]),
                int(blue * factors[2]),
            )
        return red, green, blue

    def _render_pixel(self) -> None:
        x = self.cycle - 1
        y = self.scanline
        bg_pixel, bg_palette = self._background_pixel(x)
        sp_pixel, sp_palette, behind, sprite_zero = self._sprite_pixel(x)

        if bg_pixel == 0 and sp_pixel == 0:
            palette_address = 0x3F00
        elif bg_pixel == 0:
            palette_address = 0x3F10 + sp_palette * 4 + sp_pixel
        elif sp_pixel == 0:
            palette_address = 0x3F00 + bg_palette * 4 + bg_pixel
        else:
            if behind:
                palette_address = 0x3F00 + bg_palette * 4 + bg_pixel
            else:
                palette_address = 0x3F10 + sp_palette * 4 + sp_pixel

        red, green, blue = self._color(palette_address)
        offset = (y * PPU_WIDTH + x) * 3
        self.framebuffer[offset] = red
        self.framebuffer[offset + 1] = green
        self.framebuffer[offset + 2] = blue

    def _fast_pattern_row(
        self,
        low: int,
        high: int,
        attribute: int,
        palette_signature: bytes,
    ) -> tuple[bytes, bytes]:
        key = (
            low,
            high,
            attribute,
            palette_signature,
            self.mask & 0xE1,
        )
        cached = self._fast_row_cache.get(key)
        if cached is not None:
            return cached
        row = bytearray()
        opaque = bytearray(8)
        for column in range(8):
            bit = 7 - column
            pixel = ((low >> bit) & 1) | (((high >> bit) & 1) << 1)
            opaque[column] = int(pixel != 0)
            palette_address = (
                0x3F00 if pixel == 0 else 0x3F00 + attribute * 4 + pixel
            )
            row.extend(self._color(palette_address))
        result = bytes(row), bytes(opaque)
        if len(self._fast_row_cache) >= 16384:
            for _ in range(1024):
                oldest = self._fast_row_cache_order.popleft()
                self._fast_row_cache.pop(oldest, None)
        self._fast_row_cache[key] = result
        self._fast_row_cache_order.append(key)
        return result

    def _fast_pattern_read(self, address: int) -> int:
        if self._direct_chr is not None:
            return self._direct_chr[address & 0x1FFF]
        return self._mapper_ppu_read(address & 0x1FFF)

    def _fast_palette_signature_bytes(self) -> bytes:
        """Return an immutable palette key without allocating every scanline."""
        signature = self._fast_palette_signature
        if signature != self.palette_ram:
            signature = bytes(self.palette_ram)
            self._fast_palette_signature = signature
        return signature

    def _fast_palette_part_signatures(self) -> tuple[bytes, bytes]:
        """Return allocation-free background and sprite palette identities."""
        signature = self._fast_palette_signature_bytes()
        if signature is not self._fast_palette_parts_source:
            self._fast_palette_parts_source = signature
            self._fast_background_palette_signature = signature[:0x10]
            self._fast_sprite_palette_signature = signature[0x10:]
        return (
            self._fast_background_palette_signature,
            self._fast_sprite_palette_signature,
        )

    def _fast_palette_colors(self) -> tuple[tuple[int, int, int], ...]:
        key = (self._fast_palette_signature_bytes(), self.mask & 0xE1)
        cached = self._fast_palette_cache.get(key)
        if cached is not None:
            return cached
        colors = tuple(self._color(0x3F00 + index) for index in range(0x20))
        if len(self._fast_palette_cache) >= 64:
            self._fast_palette_cache.clear()
        self._fast_palette_cache[key] = colors
        return colors

    def _build_fast_sprite_lines(self) -> None:
        """Index the first eight sprite rows that can affect each scanline."""
        height = 16 if self.ctrl & 0x20 else 8
        lines: list[list[tuple[int, int, int, int]]] = [
            [] for _ in range(PPU_HEIGHT)
        ]
        oam = self.oam
        table_8x8 = 0x1000 if self.ctrl & 0x08 else 0
        for index in range(64):
            base = index * 4
            sprite_y = oam[base] + 1
            first_line = max(0, sprite_y)
            last_line = min(PPU_HEIGHT, sprite_y + height)
            if first_line >= last_line:
                continue
            tile = oam[base + 1]
            attributes = oam[base + 2]
            sprite_x = oam[base + 3]
            for y in range(first_line, last_line):
                line = lines[y]
                if len(line) >= 8:
                    continue
                row_number = y - sprite_y
                if attributes & 0x80:
                    row_number = height - 1 - row_number
                row_tile = tile
                if height == 16:
                    table_base = (row_tile & 1) * 0x1000
                    row_tile &= 0xFE
                    if row_number >= 8:
                        row_tile += 1
                        row_number -= 8
                else:
                    table_base = table_8x8
                address = table_base + row_tile * 16 + row_number
                line.append((index, sprite_x, attributes, address))
        self._fast_sprite_lines = tuple(tuple(line) for line in lines)

    def _render_latched_background_scanline_fast(
        self,
        start_v: int,
        line_offset: int,
        line_end: int,
        universal: bytes,
        palette_signature: bytes | None = None,
        nametable_signature: tuple | None = None,
    ) -> tuple[bytes, bytes]:
        """Render one viewport in PPU fetch order for latch-based CHR mappers."""
        pattern_base = 0x1000 if self.ctrl & 0x10 else 0
        mirroring = self.mapper.mirroring
        if nametable_signature is None:
            nametable_signature = (
                self._fast_nametable_replay_signature(start_v, mirroring)
                if self._fast_nametable_generation
                else ()
            )
        plan_key = (
            start_v,
            pattern_base,
            mirroring,
            nametable_signature,
        )
        fetch_plan = self._fast_latched_fetch_plan_cache.get(plan_key)
        if fetch_plan is None:
            coarse_x = start_v & 0x1F
            coarse_y = (start_v >> 5) & 0x1F
            base_nt_x = (start_v >> 10) & 1
            base_nt_y = (start_v >> 11) & 1
            fine_y = (start_v >> 12) & 7
            effective_nt_y = (base_nt_y + coarse_y // 30) & 1
            tile_y = coarse_y % 30
            if mirroring == Mirroring.VERTICAL:
                table_bases = (0, 0x400, 0, 0x400)
            elif mirroring == Mirroring.HORIZONTAL:
                table_bases = (0, 0, 0x400, 0x400)
            elif mirroring == Mirroring.SINGLE_LOWER:
                table_bases = (0, 0, 0, 0)
            elif mirroring == Mirroring.SINGLE_UPPER:
                table_bases = (0x400, 0x400, 0x400, 0x400)
            else:
                table_bases = (0, 0x400, 0x800, 0xC00)

            nametable_ram = self.nametable_ram
            tile_row_offset = tile_y * 32
            attribute_row_offset = 0x3C0 + (tile_y >> 2) * 8
            # Pack each 13-bit pattern address and 2-bit attribute into one
            # little-endian 16-bit item. A complete plan is 66 bytes instead
            # of 33 nested Python tuples, small enough to retain a whole
            # horizontal scroll cycle without allocator churn.
            plan_items = bytearray(66)
            plan_offset = 0
            for viewport_tile in range(33):
                world_tile_x = coarse_x + viewport_tile
                nt_x = (base_nt_x + (world_tile_x >> 5)) & 1
                tile_x = world_tile_x & 0x1F
                table = (effective_nt_y << 1) | nt_x
                table_base = table_bases[table]
                tile = nametable_ram[
                    table_base + tile_row_offset + tile_x
                ]
                attribute_byte = nametable_ram[
                    table_base + attribute_row_offset + (tile_x >> 2)
                ]
                shift = ((tile_y & 2) << 1) | (tile_x & 2)
                attribute = (attribute_byte >> shift) & 3
                packed = (
                    pattern_base
                    + tile * 16
                    + fine_y
                    + (attribute << 13)
                )
                plan_items[plan_offset] = packed & 0xFF
                plan_items[plan_offset + 1] = packed >> 8
                plan_offset += 2
            fetch_plan = bytes(plan_items)
            if (
                len(self._fast_latched_fetch_plan_cache)
                >= FAST_LATCHED_FETCH_PLAN_CACHE_LIMIT
            ):
                oldest = self._fast_latched_fetch_plan_cache_order.popleft()
                self._fast_latched_fetch_plan_cache.pop(oldest, None)
            self._fast_latched_fetch_plan_cache[plan_key] = fetch_plan
            self._fast_latched_fetch_plan_cache_order.append(plan_key)

        pattern_row = self._fast_pattern_row
        if palette_signature is None:
            palette_signature, _ = self._fast_palette_part_signatures()
        row_cache_get = self._fast_row_cache.get
        row_mask = self.mask & 0xE1
        tile_run_pixels: bytes
        tile_run_opaque: bytes

        # A scrolled 256-pixel viewport can span 33 tiles. Reading only those
        # tiles, left to right, preserves the MMC2 FD/FE latch sequence and
        # avoids offscreen reads changing the bank before visible fetches.
        begin_latched_pairs = self._mapper_begin_latched_pairs
        end_latched_pairs = self._mapper_end_latched_pairs
        if begin_latched_pairs is not None and end_latched_pairs is not None:
            (
                memory,
                base_0000,
                base_1000,
                fd_0000,
                fe_0000,
                fd_1000,
                fe_1000,
                latch_0000_fe,
                latch_1000_fe,
            ) = begin_latched_pairs()
            tile_run_key = (
                fetch_plan,
                row_mask,
                palette_signature,
                fd_0000,
                fe_0000,
                fd_1000,
                fe_1000,
                latch_0000_fe,
                latch_1000_fe,
            )
            cached_tile_run = self._fast_latched_tile_run_cache.get(
                tile_run_key
            )
            if cached_tile_run is None:
                pixel_rows: list[bytes] = []
                opaque_rows: list[bytes] = []
                for plan_offset in range(0, len(fetch_plan), 2):
                    packed = (
                        fetch_plan[plan_offset]
                        | (fetch_plan[plan_offset + 1] << 8)
                    )
                    pattern_address = packed & 0x1FFF
                    attribute = packed >> 13
                    bank_base = (
                        base_1000
                        if pattern_address & 0x1000
                        else base_0000
                    )
                    row_address = pattern_address & 0x0FFF
                    low = memory[bank_base + row_address]
                    high = memory[
                        bank_base + ((row_address + 8) & 0x0FFF)
                    ]
                    trigger = pattern_address & 0x1FF8
                    if trigger == 0x0FD0 and pattern_address == 0x0FD0:
                        latch_0000_fe = False
                        base_0000 = fd_0000
                    elif trigger == 0x0FE0 and pattern_address == 0x0FE0:
                        latch_0000_fe = True
                        base_0000 = fe_0000
                    elif trigger == 0x1FD0:
                        latch_1000_fe = False
                        base_1000 = fd_1000
                    elif trigger == 0x1FE0:
                        latch_1000_fe = True
                        base_1000 = fe_1000
                    cached_row = row_cache_get(
                        (low, high, attribute, palette_signature, row_mask)
                    )
                    if cached_row is None:
                        row, opaque = pattern_row(
                            low,
                            high,
                            attribute,
                            palette_signature,
                        )
                    else:
                        row, opaque = cached_row
                    pixel_rows.append(row)
                    opaque_rows.append(opaque)
                tile_run_pixels = b"".join(pixel_rows)
                tile_run_opaque = b"".join(opaque_rows)
                if (
                    len(self._fast_latched_tile_run_cache)
                    >= FAST_LATCHED_TILE_RUN_CACHE_LIMIT
                ):
                    oldest = (
                        self._fast_latched_tile_run_cache_order.popleft()
                    )
                    self._fast_latched_tile_run_cache.pop(oldest, None)
                self._fast_latched_tile_run_cache[tile_run_key] = (
                    tile_run_pixels,
                    tile_run_opaque,
                    base_0000,
                    base_1000,
                    latch_0000_fe,
                    latch_1000_fe,
                )
                self._fast_latched_tile_run_cache_order.append(tile_run_key)
            else:
                (
                    tile_run_pixels,
                    tile_run_opaque,
                    base_0000,
                    base_1000,
                    latch_0000_fe,
                    latch_1000_fe,
                ) = cached_tile_run
            end_latched_pairs(
                base_0000,
                base_1000,
                latch_0000_fe,
                latch_1000_fe,
            )
        else:
            tile_run_key = (
                plan_key,
                row_mask,
                palette_signature,
                self._mapper_ppu_pattern_cache_token(pattern_base),
            )
            cached_tile_run = self._fast_viewport_tile_run_cache.get(
                tile_run_key
            )
            if cached_tile_run is None:
                pixel_rows = []
                opaque_rows = []
                for plan_offset in range(0, len(fetch_plan), 2):
                    packed = (
                        fetch_plan[plan_offset]
                        | (fetch_plan[plan_offset + 1] << 8)
                    )
                    pattern_address = packed & 0x1FFF
                    attribute = packed >> 13
                    low, high = self._mapper_ppu_read_pair(pattern_address)
                    cached_row = row_cache_get(
                        (low, high, attribute, palette_signature, row_mask)
                    )
                    if cached_row is None:
                        row, opaque = pattern_row(
                            low,
                            high,
                            attribute,
                            palette_signature,
                        )
                    else:
                        row, opaque = cached_row
                    pixel_rows.append(row)
                    opaque_rows.append(opaque)
                tile_run_pixels = b"".join(pixel_rows)
                tile_run_opaque = b"".join(opaque_rows)
                if (
                    len(self._fast_viewport_tile_run_cache)
                    >= FAST_VIEWPORT_TILE_RUN_CACHE_LIMIT
                ):
                    oldest = (
                        self._fast_viewport_tile_run_cache_order.popleft()
                    )
                    self._fast_viewport_tile_run_cache.pop(oldest, None)
                self._fast_viewport_tile_run_cache[tile_run_key] = (
                    tile_run_pixels,
                    tile_run_opaque,
                )
                self._fast_viewport_tile_run_cache_order.append(tile_run_key)
            else:
                tile_run_pixels, tile_run_opaque = cached_tile_run

        first_x = self.fine_x
        last_x = first_x + PPU_WIDTH
        pixels = tile_run_pixels[first_x * 3:last_x * 3]
        background_opaque = tile_run_opaque[first_x:last_x]
        if not (self.mask & 0x02):
            pixels = universal * 8 + pixels[24:]
            background_opaque = (
                EMPTY_OPAQUE_LEFT_EDGE + background_opaque[8:]
            )
        self.framebuffer[line_offset:line_end] = pixels
        return pixels, background_opaque

    def _render_background_scanline_fast(
        self,
        start_v: int,
        line_offset: int,
        line_end: int,
        background_palette: bytes | None = None,
        universal: bytes | None = None,
        background_context: tuple[Mirroring, int, tuple] | None = None,
        nametable_signature: tuple | None = None,
    ) -> tuple[bytes, bytes]:
        if universal is None:
            universal = bytes(self._color(0x3F00))
        if background_palette is None:
            background_palette, _ = self._fast_palette_part_signatures()
        if not (self.mask & 0x08):
            pixels = universal * PPU_WIDTH
            self.framebuffer[line_offset:line_end] = pixels
            return pixels, EMPTY_OPAQUE_SCANLINE
        if background_context is None:
            pattern_base = 0x1000 if self.ctrl & 0x10 else 0
            mirroring = self.mapper.mirroring
            pattern_token = self._mapper_ppu_pattern_cache_token(
                pattern_base
            )
        else:
            mirroring, pattern_base, pattern_token = background_context
        if nametable_signature is None:
            nametable_signature = (
                self._fast_nametable_replay_signature(start_v, mirroring)
                if self._fast_nametable_generation
                else ()
            )
        if self._ppu_reads_have_side_effects:
            return self._render_latched_background_scanline_fast(
                start_v,
                line_offset,
                line_end,
                universal,
                background_palette,
                nametable_signature,
            )
        coarse_x = start_v & 0x1F
        coarse_y = (start_v >> 5) & 0x1F
        base_nt_x = (start_v >> 10) & 1
        base_nt_y = (start_v >> 11) & 1
        fine_y = (start_v >> 12) & 7
        effective_nt_y = (base_nt_y + coarse_y // 30) & 1
        tile_y = coarse_y % 30
        world_key = (
            effective_nt_y,
            tile_y,
            fine_y,
            pattern_base,
            self.mask & 0xE1,
            mirroring,
            # Mapper invalidations advance the replay fallback generation,
            # but the effective CHR map is already represented by the mapper
            # token below. Excluding that redundant counter lets recurring
            # MMC3 scanline maps reuse their immutable world rows. CHR-RAM
            # and nametable content remain explicitly versioned here.
            self._fast_chr_generation,
            nametable_signature,
            background_palette,
            pattern_token,
        )
        cached_world = self._fast_background_world_cache.get(world_key)
        if (
            cached_world is not None
            and len(self._fast_background_world_cache)
            >= FAST_BACKGROUND_WORLD_CACHE_LIMIT * 3 // 4
        ):
            # Refresh recency in C without allocating a replacement value.
            # A growing cache cannot evict anything yet, so begin tracking
            # only near its bound. This preserves enough history for correct
            # LRU eviction without paying for a mutation on every early hit.
            self._fast_background_world_cache.move_to_end(world_key)
        if cached_world is None and not self._fast_world_cache_active:
            # A new mapper map makes all prior world rows inapplicable.
            # Render only the 33 visible tiles until the state either survives
            # a frame or recurs; repeating states can reuse their keyed rows.
            return self._render_latched_background_scanline_fast(
                start_v,
                line_offset,
                line_end,
                universal,
                background_palette,
                nametable_signature,
            )
        if cached_world is None:
            # A recurring world map is worth caching, but constructing all
            # 240 double-width rows on its first appearance creates a large
            # host-only tail. Track each mapper/palette state independently so
            # alternating MMC3 maps spread their first decode over two
            # appearances instead of aliasing against frame parity.
            admission_token = (
                mirroring,
                pattern_base,
                self.mask & 0xE1,
                self._fast_chr_generation,
                background_palette,
                pattern_token,
            )
            admission_states = self._fast_world_admission_states
            admission_state = admission_states.get(admission_token)
            frame_number = self.frame_number
            if admission_state is None:
                if len(admission_states) >= 128:
                    admission_states.clear()
                admission_phase = 0
                admission_states[admission_token] = (
                    frame_number,
                    admission_phase,
                )
            else:
                admission_frame, admission_phase = admission_state
                if admission_frame != frame_number:
                    admission_phase = min(1, admission_phase + 1)
                    admission_states[admission_token] = (
                        frame_number,
                        admission_phase,
                    )
            line = line_offset // (PPU_WIDTH * 3)
            if (
                admission_phase == 0
                and (
                    line % FAST_BACKGROUND_WORLD_INITIAL_ADMISSION_DIVISOR
                ) != 0
            ):
                self.diagnostic_world_cache_admission_deferrals += 1
                return self._render_latched_background_scanline_fast(
                    start_v,
                    line_offset,
                    line_end,
                    universal,
                    background_palette,
                    nametable_signature,
                )
        if cached_world is None:
            self.diagnostic_world_cache_misses += 1
            nametable_ram = self.nametable_ram
            if mirroring == Mirroring.VERTICAL:
                table_bases = (0, 0x400, 0, 0x400)
            elif mirroring == Mirroring.HORIZONTAL:
                table_bases = (0, 0, 0x400, 0x400)
            elif mirroring == Mirroring.SINGLE_LOWER:
                table_bases = (0, 0, 0, 0)
            elif mirroring == Mirroring.SINGLE_UPPER:
                table_bases = (0x400, 0x400, 0x400, 0x400)
            else:
                table_bases = (0, 0x400, 0x800, 0xC00)
            direct_chr = self._direct_chr
            mapper_read_pair = self._mapper_ppu_read_pair
            pattern_row = self._fast_pattern_row
            palette_signature = background_palette
            world_pixels = bytearray(PPU_WIDTH * 2 * 3)
            world_opaque = bytearray(PPU_WIDTH * 2)
            tile_row_offset = tile_y * 32
            attribute_row_offset = 0x3C0 + (tile_y >> 2) * 8

            for nt_x in range(2):
                table = (effective_nt_y << 1) | nt_x
                table_base = table_bases[table]
                world_nt_x = nt_x * PPU_WIDTH
                for tile_x in range(32):
                    tile = nametable_ram[
                        table_base + tile_row_offset + tile_x
                    ]
                    attribute_byte = nametable_ram[
                        table_base
                        + attribute_row_offset
                        + (tile_x >> 2)
                    ]
                    shift = ((tile_y & 2) << 1) | (tile_x & 2)
                    attribute = (attribute_byte >> shift) & 3
                    pattern_address = pattern_base + tile * 16 + fine_y
                    if direct_chr is not None:
                        low = direct_chr[pattern_address & 0x1FFF]
                        high = direct_chr[(pattern_address + 8) & 0x1FFF]
                    else:
                        low, high = mapper_read_pair(
                            pattern_address & 0x1FFF
                        )
                    row, opaque = pattern_row(
                        low, high, attribute, palette_signature
                    )
                    destination_x = world_nt_x + tile_x * 8
                    destination = destination_x * 3
                    world_pixels[destination:destination + 24] = row
                    world_opaque[destination_x:destination_x + 8] = opaque
            cached_world = bytes(world_pixels), bytes(world_opaque)
            if (
                len(self._fast_background_world_cache)
                >= FAST_BACKGROUND_WORLD_CACHE_LIMIT
            ):
                self._fast_background_world_cache.popitem(last=False)
                self.diagnostic_world_cache_evictions += 1
            self._fast_background_world_cache[world_key] = cached_world

        world_pixels, world_opaque = cached_world
        world_x = (
            base_nt_x * PPU_WIDTH + coarse_x * 8 + self.fine_x
        ) & 0x1FF
        world_end = world_x + PPU_WIDTH
        if world_end <= PPU_WIDTH * 2:
            pixels = world_pixels[world_x * 3:world_end * 3]
            opaque = world_opaque[world_x:world_end]
        else:
            wrap = world_end - PPU_WIDTH * 2
            pixels = (
                world_pixels[world_x * 3:]
                + world_pixels[:wrap * 3]
            )
            opaque = world_opaque[world_x:] + world_opaque[:wrap]
        if not (self.mask & 0x02):
            pixels = universal * 8 + pixels[24:]
            opaque = EMPTY_OPAQUE_LEFT_EDGE + opaque[8:]
        self.framebuffer[line_offset:line_end] = pixels
        return pixels, opaque

    def _fast_frame_replay_key(
        self,
        y: int,
        palette_parts: tuple[bytes, bytes] | None = None,
        nametable_signature: tuple | None = None,
    ) -> tuple:
        """Return every render-visible input for one retained scanline."""
        if self._fast_sprite_lines is None:
            self._build_fast_sprite_lines()
        assert self._fast_sprite_lines is not None
        latch_state = self._mapper_latch_state
        latches = latch_state() if latch_state is not None else ()
        background_palette, sprite_palette = (
            self._fast_palette_part_signatures()
            if palette_parts is None
            else palette_parts
        )
        matches = self._fast_sprite_lines[y]
        sprites_visible = bool(self.mask & 0x10 and matches)
        if nametable_signature is None:
            nametable_signature = (
                self._fast_nametable_replay_signature(self.v)
                if self._fast_nametable_generation
                else ()
            )
        world_signature = (
            self._fast_replay_fallback_generation,
            self._fast_chr_generation,
            nametable_signature,
        )
        return (
            self.v,
            self.fine_x,
            self.ctrl,
            self.mask,
            world_signature,
            background_palette,
            sprite_palette if sprites_visible else b"",
            self._fast_background_mapper_generation,
            (
                self._fast_sprite_mapper_generation
                if sprites_visible
                else -1
            ),
            matches,
            *latches,
        )

    def _render_scanline_fast(
        self,
        y: int,
        palette_parts: tuple[bytes, bytes] | None = None,
        palette_colors: tuple[tuple[int, int, int], ...] | None = None,
        universal: bytes | None = None,
        background_context: tuple[Mirroring, int, tuple] | None = None,
    ) -> int:
        """Compose one line and return its first sprite-zero-hit dot.

        Pixel composition is cacheable and may run before the beam reaches
        the corresponding pixels. The returned dot lets ``step_fast`` expose
        the status transition at hardware time instead of leaking the bulk
        renderer's completion time to the CPU. Line zero always enters the
        ordinary cache-admission path because it establishes the signatures
        used by the remaining frame.
        """
        self.diagnostic_scanline_renders += 1
        nametable_signature = (
            self._fast_nametable_replay_signature(
                self.v,
                None if background_context is None else background_context[0],
            )
            if self._fast_nametable_generation
            else ()
        )
        replay_key = self._fast_frame_replay_key(
            y,
            palette_parts,
            nametable_signature,
        )
        replay = self._fast_frame_replay_lines[y]
        if y != 0 and replay is not None and replay[0] == replay_key:
            hit_cycle = int(replay[1])
            if len(replay) == 4:
                set_latch_state = self._mapper_set_latch_state
                if set_latch_state is not None:
                    set_latch_state((bool(replay[2]), bool(replay[3])))
            self.diagnostic_scanline_replays += 1
            return hit_cycle

        hit_cycle = self._render_scanline_fast_uncached(
            y,
            palette_parts,
            palette_colors,
            universal,
            background_context,
            nametable_signature,
        )
        latch_state = self._mapper_latch_state
        if latch_state is None:
            self._fast_frame_replay_lines[y] = (replay_key, hit_cycle)
        else:
            latch_0000_fe, latch_1000_fe = latch_state()
            self._fast_frame_replay_lines[y] = (
                replay_key,
                hit_cycle,
                latch_0000_fe,
                latch_1000_fe,
            )
        return hit_cycle

    def _render_scanline_fast_uncached(
        self,
        y: int,
        palette_parts: tuple[bytes, bytes] | None = None,
        palette_colors: tuple[tuple[int, int, int], ...] | None = None,
        universal: bytes | None = None,
        background_context: tuple[Mirroring, int, tuple] | None = None,
        nametable_signature: tuple | None = None,
    ) -> int:
        """Compose one scanline after the retained-frame replay misses."""
        # This renderer may populate presentation caches again. A later
        # memory or mapper change must therefore perform a fresh invalidation.
        self._fast_cache_invalidation_level = 0
        line_offset = y * PPU_WIDTH * 3
        line_end = line_offset + PPU_WIDTH * 3
        start_v = self.v
        background_palette = (
            self._fast_palette_part_signatures()[0]
            if palette_parts is None
            else palette_parts[0]
        )
        cache_key = (
            y,
            start_v,
            self.fine_x,
            self.ctrl,
            self.mask,
        )
        cacheable = not self._ppu_reads_have_side_effects
        if cacheable and y == 0:
            generation = self._fast_world_generation
            if generation == self._fast_world_frame_generation:
                self._fast_world_frame_repeats += 1
            else:
                self._fast_world_frame_generation = generation
                self._fast_world_frame_repeats = 1
            self._fast_world_cache_active = (
                self._fast_world_frame_repeats >= 2
                or self._fast_world_recurring_mapper_state
            )
            self._fast_world_recurring_mapper_state = False
            background_signature = (
                start_v,
                self.fine_x,
                self.ctrl,
                self.mask,
            )
            if background_signature == self._fast_background_frame_signature:
                self._fast_background_frame_repeats += 1
            else:
                # A changing scroll would generate 240 unique viewport copies
                # per frame and evict all of them before its 256-frame wrap.
                # Keep the underlying 512-pixel world rows, but admit viewport
                # rows only after a frame state has demonstrated reuse.
                self._fast_background_cache.clear()
                self._fast_background_cache_order.clear()
                self._fast_background_frame_signature = background_signature
                self._fast_background_frame_repeats = 1
            self._fast_background_cache_active = (
                self._fast_background_frame_repeats >= 2
            )
        background_cacheable = (
            cacheable and self._fast_background_cache_active
        )
        if cacheable and y == 0:
            composed_signature = (
                self._fast_sprite_generation,
                start_v,
                self.fine_x,
                self.ctrl,
                self.mask,
            )
            if composed_signature == self._fast_composed_frame_signature:
                self._fast_composed_frame_repeats += 1
            else:
                self._fast_scanline_cache.clear()
                self._fast_composed_frame_signature = composed_signature
                self._fast_composed_frame_repeats = 1
            self._fast_composed_cache_active = (
                self._fast_composed_frame_repeats >= 2
            )
        composed_cacheable = (
            cacheable and self._fast_composed_cache_active
        )
        latch_state = self._mapper_latch_state
        set_latch_state = self._mapper_set_latch_state
        latch_replay_capable = (
            not cacheable
            and latch_state is not None
            and set_latch_state is not None
        )
        if latch_replay_capable and y == 0:
            frame_signature = (
                start_v,
                self.fine_x,
                self.ctrl,
                self.mask,
                *latch_state(),
            )
            if frame_signature == self._fast_latched_frame_signature:
                self._fast_latched_frame_repeats += 1
            else:
                self._fast_latched_background_cache.clear()
                self._fast_latched_background_cache_order.clear()
                self._fast_latched_scanline_cache.clear()
                self._fast_latched_frame_signature = frame_signature
                self._fast_latched_frame_repeats = 1
            # A changing scroll never pays cache-population overhead. The
            # second identical frame populates entries and the third replays
            # them, which targets Mapper 9's static/slow-changing scenes.
            self._fast_latched_cache_active = (
                self._fast_latched_frame_repeats >= 2
            )
        latch_cacheable = (
            latch_replay_capable and self._fast_latched_cache_active
        )
        latched_cache_key = None
        if latch_cacheable:
            latch_0000_fe, latch_1000_fe = latch_state()
            latched_cache_key = cache_key + (
                latch_0000_fe,
                latch_1000_fe,
            )
            cached_latched_scanline = (
                self._fast_latched_scanline_cache.get(latched_cache_key)
            )
            if cached_latched_scanline is not None:
                (
                    pixels,
                    hit_cycle,
                    end_latch_0000_fe,
                    end_latch_1000_fe,
                ) = cached_latched_scanline
                self.framebuffer[line_offset:line_end] = pixels
                set_latch_state(
                    (end_latch_0000_fe, end_latch_1000_fe)
                )
                return hit_cycle
        cached_scanline = (
            self._fast_scanline_cache.get(cache_key)
            if composed_cacheable
            else None
        )
        if cached_scanline is not None:
            pixels, hit_cycle = cached_scanline
            self.framebuffer[line_offset:line_end] = pixels
            return hit_cycle

        if latch_cacheable:
            assert latched_cache_key is not None
            cached_latched_background = (
                self._fast_latched_background_cache.get(latched_cache_key)
            )
        else:
            cached_latched_background = None
        cached_background = (
            self._fast_background_cache.get(cache_key)
            if background_cacheable
            else None
        )
        if cached_latched_background is not None:
            (
                background_pixels,
                background_opaque,
                end_latch_0000_fe,
                end_latch_1000_fe,
            ) = cached_latched_background
            self.framebuffer[line_offset:line_end] = background_pixels
            set_latch_state(
                (end_latch_0000_fe, end_latch_1000_fe)
            )
        elif latch_cacheable:
            background_pixels, background_opaque = (
                self._render_background_scanline_fast(
                    start_v,
                    line_offset,
                    line_end,
                    background_palette,
                    universal,
                    background_context,
                    nametable_signature,
                )
            )
            end_latch_0000_fe, end_latch_1000_fe = latch_state()
            if (
                len(self._fast_latched_background_cache)
                >= FAST_SCANLINE_CACHE_LIMIT
            ):
                oldest = self._fast_latched_background_cache_order.popleft()
                self._fast_latched_background_cache.pop(oldest, None)
            self._fast_latched_background_cache[latched_cache_key] = (
                background_pixels,
                background_opaque,
                end_latch_0000_fe,
                end_latch_1000_fe,
            )
            self._fast_latched_background_cache_order.append(
                latched_cache_key
            )
        elif cached_background is None:
            background_pixels, background_opaque = (
                self._render_background_scanline_fast(
                    start_v,
                    line_offset,
                    line_end,
                    background_palette,
                    universal,
                    background_context,
                    nametable_signature,
                )
            )
            if background_cacheable:
                self._fast_background_cache[cache_key] = (
                    background_pixels,
                    background_opaque,
                )
                if len(self._fast_background_cache) > FAST_SCANLINE_CACHE_LIMIT:
                    oldest = self._fast_background_cache_order.popleft()
                    self._fast_background_cache.pop(oldest, None)
                self._fast_background_cache_order.append(cache_key)
        else:
            background_pixels, background_opaque = cached_background
            self.framebuffer[line_offset:line_end] = background_pixels
        hit_cycle = 0

        if not (self.mask & 0x10):
            if composed_cacheable:
                self._fast_scanline_cache[cache_key] = (
                    background_pixels,
                    hit_cycle,
                )
                if len(self._fast_scanline_cache) > FAST_SCANLINE_CACHE_LIMIT:
                    self._fast_scanline_cache.clear()
                    self._fast_scanline_cache[cache_key] = (
                        background_pixels,
                        hit_cycle,
                    )
            elif latch_cacheable:
                end_latch_0000_fe, end_latch_1000_fe = latch_state()
                self._fast_latched_scanline_cache[latched_cache_key] = (
                    background_pixels,
                    hit_cycle,
                    end_latch_0000_fe,
                    end_latch_1000_fe,
                )
                if (
                    len(self._fast_latched_scanline_cache)
                    > FAST_SCANLINE_CACHE_LIMIT
                ):
                    self._fast_latched_scanline_cache.clear()
            return hit_cycle
        if self._fast_sprite_lines is None:
            self._build_fast_sprite_lines()
        assert self._fast_sprite_lines is not None
        matches = self._fast_sprite_lines[y]
        if not matches:
            if composed_cacheable:
                self._fast_scanline_cache[cache_key] = (
                    background_pixels,
                    hit_cycle,
                )
                if len(self._fast_scanline_cache) > FAST_SCANLINE_CACHE_LIMIT:
                    self._fast_scanline_cache.clear()
                    self._fast_scanline_cache[cache_key] = (
                        background_pixels,
                        hit_cycle,
                    )
            elif latch_cacheable:
                end_latch_0000_fe, end_latch_1000_fe = latch_state()
                self._fast_latched_scanline_cache[latched_cache_key] = (
                    background_pixels,
                    hit_cycle,
                    end_latch_0000_fe,
                    end_latch_1000_fe,
                )
                if (
                    len(self._fast_latched_scanline_cache)
                    > FAST_SCANLINE_CACHE_LIMIT
                ):
                    self._fast_latched_scanline_cache.clear()
            return hit_cycle

        claimed = bytearray(PPU_WIDTH)
        pattern_read_pair = self._mapper_ppu_read_pair
        sprite_colors = (
            self._fast_palette_colors()
            if palette_colors is None
            else palette_colors
        )
        sprite_pattern_cache = self._fast_sprite_pattern_cache
        framebuffer = self.framebuffer
        show_left = bool(self.mask & 0x04)
        for index, sprite_x, attributes, address in matches:
            if self._direct_chr is not None:
                direct_chr = self._direct_chr
                low = direct_chr[address & 0x1FFF]
                high = direct_chr[(address + 8) & 0x1FFF]
            else:
                low, high = pattern_read_pair(address)
            pattern_key = low | (high << 8) | ((attributes & 0x40) << 10)
            visible_pixels = sprite_pattern_cache.get(pattern_key)
            if visible_pixels is None:
                if attributes & 0x40:
                    visible_pixels = tuple(
                        (offset << 2)
                        | ((low >> offset) & 1)
                        | (((high >> offset) & 1) << 1)
                        for offset in range(8)
                        if ((low | high) >> offset) & 1
                    )
                else:
                    visible_pixels = tuple(
                        (offset << 2)
                        | ((low >> (7 - offset)) & 1)
                        | (((high >> (7 - offset)) & 1) << 1)
                        for offset in range(8)
                        if ((low | high) >> (7 - offset)) & 1
                    )
                if (
                    len(sprite_pattern_cache)
                    >= FAST_SPRITE_PATTERN_CACHE_LIMIT
                ):
                    sprite_pattern_cache.clear()
                sprite_pattern_cache[pattern_key] = visible_pixels
            palette_base = 0x10 + (attributes & 3) * 4
            behind_background = bool(attributes & 0x20)
            sprite_zero = index == 0
            for packed_pixel in visible_pixels:
                offset = packed_pixel >> 2
                pixel = packed_pixel & 3
                x = sprite_x + offset
                if x >= PPU_WIDTH:
                    break
                if x < 8 and not show_left:
                    continue
                if (
                    sprite_zero
                    and not hit_cycle
                    and background_opaque[x]
                    and x < 255
                ):
                    hit_cycle = x + 1
                if claimed[x]:
                    continue
                claimed[x] = 1
                if behind_background and background_opaque[x]:
                    continue
                red, green, blue = sprite_colors[
                    palette_base + pixel
                ]
                destination = line_offset + x * 3
                framebuffer[destination] = red
                framebuffer[destination + 1] = green
                framebuffer[destination + 2] = blue
        if composed_cacheable:
            self._fast_scanline_cache[cache_key] = (
                bytes(self.framebuffer[line_offset:line_end]),
                hit_cycle,
            )
            if len(self._fast_scanline_cache) > FAST_SCANLINE_CACHE_LIMIT:
                self._fast_scanline_cache.clear()
                self._fast_scanline_cache[cache_key] = (
                    bytes(self.framebuffer[line_offset:line_end]),
                    hit_cycle,
                )
        elif latch_cacheable:
            end_latch_0000_fe, end_latch_1000_fe = latch_state()
            self._fast_latched_scanline_cache[latched_cache_key] = (
                bytes(self.framebuffer[line_offset:line_end]),
                hit_cycle,
                end_latch_0000_fe,
                end_latch_1000_fe,
            )
            if (
                len(self._fast_latched_scanline_cache)
                > FAST_SCANLINE_CACHE_LIMIT
            ):
                self._fast_latched_scanline_cache.clear()
        return hit_cycle

    def _prepare_fast_status_plan(self) -> None:
        """Probe sprite zero early enough to schedule its status transition."""
        key = (self.frame_number, self.scanline)
        if self._fast_sprite_hit_plan_key == key:
            return
        self._fast_sprite_hit_plan_key = key
        self._fast_sprite_hit_cycle = (
            self._probe_fast_sprite_zero_hit_cycle()
        )

    def _probe_fast_sprite_zero_hit_cycle(self) -> int:
        """Inspect only sprite zero and its eight background pixels.

        Status timing must be known before dot 256, but presentation does not
        need the other 63 sprites yet. Keeping this probe separate prevents a
        later mapper write from turning one speculative status calculation
        into another full scanline composition.
        """
        y = self.scanline
        if (
            not 0 <= y < PPU_HEIGHT
            or self._ppu_reads_have_side_effects
            or (self.mask & 0x18) != 0x18
            or not self._fast_sprite_zero_candidate_cycle()
        ):
            return 0
        self.diagnostic_status_probes += 1

        line_offset = y * PPU_WIDTH * 3
        line_end = line_offset + PPU_WIDTH * 3
        _pixels, background_opaque = (
            self._render_background_scanline_fast(
                self.v,
                line_offset,
                line_end,
            )
        )

        sprite_y = self.oam[0] + 1
        tile = self.oam[1]
        attributes = self.oam[2]
        sprite_x = self.oam[3]
        height = 16 if self.ctrl & 0x20 else 8
        row = y - sprite_y
        if attributes & 0x80:
            row = height - 1 - row
        if height == 16:
            table_base = (tile & 1) * 0x1000
            tile &= 0xFE
            if row >= 8:
                tile += 1
                row -= 8
        else:
            table_base = 0x1000 if self.ctrl & 0x08 else 0
        address = table_base + tile * 16 + row
        low = self._fast_pattern_read(address)
        high = self._fast_pattern_read(address + 8)

        for offset in range(8):
            x = sprite_x + offset
            if x >= 255:
                break
            if x < 8 and not (self.mask & 0x04):
                continue
            bit = offset if attributes & 0x40 else 7 - offset
            sprite_pixel = (
                ((low >> bit) & 1)
                | (((high >> bit) & 1) << 1)
            )
            if sprite_pixel and background_opaque[x]:
                return x + 1
        return 0

    def _fast_sprite_zero_candidate_cycle(self) -> int:
        """Return the earliest dot where sprite zero could hit this line.

        The optimized renderer used to compose the entire current scanline
        every time the scheduler asked for its next status transition.
        Mapper writes invalidate that speculative result, so MMC3 games that
        change CHR banks during a scanline could render the same line hundreds
        of times. OAM already tells us whether sprite zero intersects the line
        and its earliest X position; defer composition until that dot.
        """
        scanline = self.scanline
        if not 0 <= scanline < PPU_HEIGHT:
            return 0
        sprite_y = self.oam[0] + 1
        height = 16 if self.ctrl & 0x20 else 8
        if not sprite_y <= scanline < sprite_y + height:
            return 0
        sprite_x = self.oam[3]
        if sprite_x >= 255:
            return 0
        # When either left-edge gate is closed, neither layer can participate
        # in a hit before pixel 8 even if sprite zero starts farther left.
        first_x = sprite_x
        if not (self.mask & 0x02) or not (self.mask & 0x04):
            first_x = max(8, first_x)
        if first_x >= min(PPU_WIDTH, sprite_x + 8):
            return 0
        return first_x + 1

    def _fast_status_cycles(self, rendering: bool) -> tuple[int, int]:
        """Return pending overflow and sprite-zero transitions for this line."""
        if not rendering or not 0 <= self.scanline < PPU_HEIGHT:
            return 0, 0
        overflow_cycle = (
            0
            if self.status & 0x20
            else self._fast_overflow_cycle(self.scanline)
        )
        hit_cycle = 0
        if not (self.status & 0x40) and (self.mask & 0x18) == 0x18:
            candidate_cycle = self._fast_sprite_zero_candidate_cycle()
            if candidate_cycle:
                key = (self.frame_number, self.scanline)
                if self._fast_sprite_hit_plan_key == key:
                    hit_cycle = self._fast_sprite_hit_cycle
                elif self.cycle < candidate_cycle:
                    # This conservative event is cheap and cannot assert the
                    # flag by itself. Crossing it triggers one composition
                    # with the mapper state valid at the candidate pixel.
                    hit_cycle = candidate_cycle
                else:
                    self._prepare_fast_status_plan()
                    hit_cycle = self._fast_sprite_hit_cycle
        return overflow_cycle, hit_cycle

    def _step_fast_visible_lines(self, line_count: int) -> int:
        """Advance complete rendered lines without rebuilding the dot plan.

        The device scheduler admits this path only when no CPU-visible access
        occurs inside the span. MMC3 address phases, sprite flags, scroll
        transfers, framebuffer composition, and their exact clocks remain in
        hardware order for every line.
        """
        lines = min(int(line_count), PPU_HEIGHT - self.scanline)
        if lines <= 0:
            return 0

        render_line = self._render_scanline_fast
        palette_parts = self._fast_palette_part_signatures()
        palette_colors = self._fast_palette_colors()
        universal = bytes(palette_colors[0])
        if self._ppu_reads_have_side_effects:
            background_context = None
        else:
            pattern_base = 0x1000 if self.ctrl & 0x10 else 0
            background_context = (
                self.mapper.mirroring,
                pattern_base,
                self._mapper_ppu_pattern_cache_token(pattern_base),
            )
        overflow_cycle = self._fast_overflow_cycle
        candidate_cycle = self._fast_sprite_zero_candidate_cycle
        prepare_hit = self._prepare_fast_status_plan
        increment_y = self._increment_y
        copy_x = self._copy_x
        mapper = self._mmc3_mapper
        mapper_clock_irq = None if mapper is None else mapper._clock_irq
        background_high = bool(self.ctrl & 0x10)
        sprite_high = bool(self.ctrl & 0x08)
        scanline = self.scanline
        master_clock = self.master_clock
        status = self.status
        mask_has_hit_layers = (self.mask & 0x18) == 0x18

        for _ in range(lines):
            line_clock = master_clock
            self.scanline = scanline
            self.master_clock = line_clock
            self.status = status
            if mapper is not None:
                if background_high:
                    if (
                        not mapper.last_a12
                        and line_clock + 4 - mapper.a12_low_since >= 9
                    ):
                        assert mapper_clock_irq is not None
                        mapper_clock_irq(line_clock + 4)
                    mapper.last_a12 = True
                elif mapper.last_a12:
                    mapper.a12_low_since = line_clock + 4
                    mapper.last_a12 = False

            if not status & 0x20 and overflow_cycle(scanline):
                status |= 0x20
            if not status & 0x40 and mask_has_hit_layers:
                if candidate_cycle():
                    prepare_hit()
                    if self._fast_sprite_hit_cycle:
                        status |= 0x40
            self.status = status

            rendered_hit = render_line(
                scanline,
                palette_parts,
                palette_colors,
                universal,
                background_context,
            )
            if rendered_hit and not status & 0x40:
                status |= 0x40
            increment_y()
            copy_x()
            if mapper is not None:
                if sprite_high:
                    if (
                        not mapper.last_a12
                        and line_clock + 260 - mapper.a12_low_since >= 9
                    ):
                        assert mapper_clock_irq is not None
                        mapper_clock_irq(line_clock + 260)
                    mapper.last_a12 = True
                elif mapper.last_a12:
                    mapper.a12_low_since = line_clock + 260
                    mapper.last_a12 = False

                if background_high:
                    if (
                        not mapper.last_a12
                        and line_clock + 324 - mapper.a12_low_since >= 9
                    ):
                        assert mapper_clock_irq is not None
                        mapper_clock_irq(line_clock + 324)
                    mapper.last_a12 = True
                elif mapper.last_a12:
                    mapper.a12_low_since = line_clock + 324
                    mapper.last_a12 = False

            master_clock += 341
            scanline += 1

        self.status = status
        self.master_clock = master_clock
        self.scanline = scanline
        if scanline == PPU_HEIGHT:
            self.frame_complete = True
            self.frame_number += 1
        return lines * 341

    def step_fast(self, dots: int) -> None:
        """Advance between CPU-visible PPU events without visiting every dot."""
        remaining = int(dots)
        if remaining <= 0:
            return

        # During a forced-blank setup screen, the common instruction-sized
        # span has no rendering, sprite-status, odd-frame, or mapper-fetch
        # event. Punch-Out streams setup data through thousands of such spans;
        # avoid the full rendering-event planner until a line is actually
        # crossed or a delayed PPUMASK write is due.
        if (
            not self._rendering_active
            and not (self.mask & 0x18)
            and self._rendering_change_clock is None
            and not (
                self.scanline in (241, 261)
                and self.cycle < 1
            )
        ):
            new_cycle = self.cycle + remaining
            if new_cycle < 341:
                self.cycle = new_cycle
                self.master_clock += remaining
                return

        # Most instruction-sized advances do not reach a scanline event. This
        # branch avoids setting up the general event loop in that common case.
        prerender = self.scanline == 261
        rendering = self.rendering_enabled
        line_length = self._line_length(prerender, rendering)
        old_cycle = self.cycle
        new_cycle = old_cycle + remaining
        overflow_cycle, hit_cycle = self._fast_status_cycles(rendering)
        if new_cycle < line_length:
            rendering_change = self.dots_to_rendering_change()
            event_crossed = (
                rendering_change is not None
                and rendering_change <= remaining
            )
            if 0 <= self.scanline < 240 and rendering:
                event_crossed = event_crossed or (
                    (
                        self._mmc3_mapper is not None
                        and old_cycle < 4 <= new_cycle
                    )
                    or (
                        overflow_cycle
                        and old_cycle < overflow_cycle <= new_cycle
                    )
                    or (
                        hit_cycle
                        and old_cycle < hit_cycle <= new_cycle
                    )
                    or old_cycle < 256 <= new_cycle
                    or old_cycle < 257 <= new_cycle
                    or old_cycle < 260 <= new_cycle
                    or (
                        self._mmc3_mapper is not None
                        and old_cycle < 324 <= new_cycle
                    )
                )
            elif prerender:
                event_crossed = (
                    event_crossed or old_cycle < 1 <= new_cycle
                )
                if rendering:
                    event_crossed = event_crossed or (
                        old_cycle < 256 <= new_cycle
                        or old_cycle < 257 <= new_cycle
                        or old_cycle < 304 <= new_cycle
                        or (
                            self._mmc3_mapper is not None
                            and (
                                old_cycle < 4 <= new_cycle
                                or old_cycle < 260 <= new_cycle
                                or old_cycle < 324 <= new_cycle
                            )
                        )
                    )
            elif self.scanline == 241:
                event_crossed = (
                    event_crossed or old_cycle < 1 <= new_cycle
                )
            if not event_crossed:
                self.cycle = new_cycle
                self.master_clock += remaining
                return

        while remaining > 0:
            prerender = self.scanline == 261
            if (
                self.cycle == 0
                and 0 <= self.scanline < PPU_HEIGHT
                and rendering
                and self._rendering_change_clock is None
                and remaining >= 341
                and not self._disable_fast_visible_line_batch
            ):
                advanced = self._step_fast_visible_lines(
                    min(
                        remaining // 341,
                        PPU_HEIGHT - self.scanline,
                    )
                )
                remaining -= advanced
                continue
            line_length = self._line_length(prerender, rendering)
            advance = min(remaining, line_length - self.cycle)
            rendering_change = self.dots_to_rendering_change()
            if rendering_change is not None and rendering_change > 0:
                advance = min(advance, rendering_change)
            old_cycle = self.cycle
            new_cycle = old_cycle + advance
            event_clock_base = self.master_clock
            overflow_cycle, hit_cycle = self._fast_status_cycles(rendering)

            if 0 <= self.scanline < 240 and rendering:
                if (
                    overflow_cycle
                    and old_cycle < overflow_cycle <= new_cycle
                ):
                    self.status |= 0x20
                if hit_cycle and old_cycle < hit_cycle <= new_cycle:
                    key = (self.frame_number, self.scanline)
                    if self._fast_sprite_hit_plan_key != key:
                        self._prepare_fast_status_plan()
                    actual_hit = self._fast_sprite_hit_cycle
                    if actual_hit and actual_hit <= new_cycle:
                        self.status |= 0x40
                if old_cycle < 4 <= new_cycle:
                    event_clock = event_clock_base + (4 - old_cycle)
                    self._notify_mmc3_scanline_bus(4, event_clock)
                if old_cycle < 256 <= new_cycle:
                    rendered_hit_cycle = self._render_scanline_fast(
                        self.scanline
                    )
                    # Latch-based CHR mappers cannot be rendered ahead of
                    # their fetch sequence. Preserve their mapping side
                    # effects and expose a hit no later than line completion.
                    if (
                        rendered_hit_cycle
                        and not (self.status & 0x40)
                        and rendered_hit_cycle <= new_cycle
                    ):
                        self.status |= 0x40
                    self._increment_y()
                if old_cycle < 257 <= new_cycle:
                    self._copy_x()
                if old_cycle < 260 <= new_cycle:
                    event_clock = event_clock_base + (260 - old_cycle)
                    self._notify_mmc3_scanline_bus(260, event_clock)
                if old_cycle < 324 <= new_cycle:
                    event_clock = event_clock_base + (324 - old_cycle)
                    self._notify_mmc3_scanline_bus(324, event_clock)
            elif prerender:
                if old_cycle < 1 <= new_cycle:
                    self.status &= ~0xE0
                    self._update_nmi()
                if rendering:
                    if old_cycle < 4 <= new_cycle:
                        event_clock = event_clock_base + (4 - old_cycle)
                        self._notify_mmc3_scanline_bus(4, event_clock)
                    if old_cycle < 256 <= new_cycle:
                        self._increment_y()
                    if old_cycle < 257 <= new_cycle:
                        self._copy_x()
                    if old_cycle < 260 <= new_cycle:
                        event_clock = event_clock_base + (260 - old_cycle)
                        self._notify_mmc3_scanline_bus(260, event_clock)
                    if old_cycle < 304 <= new_cycle:
                        self._copy_y()
                    if old_cycle < 324 <= new_cycle:
                        event_clock = event_clock_base + (324 - old_cycle)
                        self._notify_mmc3_scanline_bus(324, event_clock)
            elif self.scanline == 241 and old_cycle < 1 <= new_cycle:
                if self.suppress_vblank:
                    self.suppress_vblank = False
                else:
                    self.status |= 0x80
                    self._update_nmi(
                        event_clock_base + (1 - old_cycle)
                    )

            self.cycle = new_cycle
            self.master_clock += advance
            remaining -= advance
            rendering_change = self._rendering_change_clock
            if (
                rendering_change is not None
                and self.master_clock >= rendering_change
            ):
                self._apply_rendering_change()
                rendering = self._rendering_active
            if self.cycle < line_length:
                continue

            self.cycle = 0
            self.scanline += 1
            if self.scanline == 240:
                self.frame_complete = True
                self.frame_number += 1
            if self.scanline >= 262:
                self.scanline = 0
                self.odd_frame = not self.odd_frame

    def step(self) -> None:
        self.step_accurate(1)

    def step_accurate(self, dots: int) -> None:
        """Advance the dot pipeline in one Python loop."""
        for _ in range(int(dots)):
            visible = 0 <= self.scanline < 240
            prerender = self.scanline == 261
            rendering = self.rendering_enabled

            if visible and self.cycle == 0:
                self._accurate_sprite_overflow_cycle = 0
            if visible and rendering:
                if self.cycle == 65 and not (self.status & 0x20):
                    self._accurate_sprite_overflow_cycle = (
                        self._sprite_overflow_cycle(self.scanline)
                    )
                if (
                    self._accurate_sprite_overflow_cycle
                    and self.cycle
                    == self._accurate_sprite_overflow_cycle
                ):
                    self.status |= 0x20

            if visible and 1 <= self.cycle <= 256:
                self._render_pixel()

            if (visible or prerender) and rendering:
                if self.cycle in (260, 324):
                    self._notify_mmc3_scanline_bus(
                        self.cycle,
                        # The dot loop is entered at the beginning of the
                        # address phase; MMC3 sees the settled A12 level one
                        # PPU tick later.
                        self.master_clock + 1,
                    )
                if (2 <= self.cycle <= 257) or (322 <= self.cycle <= 337):
                    self._shift_background()
                if (1 <= self.cycle <= 256) or (321 <= self.cycle <= 336):
                    self._fetch_background()
                if self.cycle == 256:
                    self._increment_y()
                elif self.cycle == 257:
                    self._load_background()
                    self._copy_x()
                    next_line = 0 if prerender else self.scanline + 1
                    self._evaluate_sprites(next_line)
                elif prerender and 280 <= self.cycle <= 304:
                    self._copy_y()
                elif self.cycle in (338, 340):
                    self.next_tile_id = self.read_memory(
                        0x2000 | (self.v & 0x0FFF)
                    )

            # The shifters now contain the pixel that will be emitted on the
            # following dot. Latching sprite zero here exposes the overlap at
            # the same CPU-visible phase as the scanline-batched renderer,
            # while the framebuffer itself remains rendered on dots 1-256.
            if (
                visible
                and rendering
                and 0 <= self.cycle < 255
                and self._sprite_zero_overlaps(self.cycle)
            ):
                self.status |= 0x40

            if prerender and self.cycle == 0:
                # The dot loop labels work by the phase being entered, while
                # CPU register reads observe the phase just completed. Move
                # the two sprite latches to the preceding internal phase so
                # their externally visible clear remains hardware dot 1.
                self.status &= ~0x60
            if self.scanline == 241 and self.cycle == 1:
                if self.suppress_vblank:
                    self.suppress_vblank = False
                else:
                    self.status |= 0x80
                    self._update_nmi()
            elif prerender and self.cycle == 1:
                self.status &= ~0x80
                self._update_nmi()

            if self.scanline == 240 and self.cycle == 0:
                self.frame_complete = True
                self.frame_number += 1

            self.master_clock += 1
            if (
                prerender
                and self.cycle == 339
                and self.odd_frame
                and rendering
            ):
                self.cycle = 0
                self.scanline = 0
                self.odd_frame = False
                continue

            self.cycle += 1
            if self.cycle >= 341:
                self.cycle = 0
                self.scanline += 1
                if self.scanline >= 262:
                    self.scanline = 0
                    self.odd_frame = not self.odd_frame

    def take_frame(self, *, copy: bool = True) -> bytes | bytearray:
        self.frame_complete = False
        return bytes(self.framebuffer) if copy else self.framebuffer

    def get_state(self) -> dict:
        return {
            "nametable_ram": bytes(self.nametable_ram),
            "palette_ram": bytes(self.palette_ram),
            "oam": bytes(self.oam),
            "framebuffer": bytes(self.framebuffer),
            "ctrl": self.ctrl,
            "mask": self.mask,
            "rendering_active": self._rendering_active,
            "rendering_target": self._rendering_target,
            "rendering_change_clock": self._rendering_change_clock,
            "status": self.status,
            "oam_addr": self.oam_addr,
            "open_bus": self.open_bus,
            "open_bus_deadlines": list(self._open_bus_deadlines),
            "read_buffer": self.read_buffer,
            "v": self.v,
            "t": self.t,
            "fine_x": self.fine_x,
            "write_toggle": self.write_toggle,
            "bg_pattern_low": self.bg_pattern_low,
            "bg_pattern_high": self.bg_pattern_high,
            "bg_attribute_low": self.bg_attribute_low,
            "bg_attribute_high": self.bg_attribute_high,
            "next_tile_id": self.next_tile_id,
            "next_tile_attribute": self.next_tile_attribute,
            "next_tile_low": self.next_tile_low,
            "next_tile_high": self.next_tile_high,
            "sprites": self.sprites,
            "scanline": self.scanline,
            "cycle": self.cycle,
            "master_clock": self.master_clock,
            "odd_frame": self.odd_frame,
            "frame_complete": self.frame_complete,
            "frame_number": self.frame_number,
            "nmi_line": self.nmi_line,
            "nmi_pending": self.nmi_pending,
            "nmi_pending_clock": self.nmi_pending_clock,
            "nmi_pending_cancellable": self.nmi_pending_cancellable,
            "suppress_vblank": self.suppress_vblank,
        }

    def set_state(self, state: dict) -> None:
        self.nametable_ram[:] = bytes(state["nametable_ram"])
        self.palette_ram[:] = bytes(state["palette_ram"])
        self.oam[:] = bytes(state["oam"])
        framebuffer = bytes(state["framebuffer"])
        if len(framebuffer) != len(self.framebuffer):
            raise ValueError("save state has an incompatible frame buffer")
        self.framebuffer[:] = framebuffer
        scalar_names = (
            "ctrl", "mask", "status", "oam_addr", "open_bus", "read_buffer",
            "v", "t", "fine_x", "bg_pattern_low", "bg_pattern_high",
            "bg_attribute_low", "bg_attribute_high", "next_tile_id",
            "next_tile_attribute", "next_tile_low", "next_tile_high",
            "scanline", "cycle", "master_clock", "frame_number",
        )
        for name in scalar_names:
            setattr(self, name, int(state[name]))
        self._fast_background_mapper_token = (
            self._background_mapper_dependency()
        )
        self._fast_sprite_mapper_token = self._sprite_mapper_dependency()
        self._fast_background_mapper_generation = 0
        self._fast_sprite_mapper_generation = 0
        self._open_bus_deadlines = [
            int(value)
            for value in state.get(
                "open_bus_deadlines",
                [self.master_clock + OPEN_BUS_DECAY_DOTS] * 8,
            )
        ]
        boolean_names = (
            "write_toggle", "odd_frame", "frame_complete", "nmi_line",
            "nmi_pending", "suppress_vblank",
        )
        for name in boolean_names:
            setattr(self, name, bool(state.get(name, False)))
        pending_clock = state.get("nmi_pending_clock")
        self.nmi_pending_clock = (
            None if pending_clock is None else int(pending_clock)
        )
        self.nmi_pending_cancellable = bool(
            state.get("nmi_pending_cancellable", False)
        )
        mask_rendering = bool(self.mask & 0x18)
        self._rendering_active = bool(
            state.get("rendering_active", mask_rendering)
        )
        self._rendering_target = bool(
            state.get("rendering_target", mask_rendering)
        )
        rendering_change_clock = state.get("rendering_change_clock")
        self._rendering_change_clock = (
            None
            if rendering_change_clock is None
            else int(rendering_change_clock)
        )
        self.sprites = [tuple(int(item) for item in sprite) for sprite in state["sprites"]]
        self._fast_row_cache.clear()
        self._fast_row_cache_order.clear()
        self._fast_background_cache.clear()
        self._fast_background_cache_order.clear()
        self._fast_background_frame_signature = None
        self._fast_background_frame_repeats = 0
        self._fast_background_cache_active = False
        self._fast_latched_background_cache.clear()
        self._fast_latched_background_cache_order.clear()
        self._fast_latched_scanline_cache.clear()
        self._fast_latched_fetch_plan_cache.clear()
        self._fast_latched_fetch_plan_cache_order.clear()
        self._fast_viewport_tile_run_cache.clear()
        self._fast_viewport_tile_run_cache_order.clear()
        self._fast_latched_tile_run_cache.clear()
        self._fast_latched_tile_run_cache_order.clear()
        self._fast_latched_frame_signature = None
        self._fast_latched_frame_repeats = 0
        self._fast_latched_cache_active = False
        self._fast_background_world_cache.clear()
        self._fast_world_admission_states.clear()
        self._fast_world_generation += 1
        self._fast_nametable_row_versions = [0] * (4 * 30)
        self._fast_attribute_row_versions = [0] * (4 * 8)
        self._fast_nametable_generation = 0
        self._fast_latched_replay_signature_cache.clear()
        self._fast_replay_fallback_generation += 1
        self._fast_chr_generation += 1
        self._fast_world_frame_generation = -1
        self._fast_world_frame_repeats = 0
        self._fast_world_cache_active = False
        self._fast_world_mapper_tokens_seen.clear()
        self._fast_world_recurring_mapper_state = False
        self._fast_scanline_cache.clear()
        self._fast_sprite_generation = 0
        self._fast_composed_frame_signature = None
        self._fast_composed_frame_repeats = 0
        self._fast_composed_cache_active = False
        self._fast_palette_cache.clear()
        self._fast_sprite_lines = None
        self._fast_sprite_overflow_cycles = [-1] * PPU_HEIGHT
        self._fast_sprite_overflow_candidates = None
        self._fast_sprite_hit_plan_key = None
        self._fast_sprite_hit_cycle = 0
        self._fast_frame_replay_lines = [None] * PPU_HEIGHT
        self.diagnostic_scanline_replays = 0
        self._accurate_sprite_overflow_cycle = 0
        self._fast_cache_invalidation_level = 2
