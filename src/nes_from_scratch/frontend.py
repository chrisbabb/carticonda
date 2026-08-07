"""Pixel-art Pygame launcher, in-game menus, input, video, and audio."""

from __future__ import annotations

import gc
import os
import platform
import sys
import time
from array import array
from collections import deque
from pathlib import Path

from . import __version__
from .audio_stream import PcmBuffer
from .cartridge import CartridgeError
from .console import Console
from .constants import (
    Button,
    DEFAULT_SAMPLE_RATE,
    NTSC_FRAME_RATE,
    PPU_HEIGHT,
    PPU_WIDTH,
)
from .emulation_worker import (
    DEFAULT_QUEUE_DEPTH,
    EmulatedFrame,
    EmulationWorker,
)
from .mappers import MapperError
from .rom_loader import (
    ArchiveRom,
    ArchiveSelectionRequired,
    RomLoadError,
    load_rom,
)
from .rewind import RewindBuffer
from .state import StateError
from .ui_model import (
    DEFAULT_GAMEPAD_BINDINGS,
    DEFAULT_KEY_BINDINGS,
    WINDOW_SCALES,
    BrowserEntry,
    FrontendSettings,
    StateSlotInfo,
    browser_entries,
    browser_roots,
    state_slots,
)


UI_WIDTH = 960
UI_HEIGHT = 720
# SDL_mixer can hold one playing Sound and one queued Sound per channel. Two
# 2,048-sample chunks therefore keep about 93 ms of 44.1 kHz PCM inside the
# native device, enough to bridge the rare long Windows scheduler spike seen
# in field diagnostics. Keeping two chunks of prebuffer preserves the existing
# 4,096-sample startup reservoir instead of adding input-to-audio latency.
AUDIO_CHUNK_SAMPLES = 2048
AUDIO_PREBUFFER_CHUNKS = 2
AUDIO_MIXER_BUFFER_SAMPLES = 2048
AUDIO_PUMP_INTERVAL = 0.002
# Channel.get_busy()/get_queue() are not used to decide when to submit PCM.
# Field reports showed both misreporting for an entire session on some
# systems regardless of SDL audio driver (WASAPI, DirectSound, WinMM all
# affected) -- e.g. get_busy() staying False for thousands of consecutive
# polls while audio genuinely played, and get_queue() never reporting an
# empty slot again even immediately after Channel.stop(). Submission is
# instead paced from playback duration (chunk_samples / sample_rate) tracked
# locally in ``audio_inflight``. A gap is declared only when the buffer
# itself has too little pending PCM to fill a slot our own clock believes is
# free, for at least this long.
AUDIO_IDLE_GRACE_SECONDS = 0.012
# If both SDL_mixer slots genuinely drain, fade the first recovered emulated
# PCM back in. Never inject a synthetic Sound after a gap: doing so arrives too
# late, increases latency, and can race SDL's queued-Sound promotion.
AUDIO_CONCEALMENT_SAMPLES = 256
PACING_SPIN_SECONDS = 0.00035
PACING_MICRO_SLEEP_SECONDS = 0.00008
PACING_LATE_THRESHOLD = 0.001
PACING_DRAW_GUARD_SECONDS = 0.00020
# Let the pure-Python core finish its usual 8-10 ms frame in two interpreter
# quanta. A forced 1 ms quantum caused repeated GIL handoffs with the 2 ms host
# audio pump, nearly doubling p95 core time on Windows-class pacing workloads.
# The bounded frame and PCM reservoirs keep the main thread responsive while
# the ordinary 5 ms CPython quantum removes that self-inflicted jitter.
EMULATION_THREAD_SWITCH_SECONDS = 0.005
DIAGNOSTIC_BUCKET_MS = 0.25
DIAGNOSTIC_BUCKETS = 256
BRAND_NAME = "Cartaconda"
BRAND_TAGLINE = "Python-powered 8-bit emulation"
BRAND_LOGO_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "cartaconda-logo.png"
)

# Original NES-era palette: hardware greys and red controls, with the compact,
# high-contrast accent colors common to 8-bit game menus.
INK = (24, 24, 32)
CREAM = (248, 232, 184)
PAPER = (255, 248, 216)
CORAL = (224, 48, 56)
CORAL_DARK = (144, 32, 48)
PINK = (248, 136, 112)
MINT = (104, 200, 112)
MINT_DARK = (32, 120, 80)
LAVENDER = (112, 104, 176)
SUN = (248, 200, 72)
SKY = (72, 168, 216)
NAVY = (16, 24, 48)
MUTED = (96, 88, 96)
WHITE = (255, 251, 231)
PIXEL_BLUE = (32, 56, 96)
PIXEL_GREY = (152, 152, 144)
PIXEL_LIGHT = (216, 216, 200)
PIXEL_SHADOW = (8, 8, 16)


_BUTTON_BY_ACTION_SUFFIX = {
    "a": Button.A,
    "b": Button.B,
    "select": Button.SELECT,
    "start": Button.START,
    "up": Button.UP,
    "down": Button.DOWN,
    "left": Button.LEFT,
    "right": Button.RIGHT,
}
_DEFAULT_GAMEPAD_SOURCE_MASKS = {
    source: int(_BUTTON_BY_ACTION_SUFFIX[action.split("_", 1)[1]])
    for action, sources in DEFAULT_GAMEPAD_BINDINGS.items()
    if action.startswith("p1_")
    for source in sources
}


class _PresentationPacer:
    """Place every display flip on a steady fractional-NTSC deadline.

    Emulation variance is absorbed by the bounded worker queue, so wall-clock
    lateness must never alter the emulated rate.  Deadlines remain anchored to
    the exact NES frame period.  An explicit segment reset handles menus,
    pauses, display-mode changes, and other non-gameplay gaps.
    """

    __slots__ = (
        "draw_estimate",
        "presented_frames",
        "presentation_skips",
        "skip_streak",
        "max_skip_streak",
        "timing_debt",
        "last_presented_at",
        "presentation_intervals",
        "presentation_jitter_seconds",
        "worst_presentation_jitter",
        "presentation_jitter_histogram",
    )

    def __init__(self) -> None:
        self.draw_estimate = 0.003
        self.presented_frames = 0
        # Retain these diagnostic fields so logs remain comparable with 0.9.0.
        # The exact-NTSC scheduler never increments them.
        self.presentation_skips = 0
        self.skip_streak = 0
        self.max_skip_streak = 0
        self.timing_debt = 0.0
        self.last_presented_at: float | None = None
        self.presentation_intervals = 0
        self.presentation_jitter_seconds = 0.0
        self.worst_presentation_jitter = 0.0
        self.presentation_jitter_histogram = [0] * DIAGNOSTIC_BUCKETS

    def reset_segment(self) -> None:
        """Exclude a pause or discontinuity from gameplay timing statistics."""
        self.last_presented_at = None
        self.skip_streak = 0
        self.timing_debt = 0.0

    def draw_deadline(self, frame_deadline: float, *, gameplay: bool) -> float:
        if not gameplay:
            return frame_deadline
        lead = min(
            0.012,
            max(
                0.0005,
                self.draw_estimate + PACING_DRAW_GUARD_SECONDS,
            ),
        )
        return frame_deadline - lead

    def record_present(
        self,
        seconds: float,
        *,
        completed_at: float | None = None,
    ) -> None:
        sample = min(0.012, max(0.00025, float(seconds)))
        # Respond quickly when a draw becomes more expensive, then decay the
        # lead conservatively so one cheap frame does not make the next flip
        # late.
        response = 0.5 if sample > self.draw_estimate else 0.0625
        self.draw_estimate += (sample - self.draw_estimate) * response
        self.presented_frames += 1
        self.skip_streak = 0
        if completed_at is None:
            return
        previous = self.last_presented_at
        self.last_presented_at = completed_at
        if previous is None:
            return
        frame_period = 1.0 / NTSC_FRAME_RATE
        interval = completed_at - previous
        # A debugger break, system sleep, display-mode switch, or OS suspend is
        # a segment boundary rather than a 16-second gameplay-jitter sample.
        if interval <= 0 or interval > frame_period * 3:
            return
        jitter = abs(interval - frame_period)
        self.presentation_intervals += 1
        self.presentation_jitter_seconds += jitter
        self.worst_presentation_jitter = max(
            self.worst_presentation_jitter,
            jitter,
        )
        bucket = min(
            DIAGNOSTIC_BUCKETS - 1,
            int(jitter * 1000 / DIAGNOSTIC_BUCKET_MS),
        )
        self.presentation_jitter_histogram[bucket] += 1

    def schedule_next(
        self,
        completed_at: float,
        frame_deadline: float,
        frame_period: float,
    ) -> float:
        """Advance one exact NES period without changing emulation speed."""
        lateness = max(0.0, completed_at - frame_deadline)
        if lateness > frame_period * 3:
            self.reset_segment()
            return completed_at + frame_period
        self.timing_debt = 0.0
        return frame_deadline + frame_period

    def jitter_percentile(self, percentile: float) -> float:
        if self.presentation_intervals <= 0:
            return 0.0
        target = max(
            1,
            int(self.presentation_intervals * percentile + 0.999999),
        )
        accumulated = 0
        for index, count in enumerate(self.presentation_jitter_histogram):
            accumulated += count
            if accumulated >= target:
                return (index + 1) * DIAGNOSTIC_BUCKET_MS
        return DIAGNOSTIC_BUCKETS * DIAGNOSTIC_BUCKET_MS


class _GamepadState:
    """Pure-Python controller state fed by SDL joystick events.

    Keeping gameplay input in this object avoids querying an SDL joystick
    handle dozens of times per frame. It also ensures that a device removal is
    handled before any further native read can race the disconnected handle.
    """

    __slots__ = (
        "handle",
        "instance_id",
        "raw_buttons",
        "active_sources",
    )

    def __init__(self, handle, instance_id: int) -> None:
        self.handle = handle
        self.instance_id = int(instance_id)
        self.raw_buttons = 0
        self.active_sources: set[str] = set()

    def clear(self) -> None:
        self.raw_buttons = 0
        self.active_sources.clear()

    def set_button(self, index: int, pressed: bool) -> None:
        if not 0 <= index < 64:
            return
        bit = 1 << index
        source = f"button:{index}"
        if pressed:
            self.raw_buttons |= bit
            self.active_sources.add(source)
        else:
            self.raw_buttons &= ~bit
            self.active_sources.discard(source)

    def set_axis(self, index: int, value: float) -> None:
        if not 0 <= index < 16:
            return
        negative = f"axis:{index}:-"
        positive = f"axis:{index}:+"
        self.active_sources.discard(negative)
        self.active_sources.discard(positive)
        if value < -0.5:
            self.active_sources.add(negative)
        elif value > 0.5:
            self.active_sources.add(positive)

    def set_hat(self, index: int, value: tuple[int, int]) -> None:
        if not 0 <= index < 16:
            return
        for direction in ("up", "down", "left", "right"):
            self.active_sources.discard(f"hat:{index}:{direction}")
        horizontal, vertical = int(value[0]), int(value[1])
        if horizontal < 0:
            self.active_sources.add(f"hat:{index}:left")
        elif horizontal > 0:
            self.active_sources.add(f"hat:{index}:right")
        if vertical < 0:
            self.active_sources.add(f"hat:{index}:down")
        elif vertical > 0:
            self.active_sources.add(f"hat:{index}:up")

    def mapped_buttons(self, source_masks: dict[str, int]) -> int:
        buttons = 0
        for source in self.active_sources:
            buttons |= source_masks.get(source, 0)
        return buttons

    @property
    def nes_buttons(self) -> int:
        """Compatibility view using Cartaconda's default controller layout."""
        return self.mapped_buttons(_DEFAULT_GAMEPAD_SOURCE_MASKS)

    @property
    def menu_chord(self) -> bool:
        buttons = self.nes_buttons
        return bool(buttons & int(Button.SELECT) and buttons & int(Button.START))


CONTROL_ROWS = (
    ("p1_a", "A Button", 0, Button.A),
    ("p1_b", "B Button", 0, Button.B),
    ("p1_select", "Select", 0, Button.SELECT),
    ("p1_start", "Start", 0, Button.START),
    ("p1_up", "D-pad Up", 0, Button.UP),
    ("p1_down", "D-pad Down", 0, Button.DOWN),
    ("p1_left", "D-pad Left", 0, Button.LEFT),
    ("p1_right", "D-pad Right", 0, Button.RIGHT),
    ("p2_a", "A Button", 1, Button.A),
    ("p2_b", "B Button", 1, Button.B),
    ("p2_select", "Select", 1, Button.SELECT),
    ("p2_start", "Start", 1, Button.START),
    ("p2_up", "D-pad Up", 1, Button.UP),
    ("p2_down", "D-pad Down", 1, Button.DOWN),
    ("p2_left", "D-pad Left", 1, Button.LEFT),
    ("p2_right", "D-pad Right", 1, Button.RIGHT),
)


class FrontendUnavailable(RuntimeError):
    pass


class PygameFrontend:
    def __init__(
        self,
        console: Console | None = None,
        *,
        rom_path: str | Path | None = None,
        rom_member: str | None = None,
        scale: int | None = None,
        fullscreen: bool | None = None,
        audio: bool = True,
        state_directory: str | Path | None = None,
        battery_directory: str | Path | None = None,
        settings_path: str | Path | None = None,
        fast_ppu: bool | None = None,
        gamepads: bool = True,
        diagnostics: bool = False,
    ) -> None:
        try:
            import pygame
        except ImportError as exc:
            raise FrontendUnavailable(
                "Pygame is required for the interactive frontend. "
                "Install the project with: python -m pip install -e ."
            ) from exc

        self.pg = pygame
        self.console = console
        resolved_rom_path = (
            Path(rom_path).expanduser().resolve() if rom_path else None
        )
        self.rom_path = resolved_rom_path if console is not None else None
        self.rom_member = rom_member if console is not None else None
        self.startup_rom_path = (
            resolved_rom_path if console is None else None
        )
        self.startup_rom_member = rom_member if console is None else None
        self.fast_ppu = (
            bool(fast_ppu)
            if fast_ppu is not None
            else (console.ppu.fast_mode if console is not None else True)
        )
        self.sample_rate = (
            console.apu.sample_rate if console is not None else DEFAULT_SAMPLE_RATE
        )
        if not audio:
            self.sample_rate = 0
            if console is not None:
                console.apu.sample_rate = 0
                console.apu.drain_samples()

        cartaconda_root = Path.home() / ".cartaconda"
        legacy_root = Path.home() / ".nes-from-scratch"
        data_root = (
            legacy_root
            if legacy_root.exists() and not cartaconda_root.exists()
            else cartaconda_root
        )
        self.state_directory = Path(state_directory or data_root / "states")
        self.battery_directory = Path(battery_directory or data_root / "battery")
        self.settings_path = Path(settings_path or data_root / "settings.json")
        self.settings = FrontendSettings.load(self.settings_path)
        if scale is not None and int(scale) in WINDOW_SCALES:
            self.settings.scale = int(scale)
            if fullscreen is None:
                self.settings.fullscreen = False
        if fullscreen is not None:
            self.settings.fullscreen = bool(fullscreen)

        self.audio_requested = bool(audio)
        self.gamepads_requested = bool(gamepads)
        self.diagnostics = bool(diagnostics)
        self.audio_available = False
        self.audio_init_error: str | None = None
        self.audio_channel = None
        self.audio_buffer = PcmBuffer(chunk_samples=AUDIO_CHUNK_SAMPLES)
        self.audio_started = False
        self.audio_underruns = 0
        self.audio_concealments = 0
        self.audio_recovery_fades = 0
        self.audio_start_failures = 0
        self.audio_recovering = False
        self.audio_idle_since: float | None = None
        # Estimated perf_counter() finish time of each PCM chunk currently
        # occupying one of SDL_mixer's two channel slots (current + queued),
        # oldest first. See AUDIO_IDLE_GRACE_SECONDS for why this replaces
        # Channel.get_busy()/get_queue() as the pacing signal.
        self.audio_inflight: list[float] = []
        self.audio_sound_refs = deque(maxlen=8)
        self.audio_prebuffer_samples = (
            self.audio_buffer.chunk_samples * AUDIO_PREBUFFER_CHUNKS
        )
        self.screen = None
        self.window = None
        self.renderer = None
        self.game_texture = None
        self.screen_texture = None
        self.toast_texture = None
        self.toast_texture_rect = None
        self.toast_texture_key = None
        self.presentation_backend = "surface"
        self.presentation_driver = "surface"
        self._texture_backend_failed = False
        self.ui_surface = None
        self.brand_wordmark = None
        self.brand_mascot = None
        self.brand_icon = None
        self.ui_screen_rect = None
        self.game_screen_rect = None
        self.game_native_surface = None
        self.game_scaled_surface = None
        self.game_frame_dirty = True
        self.game_display_dirty = True
        self._toast_was_visible = False
        self.unchanged_display_skips = 0
        self.frame_surface = None
        self.fonts: dict[tuple[int, bool], object] = {}
        self.thumbnails: dict[tuple[str, float, tuple[int, int]], object] = {}

        self.slot = 0
        self.view = "game" if console is not None else "home"
        self.running = False
        self.message = ""
        self.message_until = 0.0
        # The SDL surface is permanently bound to this main-thread buffer.
        # Worker-owned PPU buffers are copied into it and immediately recycled.
        self.frame = (
            bytearray(console.ppu.framebuffer)
            if console is not None
            else bytearray(PPU_WIDTH * PPU_HEIGHT * 3)
        )
        self.emulation_worker: EmulationWorker | None = None
        self._original_switch_interval: float | None = None
        self._pacing_segment_reset = True
        self.latest_frame_number = (
            int(console.ppu.frame_number) if console is not None else 0
        )
        self.emulation_queue_high_water = 0
        self.emulation_queue_starves = 0
        self.emulation_worker_starts = 0
        self.unchanged_video_frames = 0
        self.rewind_capture_deferrals = 0
        self.rewind_compression_deferrals = 0
        self._gc_was_enabled = gc.isenabled()
        self._gc_suspended = False
        self.emulated_frames = 0
        self.emulation_seconds = 0.0
        self.emulation_cpu_seconds = 0.0
        self.slowest_frame_seconds = 0.0
        self.slowest_cpu_frame_seconds = 0.0
        self.frame_budget_misses = 0
        self.emulation_histogram = [0] * DIAGNOSTIC_BUCKETS
        self.draw_seconds = 0.0
        self.slowest_draw_seconds = 0.0
        self.draw_prepare_seconds = 0.0
        self.slowest_draw_prepare_seconds = 0.0
        self.flip_seconds = 0.0
        self.slowest_flip_seconds = 0.0
        self.active_loop_seconds = 0.0
        self.slowest_active_loop_seconds = 0.0
        self.pacing_late_frames = 0
        self.worst_pacing_lateness = 0.0
        self.frame_pacer = _PresentationPacer()
        self.rewind_buffer = RewindBuffer()
        if console is not None:
            if self.diagnostics:
                console.enable_diagnostics()
            self.rewind_buffer.capture(console, force=True)

        self.joysticks: list[_GamepadState] = []
        self.gamepad_events = 0
        self.keyboard_buttons = [0, 0]
        self.key_bindings: dict[int, tuple[int, Button]] = {}
        self.gamepad_source_masks: list[dict[str, int]] = [{}, {}]
        self._rebuild_gamepad_bindings()
        self.capture_binding: str | None = None
        self.capture_device: str | None = None
        self.controls_input_mode = "keyboard"
        self.controls_return_view = "pause" if console is not None else "home"
        self.confirmation: tuple[str, str, str] | None = None
        self.buttons: list[tuple[object, str, bool]] = []
        self.focusables: list[str] = []
        self.focus_action: str | None = None
        self.mouse_ui = (-10_000, -10_000)
        self.volume_dragging = False
        self.volume_rect = None
        self.menu_axes = {0: 0, 1: 0}
        self.pending_archive_path: Path | None = None
        self.archive_items: list[ArchiveRom] = []
        self.archive_scroll = 0

        preferred_directory = (
            self.rom_path.parent
            if self.rom_path is not None
            else self.startup_rom_path.parent
            if self.startup_rom_path is not None
            else Path(self.settings.rom_directory).expanduser()
            if self.settings.rom_directory
            else Path.cwd()
        )
        self.browser_directory = (
            preferred_directory if preferred_directory.is_dir() else Path.cwd()
        )
        self.browser_showing_roots = False
        self.browser_items: list[BrowserEntry] = []
        self.browser_scroll = 0
        self._refresh_browser()

    @property
    def has_game(self) -> bool:
        return self.console is not None

    def _show_message(self, message: str, seconds: float = 2.5) -> None:
        self.message = message
        self.message_until = time.monotonic() + seconds
        self.game_display_dirty = True

    def _save_settings(self) -> None:
        try:
            self.settings.save(self.settings_path)
        except OSError as exc:
            self._show_message(f"Could not save settings: {exc}", 4)

    def _state_path(self, slot: int | None = None) -> Path:
        if self.console is None:
            raise RuntimeError("no game is loaded")
        selected = self.slot if slot is None else slot
        return (
            self.state_directory
            / self.console.cartridge.sha256[:16]
            / f"slot-{selected}.fnes"
        )

    def _thumbnail_path(self, slot: int) -> Path:
        return self._state_path(slot).with_suffix(".png")

    def _battery_path_for(self, console: Console) -> Path:
        return self.battery_directory / f"{console.cartridge.sha256[:16]}.sav"

    def _set_caption(self) -> None:
        caption = f"{BRAND_NAME} - {BRAND_TAGLINE}"
        if self.console is not None:
            caption = (
                f"{BRAND_NAME} - {self.console.cartridge.title} - "
                f"{BRAND_TAGLINE}"
            )
        window = getattr(self, "window", None)
        if window is not None:
            window.title = caption
        else:
            self.pg.display.set_caption(caption)

    def _release_texture_display(self) -> None:
        """Drop SDL renderer objects in dependency order on the main thread."""
        self.toast_texture = None
        self.toast_texture_rect = None
        self.toast_texture_key = None
        self.game_texture = None
        self.screen_texture = None
        self.renderer = None
        window = getattr(self, "window", None)
        self.window = None
        self.presentation_backend = "surface"
        self.presentation_driver = "surface"
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def _try_set_texture_display(
        self,
        size: tuple[int, int],
    ) -> bool:
        """Create an accelerated nearest-neighbor SDL texture presenter."""
        force_texture = (
            os.environ.get("CARTACONDA_TEXTURE_DISPLAY", "").strip() == "1"
        )
        force_surface = (
            os.environ.get("CARTACONDA_SOFTWARE_DISPLAY", "").strip() == "1"
        )
        if (
            force_surface
            or self._texture_backend_failed
            or (sys.platform != "win32" and not force_texture)
        ):
            return False

        window = None
        renderer = None
        game_texture = None
        screen_texture = None
        try:
            import pygame._sdl2 as sdl2
            from pygame._sdl2.video import Renderer, Texture, Window

            drivers = tuple(sdl2.get_drivers())
            driver_indices = tuple(
                index
                for index, driver in enumerate(drivers)
                if int(driver.flags) & 0x02
            )
            if not driver_indices:
                raise RuntimeError("SDL exposes no accelerated renderer")
            window_size = size
            options: dict[str, object] = {
                "size": window_size,
                "resizable": False,
                "allow_high_dpi": False,
            }
            if self.settings.fullscreen:
                desktop_sizes = self.pg.display.get_desktop_sizes()
                if desktop_sizes:
                    window_size = tuple(desktop_sizes[0])
                    options["size"] = window_size
                options["fullscreen_desktop"] = True
            window = Window(
                f"{BRAND_NAME} - {BRAND_TAGLINE}",
                **options,
            )
            actual_size = tuple(window.size)
            screen = self.pg.Surface(actual_size, depth=32)
            driver_index = -1
            for candidate_index in driver_indices:
                candidate_renderer = None
                candidate_game_texture = None
                candidate_screen_texture = None
                try:
                    candidate_renderer = Renderer(
                        window,
                        index=candidate_index,
                        accelerated=1,
                        vsync=False,
                    )
                    candidate_game_texture = Texture(
                        candidate_renderer,
                        (PPU_WIDTH, PPU_HEIGHT),
                        depth=0,
                        streaming=True,
                        scale_quality=0,
                    )
                    candidate_screen_texture = Texture(
                        candidate_renderer,
                        actual_size,
                        depth=0,
                        streaming=True,
                        scale_quality=0,
                    )
                except Exception:
                    candidate_game_texture = None
                    candidate_screen_texture = None
                    candidate_renderer = None
                    continue
                renderer = candidate_renderer
                game_texture = candidate_game_texture
                screen_texture = candidate_screen_texture
                driver_index = candidate_index
                break
            if renderer is None:
                raise RuntimeError("SDL accelerated renderers failed")
        except Exception:
            # Renderer-owned textures must die before their renderer, and the
            # renderer before its window. This ordering also covers a partial
            # Direct3D/OpenGL initialization failure.
            game_texture = None
            screen_texture = None
            renderer = None
            if window is not None:
                try:
                    window.destroy()
                except Exception:
                    pass
            self._texture_backend_failed = True
            return False

        self.window = window
        self.renderer = renderer
        self.screen = screen
        self.game_texture = game_texture
        self.screen_texture = screen_texture
        self.presentation_backend = "sdl-texture"
        self.presentation_driver = str(drivers[driver_index].name)
        return True

    def _set_display(self) -> None:
        desktop_sizes = (
            self.pg.display.get_desktop_sizes()
            if self.settings.fullscreen
            else ()
        )
        size = (
            tuple(desktop_sizes[0])
            if desktop_sizes
            else self._windowed_size(self.settings.scale)
        )
        self._release_texture_display()
        self.presentation_backend = "surface"
        self.presentation_driver = "surface"
        if not self._try_set_texture_display(size):
            flags = self.pg.FULLSCREEN if self.settings.fullscreen else 0
            surface_size = (0, 0) if self.settings.fullscreen else size
            # Cartaconda owns fractional-NTSC pacing. Explicitly disable swap
            # interval waiting so a 60 Hz desktop does not fight the 60.0988
            # Hz emulated clock or block unpredictably inside flip().
            self.screen = self.pg.display.set_mode(
                surface_size,
                flags,
                vsync=0,
            )
        self.ui_surface = self.pg.Surface((UI_WIDTH, UI_HEIGHT), self.pg.SRCALPHA)
        self.ui_screen_rect = self._fit_rect(
            UI_WIDTH,
            UI_HEIGHT,
            *self.screen.get_size(),
        )
        self.game_screen_rect = self._fit_rect(
            PPU_WIDTH,
            PPU_HEIGHT,
            *self.screen.get_size(),
        )
        # The accelerated Windows path uploads only the native 256x240 frame
        # and lets SDL's GPU renderer scale it. The portable surface fallback
        # converts once at native resolution and scales in the display format.
        self.game_native_surface = (
            None
            if self.presentation_backend == "sdl-texture"
            else self.pg.Surface((PPU_WIDTH, PPU_HEIGHT)).convert(self.screen)
        )
        self.game_scaled_surface = None
        self.toast_texture = None
        self.toast_texture_rect = None
        self.toast_texture_key = None
        self.game_frame_dirty = True
        self.game_display_dirty = True
        self._set_caption()
        icon = getattr(self, "brand_icon", None)
        if icon is not None:
            window = getattr(self, "window", None)
            if window is not None:
                window.set_icon(icon)
            else:
                self.pg.display.set_icon(icon)

    def _bind_frame_surface(self) -> None:
        """Bind one SDL surface to a Python buffer with frontend-long life."""
        self.frame_surface = self.pg.image.frombuffer(
            self.frame,
            (PPU_WIDTH, PPU_HEIGHT),
            "RGB",
        )

    def _set_frame_source(self, frame: bytes | bytearray) -> None:
        """Switch frame storage only when a console is loaded or unloaded."""
        if frame is self.frame and self.frame_surface is not None:
            return
        self.frame_surface = None
        self.frame = frame if isinstance(frame, bytearray) else bytearray(frame)
        self.game_frame_dirty = True
        self.game_display_dirty = True
        if self.pg.display.get_init():
            self._bind_frame_surface()

    def _sync_gc_mode(self) -> None:
        """Keep cyclic-GC pauses out of active emulation frame deadlines."""
        should_suspend = (
            self._gc_was_enabled
            and self.console is not None
            and self.view == "game"
        )
        if should_suspend and not self._gc_suspended:
            gc.disable()
            self._gc_suspended = True
        elif not should_suspend and self._gc_suspended:
            gc.enable()
            self._gc_suspended = False
            # Menu time is outside emulated/audio deadlines and is the safest
            # place to reclaim short-lived UI reference cycles, if any.
            gc.collect(0)

    def _load_brand_assets(self) -> None:
        """Load the supplied logo once and derive crisp UI/icon crops."""
        try:
            logo = self.pg.image.load(str(BRAND_LOGO_PATH)).convert(self.screen)
            wordmark = logo.subsurface((630, 300, 1270, 280))
            mascot = logo.subsurface((70, 90, 570, 620))
            self.brand_wordmark = self.pg.transform.scale(
                wordmark,
                (500, 110),
            )
            self.brand_mascot = self.pg.transform.scale(
                mascot,
                (250, 272),
            )
            icon = self.pg.transform.scale(mascot, (64, 64))
            self.brand_icon = icon
            window = getattr(self, "window", None)
            if window is not None:
                window.set_icon(icon)
            else:
                self.pg.display.set_icon(icon)
        except (OSError, ValueError, self.pg.error):
            self.brand_wordmark = None
            self.brand_mascot = None
            self.brand_icon = None

    @staticmethod
    def _windowed_size(scale: int) -> tuple[int, int]:
        """Keep an exact NES integer scale inside a UI-friendly 4:3 window."""
        height = PPU_HEIGHT * scale
        width = round(height * UI_WIDTH / UI_HEIGHT)
        return width, height

    def _initialize(self) -> None:
        # Initialize only the native subsystems Cartaconda will actually use.
        # In particular, --mute and --no-gamepad must not open an SDL audio or
        # HID driver merely because pygame.init() initializes every module.
        self.pg.display.init()
        self.pg.font.init()
        if self.audio_requested:
            try:
                self.pg.mixer.init(
                    frequency=self.sample_rate,
                    size=-16,
                    channels=1,
                    buffer=AUDIO_MIXER_BUFFER_SAMPLES,
                    allowedchanges=self.pg.AUDIO_ALLOW_FREQUENCY_CHANGE,
                )
            except self.pg.error as exc:
                # Surfaced by --diagnostics as audio-init-error= so a failed
                # mixer.init() (wrong/unavailable SDL_AUDIODRIVER, device
                # held exclusively by another app, missing driver, ...) is
                # diagnosable instead of silently reporting mixer=disabled
                # with no indication of why.
                self.audio_init_error = str(exc)
        if self.gamepads_requested:
            try:
                self.pg.joystick.init()
            except self.pg.error:
                self.gamepads_requested = False
        self._set_display()
        self._bind_frame_surface()
        self._load_brand_assets()
        self._set_caption()
        self._rebuild_key_bindings()
        if self.gamepads_requested:
            self._prime_gamepad_events()
        if self.audio_requested and self.pg.mixer.get_init():
            actual_rate, _sample_format, _channels = self.pg.mixer.get_init()
            self.sample_rate = int(actual_rate)
            if self.console is not None:
                self.console.apu.drain_samples()
            self.pg.mixer.set_num_channels(1)
            self.audio_channel = self.pg.mixer.Channel(0)
            self.audio_available = True
            self._apply_volume()
        else:
            self._sync_core_sample_generation()
        if self.console is not None:
            try:
                if self.console.load_battery(
                    self._battery_path_for(self.console)
                ):
                    self._show_message("Battery save loaded")
            except (OSError, ValueError) as exc:
                self._show_message(f"Battery save ignored: {exc}", 4)
        elif self.startup_rom_path is not None:
            source = self.startup_rom_path
            member = self.startup_rom_member
            self.startup_rom_path = None
            self.startup_rom_member = None
            self._load_game(source, member=member)
        self._sync_gc_mode()
        if self.diagnostics:
            self._print_startup_diagnostics()

    def _print_startup_diagnostics(self) -> None:
        mixer = self.pg.mixer.get_init()
        try:
            mixer_driver = (
                self.pg.mixer.get_driver() if mixer else "disabled"
            )
        except (AttributeError, self.pg.error):
            mixer_driver = "unavailable"
        sdl_version = ".".join(
            str(part) for part in self.pg.get_sdl_version()
        )
        try:
            video_driver = self.pg.display.get_driver()
        except self.pg.error:
            video_driver = "unavailable"
        print(
            "[Cartaconda diagnostics] "
            f"Cartaconda={__version__} "
            f"Python={platform.python_version()} "
            f"pygame-ce={self.pg.version.ver} "
            f"SDL={sdl_version} "
            f"video={video_driver} "
            f"display-pacing=buffered-exact-ntsc "
            f"display-backend={self.presentation_backend} "
            f"display-driver={self.presentation_driver} "
            f"emulation-thread=bounded "
            f"frame-queue={DEFAULT_QUEUE_DEPTH} "
            f"thread-switch-ms={EMULATION_THREAD_SWITCH_SECONDS * 1000:.1f} "
            f"mixer={mixer or 'disabled'} "
            f"mixer-driver={mixer_driver} "
            f"audio-init-error="
            f"{(self.audio_init_error or 'none').replace(' ', '_')} "
            f"PCM-chunk={self.audio_buffer.chunk_samples} "
            f"mixer-buffer={AUDIO_MIXER_BUFFER_SAMPLES} "
            f"audio-start=channel-queue "
            f"gamepads={len(self.joysticks)} "
            f"gamepad-input=events "
            f"PCM-prebuffer={self.audio_prebuffer_samples} "
            f"frozen={'yes' if getattr(sys, 'frozen', False) else 'no'}",
            file=sys.stderr,
            flush=True,
        )

    def _print_final_diagnostics(self, *, clean_shutdown: bool) -> None:
        frame_pacer = getattr(self, "frame_pacer", _PresentationPacer())
        console = getattr(self, "console", None)
        ppu = console.ppu if console is not None else None
        cpu = console.cpu if console is not None else None
        apu = console.apu if console is not None else None
        average_ms = (
            self.emulation_seconds * 1000 / self.emulated_frames
            if self.emulated_frames
            else 0.0
        )
        draw_average_ms = (
            self.draw_seconds * 1000 / frame_pacer.presented_frames
            if frame_pacer.presented_frames
            else 0.0
        )
        draw_prepare_average_ms = (
            getattr(self, "draw_prepare_seconds", 0.0)
            * 1000
            / frame_pacer.presented_frames
            if frame_pacer.presented_frames
            else 0.0
        )
        flip_average_ms = (
            getattr(self, "flip_seconds", 0.0)
            * 1000
            / frame_pacer.presented_frames
            if frame_pacer.presented_frames
            else 0.0
        )
        active_average_ms = (
            self.active_loop_seconds * 1000 / self.emulated_frames
            if self.emulated_frames
            else 0.0
        )
        cpu_average_ms = (
            self.emulation_cpu_seconds * 1000 / self.emulated_frames
            if self.emulated_frames
            else 0.0
        )
        hot_cycles = (
            getattr(console, "diagnostic_hot_cycles", None)
            if console is not None
            else None
        )
        hot_spans = "none"
        if hot_cycles is not None:
            top = sorted(
                (
                    (cycles, address)
                    for address, cycles in enumerate(hot_cycles)
                    if cycles
                ),
                reverse=True,
            )[:8]
            if top:
                hot_spans = ",".join(
                    f"{address:04X}:{cycles}"
                    for cycles, address in top
                )
        print(
            "[Cartaconda diagnostics] "
            f"frames={self.emulated_frames} "
            f"emulation-average-ms={average_ms:.3f} "
            f"emulation-p95-ms={self._diagnostic_percentile(0.95):.3f} "
            f"emulation-p99-ms={self._diagnostic_percentile(0.99):.3f} "
            f"emulation-max-ms={self.slowest_frame_seconds * 1000:.3f} "
            f"emulation-cpu-average-ms={cpu_average_ms:.3f} "
            f"emulation-cpu-max-ms="
            f"{self.slowest_cpu_frame_seconds * 1000:.3f} "
            f"draw-average-ms={draw_average_ms:.3f} "
            f"draw-max-ms={self.slowest_draw_seconds * 1000:.3f} "
            f"draw-prepare-average-ms={draw_prepare_average_ms:.3f} "
            f"draw-prepare-max-ms="
            f"{getattr(self, 'slowest_draw_prepare_seconds', 0.0) * 1000:.3f} "
            f"flip-average-ms={flip_average_ms:.3f} "
            f"flip-max-ms="
            f"{getattr(self, 'slowest_flip_seconds', 0.0) * 1000:.3f} "
            f"active-average-ms={active_average_ms:.3f} "
            f"active-max-ms={self.slowest_active_loop_seconds * 1000:.3f} "
            f"budget-misses={self.frame_budget_misses} "
            f"late-frames={self.pacing_late_frames} "
            f"max-late-ms={self.worst_pacing_lateness * 1000:.3f} "
            f"presented-frames={frame_pacer.presented_frames} "
            f"presentation-skips={frame_pacer.presentation_skips} "
            f"max-skip-streak={frame_pacer.max_skip_streak} "
            f"presentation-jitter-p95-ms="
            f"{frame_pacer.jitter_percentile(0.95):.3f} "
            f"presentation-jitter-max-ms="
            f"{frame_pacer.worst_presentation_jitter * 1000:.3f} "
            f"pacing-debt-ms={frame_pacer.timing_debt * 1000:.3f} "
            f"frame-queue-high-water="
            f"{getattr(self, 'emulation_queue_high_water', 0)} "
            f"frame-queue-starves="
            f"{getattr(self, 'emulation_queue_starves', 0)} "
            f"worker-starts={getattr(self, 'emulation_worker_starts', 0)} "
            f"unchanged-video-frames="
            f"{getattr(self, 'unchanged_video_frames', 0)} "
            f"unchanged-display-skips="
            f"{getattr(self, 'unchanged_display_skips', 0)} "
            f"rewind-deferrals="
            f"{getattr(self, 'rewind_capture_deferrals', 0)} "
            f"rewind-compression-deferrals="
            f"{getattr(self, 'rewind_compression_deferrals', 0)} "
            f"rewind-captures="
            f"{getattr(getattr(self, 'rewind_buffer', None), 'captures', 0)} "
            f"ppu-scanline-renders="
            f"{getattr(ppu, 'diagnostic_scanline_renders', 0)} "
            f"ppu-scanline-replays="
            f"{getattr(ppu, 'diagnostic_scanline_replays', 0)} "
            f"ppu-status-probes="
            f"{getattr(ppu, 'diagnostic_status_probes', 0)} "
            f"ppu-mapper-invalidations="
            f"{getattr(ppu, 'diagnostic_mapper_invalidations', 0)} "
            f"ppu-mapper-background-preserves="
            f"{getattr(ppu, 'diagnostic_mapper_background_preserves', 0)} "
            f"ppu-palette-background-preserves="
            f"{getattr(ppu, 'diagnostic_palette_background_preserves', 0)} "
            f"ppu-cache-invalidations="
            f"{getattr(ppu, 'diagnostic_cache_invalidations', 0)} "
            f"ppu-cache-coalesces="
            f"{getattr(ppu, 'diagnostic_cache_coalesces', 0)} "
            f"ppu-world-cache-misses="
            f"{getattr(ppu, 'diagnostic_world_cache_misses', 0)} "
            f"ppu-world-cache-evictions="
            f"{getattr(ppu, 'diagnostic_world_cache_evictions', 0)} "
            f"ppu-world-cache-admission-deferrals="
            f"{getattr(ppu, 'diagnostic_world_cache_admission_deferrals', 0)} "
            f"ppu-world-cache-size="
            f"{len(getattr(ppu, '_fast_background_world_cache', ()))} "
            f"ppu-deferred-data-writes="
            f"{getattr(ppu, 'diagnostic_deferred_data_writes', 0)} "
            f"cpu-ppu-stream-batches="
            f"{getattr(cpu, 'deferred_ppu_stream_batches', 0)} "
            f"cpu-literal-instructions="
            f"{getattr(console, 'diagnostic_literal_instructions', 0)} "
            f"cpu-safe-batches="
            f"{getattr(console, 'diagnostic_safe_batches', 0)} "
            f"cpu-poll-batches="
            f"{getattr(console, 'diagnostic_poll_batches', 0)} "
            f"cpu-dmc-poll-batches="
            f"{getattr(console, 'diagnostic_dmc_poll_batches', 0)} "
            f"cpu-dmc-poll-fetches="
            f"{getattr(console, 'diagnostic_dmc_poll_fetches', 0)} "
            f"cpu-counter-batches="
            f"{getattr(console, 'diagnostic_counter_batches', 0)} "
            f"cpu-pcm-batches="
            f"{getattr(console, 'diagnostic_pcm_batches', 0)} "
            f"cpu-pcm-writes="
            f"{getattr(console, 'diagnostic_pcm_writes', 0)} "
            f"cpu-ppu-wait-batches="
            f"{getattr(console, 'diagnostic_ppu_wait_batches', 0)} "
            f"cpu-device-flushes="
            f"{getattr(console, 'diagnostic_device_flushes', 0)} "
            f"cpu-device-flush-cycles="
            f"{getattr(console, 'diagnostic_device_flush_cycles', 0)} "
            f"cpu-stall-spans="
            f"{getattr(console, 'diagnostic_stall_spans', 0)} "
            f"dmc-fetches="
            f"{getattr(getattr(apu, 'dmc', None), 'fetch_count', 0)} "
            f"cpu-hot-spans={hot_spans} "
            f"cpu-translated-blocks="
            f"{getattr(cpu, 'translated_block_compilations', 0)} "
            f"cpu-block-deferrals="
            f"{getattr(cpu, 'translated_block_deferrals', 0)} "
            f"cpu-block-compile-peak="
            f"{getattr(cpu, 'translated_block_peak_frame_compilations', 0)} "
            f"cpu-bank-compiles="
            f"{getattr(cpu, 'segmented_map_compilations', 0)} "
            f"cpu-bank-deferrals="
            f"{getattr(cpu, 'segmented_map_deferrals', 0)} "
            f"audio-underruns={self.audio_underruns} "
            f"audio-concealments="
            f"{getattr(self, 'audio_concealments', 0)} "
            f"audio-recovery-fades="
            f"{getattr(self, 'audio_recovery_fades', 0)} "
            f"audio-start-failures={self.audio_start_failures} "
            f"audio-resyncs={self.audio_buffer.resyncs} "
            f"gamepad-events={self.gamepad_events} "
            f"clean-shutdown={'yes' if clean_shutdown else 'no'}",
            file=sys.stderr,
            flush=True,
        )

    def _record_emulation_time(
        self,
        seconds: float,
        cpu_seconds: float | None = None,
    ) -> None:
        self.emulated_frames += 1
        self.emulation_seconds += seconds
        measured_cpu = seconds if cpu_seconds is None else cpu_seconds
        self.emulation_cpu_seconds += measured_cpu
        self.slowest_cpu_frame_seconds = max(
            self.slowest_cpu_frame_seconds,
            measured_cpu,
        )
        self.slowest_frame_seconds = max(
            self.slowest_frame_seconds,
            seconds,
        )
        bucket = min(
            DIAGNOSTIC_BUCKETS - 1,
            int(seconds * 1000 / DIAGNOSTIC_BUCKET_MS),
        )
        self.emulation_histogram[bucket] += 1

    def _diagnostic_percentile(self, percentile: float) -> float:
        if self.emulated_frames <= 0:
            return 0.0
        target = max(1, int(self.emulated_frames * percentile + 0.999999))
        accumulated = 0
        for index, count in enumerate(self.emulation_histogram):
            accumulated += count
            if accumulated >= target:
                return (index + 1) * DIAGNOSTIC_BUCKET_MS
        return DIAGNOSTIC_BUCKETS * DIAGNOSTIC_BUCKET_MS

    def _prime_gamepad_events(self) -> None:
        """Open startup devices through pygame-ce's hot-plug event path."""
        if not self.gamepads_requested:
            return

        added = getattr(self.pg, "JOYDEVICEADDED", -1)
        removed = getattr(self.pg, "JOYDEVICEREMOVED", -1)
        try:
            self.pg.event.pump()
            events = self.pg.event.get((added, removed))
        except (AttributeError, TypeError, self.pg.error):
            events = ()
        for event in events:
            self.gamepad_events += 1
            self._handle_gamepad_device_event(event)

    def _open_gamepad(self, device_index: int) -> None:
        """Open exactly the device named by a JOYDEVICEADDED event."""
        try:
            handle = self.pg.joystick.Joystick(int(device_index))
            instance_id = int(handle.get_instance_id())
        except (TypeError, ValueError, self.pg.error):
            return
        if any(
            state.instance_id == instance_id for state in self.joysticks
        ):
            try:
                handle.quit()
            except self.pg.error:
                pass
            return
        self.joysticks.append(_GamepadState(handle, instance_id))

    def _remove_gamepad(self, instance_id: int) -> None:
        """Close one removed device without touching any surviving handle."""
        for index, state in enumerate(self.joysticks):
            if state.instance_id != int(instance_id):
                continue
            del self.joysticks[index]
            try:
                state.handle.quit()
            except self.pg.error:
                pass
            return

    def _handle_gamepad_device_event(self, event) -> None:
        if event.type == getattr(self.pg, "JOYDEVICEADDED", -1):
            self._open_gamepad(event.device_index)
        elif event.type == getattr(self.pg, "JOYDEVICEREMOVED", -1):
            self._remove_gamepad(event.instance_id)

    def _gamepad_for_event(self, event) -> _GamepadState | None:
        instance_id = getattr(event, "instance_id", None)
        if instance_id is None:
            return None
        for state in self.joysticks:
            if state.instance_id == int(instance_id):
                return state
        return None

    def _rebuild_key_bindings(self) -> None:
        self.key_bindings.clear()
        specifications = {
            action: (player, button)
            for action, _label, player, button in CONTROL_ROWS
        }
        for action, binding_name in self.settings.key_bindings.items():
            specification = specifications.get(action)
            if specification is None:
                continue
            try:
                key = self.pg.key.key_code(binding_name)
            except ValueError:
                key = self.pg.key.key_code(DEFAULT_KEY_BINDINGS[action])
                self.settings.key_bindings[action] = DEFAULT_KEY_BINDINGS[action]
            self.key_bindings[key] = specification

    def _rebuild_gamepad_bindings(self) -> None:
        """Compile persistent source strings into two cheap event-state maps."""
        source_masks: list[dict[str, int]] = [{}, {}]
        for action, _label, player, button in CONTROL_ROWS:
            for source in self.settings.gamepad_bindings[action]:
                source_masks[player][source] = (
                    source_masks[player].get(source, 0) | int(button)
                )
        self.gamepad_source_masks = source_masks

    def _mapped_gamepad_buttons(
        self,
        player: int,
        gamepad: _GamepadState,
    ) -> int:
        source_masks = getattr(self, "gamepad_source_masks", None)
        if source_masks is None:
            return gamepad.nes_buttons
        return gamepad.mapped_buttons(source_masks[player])

    def _start_emulation_worker(self) -> None:
        if self.console is None or self.view != "game":
            return
        worker = getattr(self, "emulation_worker", None)
        if worker is not None and worker.console is not self.console:
            self._stop_emulation_worker()
            worker = None
        if worker is None:
            worker = EmulationWorker(
                self.console,
                queue_depth=DEFAULT_QUEUE_DEPTH,
                rewind_buffer=self.rewind_buffer,
                measure_timing=self.diagnostics,
            )
            self.emulation_worker = worker
        if worker.running:
            return
        if self._original_switch_interval is None:
            self._original_switch_interval = sys.getswitchinterval()
            sys.setswitchinterval(EMULATION_THREAD_SWITCH_SECONDS)
        try:
            worker.start()
        except BaseException:
            original = self._original_switch_interval
            if original is not None:
                sys.setswitchinterval(original)
                self._original_switch_interval = None
            self.emulation_worker = None
            raise
        self.emulation_worker_starts += 1
        self._pacing_segment_reset = True

    def _stop_emulation_worker(self) -> None:
        worker = getattr(self, "emulation_worker", None)
        try:
            if worker is not None:
                worker.stop()
                self.emulation_queue_high_water = max(
                    self.emulation_queue_high_water,
                    worker.queue_high_water,
                )
                self.rewind_capture_deferrals += worker.rewind_deferrals
                self.rewind_compression_deferrals += (
                    worker.rewind_compression_deferrals
                )
        finally:
            self.emulation_worker = None
            original = getattr(self, "_original_switch_interval", None)
            if original is not None:
                sys.setswitchinterval(original)
                self._original_switch_interval = None

    def _player_buttons(self) -> tuple[int, int]:
        player_buttons = self.keyboard_buttons.copy()
        for player, gamepad in enumerate(self.joysticks[:2]):
            player_buttons[player] |= self._mapped_gamepad_buttons(
                player,
                gamepad,
            )
        return player_buttons[0], player_buttons[1]

    def _poll_inputs(self) -> None:
        if self.console is None or self.view != "game":
            worker = getattr(self, "emulation_worker", None)
            if worker is not None:
                worker.set_inputs(0, 0)
            elif self.console is not None:
                self.console.controller1.set_buttons(0)
                self.console.controller2.set_buttons(0)
            return
        player1, player2 = self._player_buttons()
        worker = getattr(self, "emulation_worker", None)
        if worker is not None:
            worker.set_inputs(player1, player2)
        else:
            self.console.controller1.set_buttons(player1)
            self.console.controller2.set_buttons(player2)

    def _clear_inputs(self) -> None:
        self.keyboard_buttons[:] = (0, 0)
        for gamepad in self.joysticks:
            gamepad.clear()
        worker = getattr(self, "emulation_worker", None)
        if worker is not None:
            worker.set_inputs(0, 0)
        elif self.console is not None:
            self.console.controller1.set_buttons(0)
            self.console.controller2.set_buttons(0)

    def _set_keyboard_key(self, key: int, pressed: bool) -> None:
        binding = self.key_bindings.get(key)
        if binding is None:
            return
        player, button = binding
        if pressed:
            self.keyboard_buttons[player] |= int(button)
        else:
            self.keyboard_buttons[player] &= ~int(button)

    def _set_view(self, view: str) -> None:
        old_view = self.view
        if old_view == "game" and view != "game":
            self._stop_emulation_worker()
        self.view = view
        self.focus_action = None
        self.confirmation = None
        self.capture_binding = None
        self.capture_device = None
        self._clear_inputs()
        if view != "game":
            self._reset_audio_stream()
        if view == "game" and old_view != "game":
            self.frame_pacer.reset_segment()
            self._pacing_segment_reset = True
            self.game_display_dirty = True
            if self.running:
                self._start_emulation_worker()
        self._sync_gc_mode()

    def _open_pause(self) -> None:
        if self.console is not None:
            self._set_view("pause")

    def _pause_core_access(self) -> bool:
        """Stop the owner thread before a main-thread console operation."""
        worker = getattr(self, "emulation_worker", None)
        was_running = bool(worker is not None and worker.running)
        if worker is not None:
            self._stop_emulation_worker()
        return was_running

    def _resume_core_access(self, was_running: bool) -> None:
        if (
            was_running
            and self.console is not None
            and self.view == "game"
            and self.running
        ):
            self._reset_audio_stream(drain_core=True)
            self.frame_pacer.reset_segment()
            self._pacing_segment_reset = True
            self._start_emulation_worker()

    def _go_back(self) -> None:
        if self.confirmation is not None:
            self.confirmation = None
            self.focus_action = None
            return
        if self.capture_binding is not None:
            self.capture_binding = None
            self.capture_device = None
            self.focus_action = None
            return
        if self.view == "home":
            self.running = False
        elif self.view == "pause":
            self._set_view("game")
        elif self.view in ("states", "settings"):
            self._set_view("pause" if self.has_game else "home")
        elif self.view == "controls":
            self._set_view(self.controls_return_view)
        elif self.view == "browser":
            self._set_view("pause" if self.has_game else "home")
        elif self.view == "archive":
            self.pending_archive_path = None
            self.archive_items.clear()
            self.archive_scroll = 0
            self._set_view("pause" if self.has_game else "home")
        else:
            self._set_view("home")

    def _save_current_battery(self) -> None:
        self._pause_core_access()
        if self.console is None:
            return
        try:
            self.console.save_battery(self._battery_path_for(self.console))
        except OSError as exc:
            self._show_message(f"Could not save battery RAM: {exc}", 4)

    def _load_game(
        self,
        path: str | Path,
        *,
        member: str | None = None,
        reloading: bool = False,
    ) -> bool:
        resume_worker = self._pause_core_access()
        source = Path(path).expanduser().resolve()
        try:
            loaded = load_rom(source, member)
            cartridge = loaded.cartridge
            new_console = Console(
                cartridge,
                sample_rate=self.sample_rate,
                fast_ppu=self.fast_ppu,
            )
            if self.diagnostics:
                new_console.enable_diagnostics()
        except ArchiveSelectionRequired as selection:
            self.pending_archive_path = source
            self.archive_items = list(selection.roms)
            self.archive_scroll = 0
            self._set_view("archive")
            self._show_message(
                f"Choose one of {len(self.archive_items)} games in "
                f"{source.name}"
            )
            return True
        except (
            OSError,
            CartridgeError,
            MapperError,
            RomLoadError,
            ValueError,
        ) as exc:
            self._show_message(f"Could not load game: {exc}", 5)
            self._resume_core_access(resume_worker)
            return False

        self._save_current_battery()
        self.console = new_console
        self._sync_core_sample_generation()
        self.rom_path = loaded.source_path
        self.rom_member = loaded.member_name
        self.pending_archive_path = None
        self.archive_items.clear()
        self.archive_scroll = 0
        self.latest_frame_number = int(new_console.ppu.frame_number)
        self._set_frame_source(bytearray(new_console.ppu.framebuffer))
        try:
            new_console.load_battery(self._battery_path_for(new_console))
        except (OSError, ValueError) as exc:
            self._show_message(f"Battery save ignored: {exc}", 4)
        self.rewind_buffer.clear()
        self.rewind_buffer.capture(new_console, force=True)
        self.settings.remember_rom(source)
        self._save_settings()
        self.slot = 0
        self.thumbnails.clear()
        self._reset_audio_stream()
        self._set_caption()
        self._set_view("game")
        if self.running:
            self._start_emulation_worker()
        self._show_message(
            f"{'Reloaded' if reloading else 'Loaded'} {cartridge.title}"
        )
        return True

    def _unload_game(self) -> None:
        self._pause_core_access()
        self._save_current_battery()
        self._reset_audio_stream()
        self.console = None
        self.rewind_buffer.clear()
        self.rom_path = None
        self.rom_member = None
        self.pending_archive_path = None
        self.archive_items.clear()
        self.archive_scroll = 0
        self._set_frame_source(bytearray(PPU_WIDTH * PPU_HEIGHT * 3))
        self.slot = 0
        self._set_caption()
        self._set_view("home")
        self._show_message("Game tucked safely back on the shelf")

    def _save_state(self, slot: int | None = None) -> None:
        if self.console is None:
            self._show_message("Load a game before using save states")
            return
        resume_worker = self._pause_core_access()
        selected = self.slot if slot is None else slot
        try:
            self.console.save_state(self._state_path(selected))
            thumbnail_path = self._thumbnail_path(selected)
            thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
            self.pg.image.save(self.frame_surface, thumbnail_path)
            self.slot = selected
            self.thumbnails.clear()
            self._show_message(f"Saved a snapshot in slot {selected}")
        except (OSError, ValueError) as exc:
            self._show_message(f"Could not save state: {exc}", 4)
        finally:
            self._resume_core_access(resume_worker)

    def _load_state(self, slot: int | None = None) -> None:
        if self.console is None:
            self._show_message("Load a game before using save states")
            return
        resume_worker = self._pause_core_access()
        selected = self.slot if slot is None else slot
        try:
            self.console.load_state(self._state_path(selected))
            # The sample rate is a host-device property, not NES state. A
            # portable state may have been created on a different mixer.
            self._sync_core_sample_generation()
            self.latest_frame_number = int(self.console.ppu.frame_number)
            self._set_frame_source(bytearray(self.console.ppu.framebuffer))
            self.rewind_buffer.clear()
            self.rewind_buffer.capture(self.console, force=True)
            self.slot = selected
            self._reset_audio_stream(drain_core=True)
            self._show_message(f"Restored slot {selected}")
            self._set_view("game")
        except FileNotFoundError:
            self._show_message(f"Slot {selected} is empty")
        except (OSError, ValueError, StateError) as exc:
            self._show_message(f"Could not load state: {exc}", 4)
        finally:
            self._resume_core_access(resume_worker)

    def _reset_console(self) -> None:
        if self.console is None:
            return
        resume_worker = self._pause_core_access()
        try:
            self.console.reset()
            self.latest_frame_number = int(self.console.ppu.frame_number)
            self.frame[:] = self.console.ppu.framebuffer
            self.game_frame_dirty = True
            self.rewind_buffer.clear()
            self.rewind_buffer.capture(self.console, force=True)
            self._reset_audio_stream(drain_core=True)
            self._show_message("Console reset")
        finally:
            self._resume_core_access(resume_worker)

    def _rewind(self, seconds: float = 1.0) -> None:
        if self.console is None:
            return
        resume_worker = self._pause_core_access()
        try:
            result = self.rewind_buffer.restore_seconds(
                self.console,
                seconds,
            )
            if result is None:
                self._show_message("No earlier rewind snapshot yet")
                return
            self._sync_core_sample_generation()
            self.latest_frame_number = int(self.console.ppu.frame_number)
            self.frame[:] = self.console.ppu.framebuffer
            self.game_frame_dirty = True
            self._reset_audio_stream(drain_core=True)
            self.frame_pacer.reset_segment()
            self._pacing_segment_reset = True
            self._show_message(f"Rewound {result.seconds:.1f} seconds")
            self._set_view("game")
        except (ValueError, StateError) as exc:
            self.rewind_buffer.clear()
            self._show_message(f"Could not rewind: {exc}", 4)
        finally:
            self._resume_core_access(resume_worker)

    def _delete_state(self, slot: int) -> None:
        removed = False
        for path in (self._state_path(slot), self._thumbnail_path(slot)):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._show_message(f"Could not delete slot {slot}: {exc}", 4)
                return
        self.thumbnails.clear()
        self._show_message(
            f"Cleared slot {slot}" if removed else f"Slot {slot} is already empty"
        )

    def _save_screenshot(self) -> None:
        if self.console is None:
            return
        screenshot = (
            self.state_directory
            / self.console.cartridge.sha256[:16]
            / f"frame-{self.latest_frame_number}.png"
        )
        try:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            self.pg.image.save(self.frame_surface, screenshot)
            self._show_message(f"Screenshot saved: {screenshot.name}")
        except OSError as exc:
            self._show_message(f"Could not save screenshot: {exc}", 4)

    def _refresh_browser(self) -> None:
        self.browser_items = (
            browser_roots()
            if self.browser_showing_roots
            else browser_entries(self.browser_directory)
        )
        self.browser_scroll = min(
            self.browser_scroll,
            max(0, len(self.browser_items) - 9),
        )

    def _open_browser(self) -> None:
        if self.rom_path is not None:
            self.browser_directory = self.rom_path.parent
        elif self.settings.rom_directory:
            candidate = Path(self.settings.rom_directory).expanduser()
            if candidate.is_dir():
                self.browser_directory = candidate
        self.browser_showing_roots = False
        self.browser_scroll = 0
        self._refresh_browser()
        self._set_view("browser")

    def _enter_browser_item(self, index: int) -> None:
        if not 0 <= index < len(self.browser_items):
            return
        entry = self.browser_items[index]
        if entry.is_directory:
            self.browser_directory = entry.path
            self.browser_showing_roots = False
            self.browser_scroll = 0
            self._refresh_browser()
            self.focus_action = "browse:0" if self.browser_items else "browse_parent"
        else:
            self._load_game(entry.path)

    def _enter_archive_item(self, index: int) -> None:
        if (
            self.pending_archive_path is None
            or not 0 <= index < len(self.archive_items)
        ):
            return
        self._load_game(
            self.pending_archive_path,
            member=self.archive_items[index].member_name,
        )

    def _browser_parent(self) -> None:
        if self.browser_showing_roots:
            return
        parent = self.browser_directory.parent
        if parent != self.browser_directory:
            self.browser_directory = parent
            self.browser_scroll = 0
            self._refresh_browser()
        else:
            self._show_browser_roots()

    def _show_browser_roots(self) -> None:
        self.browser_showing_roots = True
        self.browser_scroll = 0
        self._refresh_browser()
        self.focus_action = "browse:0" if self.browser_items else "back"

    def _request_confirmation(
        self,
        title: str,
        body: str,
        action: str,
    ) -> None:
        self.confirmation = (title, body, action)
        self.focus_action = "confirm_cancel"

    def _perform_confirmed_action(self, action: str) -> None:
        if action == "unload":
            self._unload_game()
        elif action == "reload" and self.rom_path is not None:
            self._load_game(
                self.rom_path,
                member=self.rom_member,
                reloading=True,
            )
        elif action.startswith("delete:"):
            self._delete_state(int(action.split(":", 1)[1]))

    def _apply_volume(self) -> None:
        volume = 0.0 if self.settings.muted else self.settings.volume
        if self.audio_channel is not None:
            # Channel volume can reset when a new Sound starts. Keep the
            # dedicated transport channel neutral and put the requested gain
            # on every retained Sound instead, so chunk handoffs cannot jump
            # back to full volume.
            self.audio_channel.set_volume(1.0)
        references = getattr(self, "audio_sound_refs", ())
        for sound in references:
            sound.set_volume(volume)
        self._sync_core_sample_generation()

    def _sync_core_sample_generation(self) -> None:
        """Generate host PCM only while an audible mixer is available."""
        if self.console is None:
            return
        target_rate = (
            self.sample_rate
            if (
                self.audio_available
                and not self.settings.muted
                and self.settings.volume > 0
            )
            else 0
        )
        if self.console.apu.sample_rate != target_rate:
            self.console.apu.sample_rate = target_rate
            self.console.apu.drain_samples()

    def _stop_audio_playback(self) -> None:
        if self.audio_channel is not None:
            self.audio_channel.stop()
        self.audio_recovering = False
        self.audio_idle_since = None
        self.audio_inflight = []
        references = getattr(self, "audio_sound_refs", None)
        if references is not None:
            references.clear()

    def _reset_audio_stream(self, *, drain_core: bool = False) -> None:
        self.audio_buffer.clear()
        self.audio_started = False
        self._stop_audio_playback()
        if drain_core and self.console is not None:
            self.console.apu.drain_samples()

    def _set_volume_from_x(self, x: int) -> None:
        if self.volume_rect is None or not self.audio_available:
            return
        value = (x - self.volume_rect.left) / max(1, self.volume_rect.width)
        self.settings.volume = min(1.0, max(0.0, value))
        if self.settings.volume > 0:
            self.settings.muted = False
        self._apply_volume()

    def _toggle_fullscreen(self) -> None:
        self.settings.fullscreen = not self.settings.fullscreen
        self._set_display()
        self._save_settings()

    def _set_scale(self, scale: int) -> None:
        if scale not in WINDOW_SCALES:
            return
        self.settings.scale = scale
        if self.settings.fullscreen:
            self.settings.fullscreen = False
        self._set_display()
        self._save_settings()

    def _capture_key(self, key: int) -> None:
        action = self.capture_binding
        if action is None or self.capture_device != "keyboard":
            return
        if key == self.pg.K_ESCAPE:
            self.capture_binding = None
            self.capture_device = None
            return
        reserved = {
            self.pg.K_p,
            self.pg.K_r,
            self.pg.K_F5,
            self.pg.K_F9,
            self.pg.K_F11,
            self.pg.K_F12,
            *range(self.pg.K_0, self.pg.K_9 + 1),
        }
        if key in reserved:
            self._show_message("That key is reserved for an emulator shortcut", 3)
            return
        new_name = self.pg.key.name(key).lower()
        if not new_name:
            self._show_message("That key cannot be used here", 3)
            return
        old_name = self.settings.key_bindings[action]
        for other_action, binding_name in self.settings.key_bindings.items():
            if other_action != action and binding_name == new_name:
                self.settings.key_bindings[other_action] = old_name
                break
        self.settings.key_bindings[action] = new_name
        self.capture_binding = None
        self.capture_device = None
        self._rebuild_key_bindings()
        self._save_settings()
        self._show_message(f"Control changed to {self._pretty_key(new_name)}")

    @staticmethod
    def _pretty_gamepad_source(source: str) -> str:
        parts = source.split(":")
        if len(parts) == 2 and parts[0] == "button":
            return f"Button {parts[1]}"
        if len(parts) == 3 and parts[0] == "axis":
            direction = "-" if parts[2] == "-" else "+"
            return f"Axis {parts[1]} {direction}"
        if len(parts) == 3 and parts[0] == "hat":
            arrows = {
                "up": "Up",
                "down": "Down",
                "left": "Left",
                "right": "Right",
            }
            return f"Hat {parts[1]} {arrows.get(parts[2], parts[2])}"
        return source

    def _capture_gamepad_source(
        self,
        gamepad: _GamepadState,
        source: str,
    ) -> bool:
        action = self.capture_binding
        if action is None or self.capture_device != "gamepad":
            return False
        if source == "button:10":
            self._show_message("The guide button is reserved for the menu", 3)
            return True

        specifications = {
            row_action: player
            for row_action, _label, player, _button in CONTROL_ROWS
        }
        target_player = specifications[action]
        try:
            source_player = self.joysticks.index(gamepad)
        except ValueError:
            return True
        if source_player != target_player:
            self._show_message(
                f"Use the Player {target_player + 1} controller",
                2,
            )
            return True

        old_sources = self.settings.gamepad_bindings[action].copy()
        for other_action, _label, player, _button in CONTROL_ROWS:
            if other_action == action or player != target_player:
                continue
            if source in self.settings.gamepad_bindings[other_action]:
                self.settings.gamepad_bindings[other_action] = old_sources
                break
        self.settings.gamepad_bindings[action] = [source]
        self.capture_binding = None
        self.capture_device = None
        self._rebuild_gamepad_bindings()
        self._save_settings()
        self._show_message(
            f"Gamepad control changed to "
            f"{self._pretty_gamepad_source(source)}"
        )
        return True

    def _activate(self, action: str) -> None:
        if action == "open":
            self._open_browser()
        elif action == "resume":
            self._set_view("game")
        elif action == "states" and self.has_game:
            self._set_view("states")
        elif action == "rewind" and self.has_game:
            self._rewind(1.0)
        elif action == "settings":
            self._set_view("settings")
        elif action == "controls":
            self.controls_return_view = self.view
            self._set_view("controls")
        elif action.startswith("controls_mode:"):
            mode = action.split(":", 1)[1]
            if mode in ("keyboard", "gamepad"):
                self.controls_input_mode = mode
                self.capture_binding = None
                self.capture_device = None
                self.focus_action = None
        elif action == "back":
            self._go_back()
        elif action == "quit":
            self.running = False
        elif action == "reload" and self.rom_path is not None:
            self._request_confirmation(
                "Reload this game?",
                "The cartridge will restart from power-on. "
                "Save a state first if you want to keep your exact spot.",
                "reload",
            )
        elif action == "unload" and self.has_game:
            self._request_confirmation(
                "Put this game away?",
                "Battery-backed progress will be saved, then the launcher will open.",
                "unload",
            )
        elif action == "browse_parent":
            self._browser_parent()
        elif action == "browse_roots":
            self._show_browser_roots()
        elif action.startswith("browse:"):
            self._enter_browser_item(int(action.split(":", 1)[1]))
        elif action.startswith("archive:"):
            self._enter_archive_item(int(action.split(":", 1)[1]))
        elif action.startswith("recent:"):
            index = int(action.split(":", 1)[1])
            if 0 <= index < len(self.settings.recent_roms):
                self._load_game(self.settings.recent_roms[index])
        elif action.startswith("save:"):
            self._save_state(int(action.split(":", 1)[1]))
        elif action.startswith("load:"):
            self._load_state(int(action.split(":", 1)[1]))
        elif action.startswith("delete:"):
            slot = int(action.split(":", 1)[1])
            self._request_confirmation(
                f"Clear slot {slot}?",
                "This save-state snapshot will be permanently removed.",
                action,
            )
        elif action.startswith("scale:"):
            self._set_scale(int(action.split(":", 1)[1]))
        elif action == "fullscreen":
            self._toggle_fullscreen()
        elif action == "mute" and self.audio_available:
            self.settings.muted = not self.settings.muted
            self._reset_audio_stream(drain_core=True)
            self._apply_volume()
            self._save_settings()
        elif action == "volume":
            pass
        elif action.startswith("rebind:"):
            self.capture_binding = action.split(":", 1)[1]
            self.capture_device = self.controls_input_mode
            self.focus_action = None
        elif action == "reset_controls":
            self.settings.reset_controls()
            self._rebuild_key_bindings()
            self._save_settings()
            self._show_message("Keyboard controls restored")
        elif action == "reset_gamepad_controls":
            self.settings.reset_gamepad_controls()
            self._rebuild_gamepad_bindings()
            self._save_settings()
            self._show_message("Gamepad controls restored")
        elif action == "confirm_cancel":
            self.confirmation = None
            self.focus_action = None
        elif action == "confirm_yes" and self.confirmation is not None:
            confirmed_action = self.confirmation[2]
            self.confirmation = None
            self._perform_confirmed_action(confirmed_action)

    def _navigate_focus(self, delta: int) -> None:
        if not self.focusables:
            return
        try:
            index = self.focusables.index(self.focus_action)
        except ValueError:
            index = 0 if delta >= 0 else len(self.focusables) - 1
        else:
            index = (index + delta) % len(self.focusables)
        self.focus_action = self.focusables[index]
        if self.focus_action.startswith("browse:"):
            item_index = int(self.focus_action.split(":", 1)[1])
            if item_index < self.browser_scroll:
                self.browser_scroll = item_index
            elif item_index >= self.browser_scroll + 9:
                self.browser_scroll = item_index - 8
        elif self.focus_action.startswith("archive:"):
            item_index = int(self.focus_action.split(":", 1)[1])
            if item_index < self.archive_scroll:
                self.archive_scroll = item_index
            elif item_index >= self.archive_scroll + 9:
                self.archive_scroll = item_index - 8

    def _handle_menu_key(self, event) -> None:
        if event.key == self.pg.K_ESCAPE:
            self._go_back()
        elif event.key in (self.pg.K_UP, self.pg.K_LEFT):
            if self.view == "settings" and self.focus_action == "volume":
                self.settings.volume = max(0.0, self.settings.volume - 0.05)
                self.settings.muted = False
                self._apply_volume()
            else:
                self._navigate_focus(-1)
        elif event.key in (self.pg.K_DOWN, self.pg.K_RIGHT):
            if self.view == "settings" and self.focus_action == "volume":
                self.settings.volume = min(1.0, self.settings.volume + 0.05)
                self.settings.muted = False
                self._apply_volume()
            else:
                self._navigate_focus(1)
        elif event.key in (self.pg.K_RETURN, self.pg.K_SPACE):
            if self.focus_action:
                self._activate(self.focus_action)
        elif event.key == self.pg.K_F11:
            self._toggle_fullscreen()

    def _handle_event(self, event) -> None:
        if event.type == self.pg.QUIT:
            self.running = False
            return
        if event.type in (
            getattr(self.pg, "WINDOWEXPOSED", -1),
            getattr(self.pg, "WINDOWSHOWN", -1),
            getattr(self.pg, "VIDEOEXPOSE", -1),
        ):
            self.game_display_dirty = True
            return
        if event.type in (
            getattr(self.pg, "JOYDEVICEADDED", -1),
            getattr(self.pg, "JOYDEVICEREMOVED", -1),
        ):
            if self.gamepads_requested:
                self.gamepad_events += 1
                self._handle_gamepad_device_event(event)
            return
        if event.type == getattr(self.pg, "WINDOWFOCUSLOST", -1):
            self._clear_inputs()
            return

        if event.type == self.pg.KEYDOWN:
            if self.capture_binding is not None:
                if self.capture_device == "keyboard":
                    self._capture_key(event.key)
                elif event.key == self.pg.K_ESCAPE:
                    self.capture_binding = None
                    self.capture_device = None
                return
            if self.view != "game":
                self._handle_menu_key(event)
                return
            if event.key in (self.pg.K_ESCAPE, self.pg.K_p):
                self._open_pause()
            elif event.key == self.pg.K_r and self.console is not None:
                self._reset_console()
            elif event.key == self.pg.K_F5:
                self._save_state()
            elif event.key == self.pg.K_F9:
                self._load_state()
            elif event.key == self.pg.K_F11:
                self._toggle_fullscreen()
            elif event.key == self.pg.K_F12:
                self._save_screenshot()
            elif event.key == self.pg.K_BACKSPACE:
                self._rewind(
                    5.0
                    if event.mod & self.pg.KMOD_SHIFT
                    else 1.0
                )
            elif self.pg.K_0 <= event.key <= self.pg.K_9:
                self.slot = event.key - self.pg.K_0
                self._show_message(f"Quick-save slot {self.slot}")
            else:
                self._set_keyboard_key(event.key, True)
            return

        if event.type == self.pg.KEYUP:
            self._set_keyboard_key(event.key, False)
            return

        if event.type == self.pg.MOUSEMOTION:
            self.mouse_ui = self._screen_to_ui(event.pos)
            if self.volume_dragging:
                self._set_volume_from_x(self.mouse_ui[0])
            return
        if event.type == self.pg.MOUSEBUTTONDOWN and event.button == 1:
            self.mouse_ui = self._screen_to_ui(event.pos)
            if (
                self.view == "settings"
                and self.volume_rect is not None
                and self.volume_rect.inflate(0, 28).collidepoint(self.mouse_ui)
                and self.audio_available
            ):
                self.volume_dragging = True
                self.focus_action = "volume"
                self._set_volume_from_x(self.mouse_ui[0])
            return
        if event.type == self.pg.MOUSEBUTTONUP and event.button == 1:
            self.mouse_ui = self._screen_to_ui(event.pos)
            if self.volume_dragging:
                self.volume_dragging = False
                self._save_settings()
                return
            for rectangle, action, enabled in reversed(self.buttons):
                if enabled and rectangle.collidepoint(self.mouse_ui):
                    self.focus_action = action
                    self._activate(action)
                    break
            return
        if event.type == self.pg.MOUSEWHEEL and self.view in ("browser", "archive"):
            if self.view == "browser":
                self.browser_scroll = min(
                    max(0, len(self.browser_items) - 9),
                    max(0, self.browser_scroll - event.y),
                )
            else:
                self.archive_scroll = min(
                    max(0, len(self.archive_items) - 9),
                    max(0, self.archive_scroll - event.y),
                )
            return

        if event.type in (self.pg.JOYBUTTONDOWN, self.pg.JOYBUTTONUP):
            gamepad = self._gamepad_for_event(event)
            if gamepad is not None:
                gamepad.set_button(
                    int(event.button),
                    event.type == self.pg.JOYBUTTONDOWN,
                )
            self.gamepad_events += 1
            if self.capture_device == "gamepad":
                if (
                    gamepad is not None
                    and event.type == self.pg.JOYBUTTONDOWN
                ):
                    self._capture_gamepad_source(
                        gamepad,
                        f"button:{int(event.button)}",
                    )
                return
            if event.type == self.pg.JOYBUTTONUP:
                return
            if self.view == "game":
                menu_chord = any(
                    (
                        self._mapped_gamepad_buttons(player, state)
                        & int(Button.SELECT)
                        and self._mapped_gamepad_buttons(player, state)
                        & int(Button.START)
                    )
                    for player, state in enumerate(self.joysticks[:2])
                )
                if event.button == 10 or menu_chord:
                    self._open_pause()
            elif event.button == 0 and self.focus_action:
                self._activate(self.focus_action)
            elif event.button == 1:
                self._go_back()
            return
        if event.type == self.pg.JOYHATMOTION:
            gamepad = self._gamepad_for_event(event)
            hat_index = int(getattr(event, "hat", 0))
            if gamepad is not None:
                gamepad.set_hat(hat_index, event.value)
            self.gamepad_events += 1
            horizontal, vertical = event.value
            if self.capture_device == "gamepad":
                if gamepad is not None and (horizontal or vertical):
                    if vertical:
                        direction = "up" if vertical > 0 else "down"
                    else:
                        direction = "right" if horizontal > 0 else "left"
                    self._capture_gamepad_source(
                        gamepad,
                        f"hat:{hat_index}:{direction}",
                    )
                return
            if self.view == "game":
                return
            if vertical > 0 or horizontal < 0:
                self._navigate_focus(-1)
            elif vertical < 0 or horizontal > 0:
                self._navigate_focus(1)
            return
        if event.type == self.pg.JOYAXISMOTION:
            gamepad = self._gamepad_for_event(event)
            axis = int(event.axis)
            value = float(event.value)
            if gamepad is not None:
                gamepad.set_axis(axis, value)
            self.gamepad_events += 1
            if self.capture_device == "gamepad":
                if gamepad is not None and abs(value) >= 0.75:
                    self._capture_gamepad_source(
                        gamepad,
                        f"axis:{axis}:{'+' if value > 0 else '-'}",
                    )
                return
            if self.view == "game":
                return
            if axis not in (0, 1):
                return
            direction = -1 if value < -0.55 else 1 if value > 0.55 else 0
            previous = self.menu_axes[axis]
            self.menu_axes[axis] = direction
            if direction and not previous:
                if (axis == 0 and direction < 0) or (
                    axis == 1 and direction < 0
                ):
                    self._navigate_focus(-1)
                else:
                    self._navigate_focus(1)

    def _queue_audio(self, samples=None) -> None:
        if samples is None:
            if self.console is None:
                return
            samples = self.console.apu.drain_samples()
        if (
            not self.audio_available
            or self.audio_channel is None
            or self.settings.muted
            or self.settings.volume <= 0
        ):
            self.audio_buffer.clear()
            self.audio_started = False
            self._stop_audio_playback()
            return
        if samples and self.audio_buffer.append(samples):
            # The mixer stopped consuming for long enough to create noticeable
            # latency. Restart from a bounded amount of newest audio.
            self._stop_audio_playback()
            self.audio_started = False

        self._pump_audio()

    def _accept_emulated_frame(
        self,
        packet: EmulatedFrame,
        frame_period: float,
    ) -> None:
        worker = self.emulation_worker
        try:
            if packet.video_changed:
                self.frame[:] = packet.pixels
                self.game_frame_dirty = True
                self.game_display_dirty = True
            else:
                self.unchanged_video_frames += 1
        finally:
            if worker is not None:
                worker.release(packet)
        self.latest_frame_number = packet.frame_number
        if self.diagnostics:
            self._record_emulation_time(
                packet.emulation_seconds,
                packet.emulation_cpu_seconds,
            )
            if packet.emulation_seconds > frame_period:
                self.frame_budget_misses += 1
        self._queue_audio(packet.samples)

    def _wait_for_emulated_frame(
        self,
        deadline: float,
    ) -> EmulatedFrame | None:
        """Wait for one ordered frame while servicing the host audio queue."""
        worker = self.emulation_worker
        if worker is None:
            return None
        packet = worker.get()
        if packet is not None:
            return packet
        while self.running and self.view == "game":
            now = time.perf_counter()
            remaining = deadline - now
            if remaining <= 0:
                return None
            self._pump_audio()
            packet = worker.get(timeout=min(AUDIO_PUMP_INTERVAL, remaining))
            if packet is not None:
                return packet
        return None

    def _pump_audio(self) -> None:
        if (
            not self.audio_available
            or self.audio_channel is None
            or self.settings.muted
            or self.settings.volume <= 0
        ):
            return
        if not self.audio_started:
            if self.audio_buffer.pending_samples < self.audio_prebuffer_samples:
                return
            self.audio_started = True
            self.audio_inflight = []

        now = time.perf_counter()
        inflight = self.audio_inflight
        while inflight and now >= inflight[0]:
            inflight.pop(0)

        chunk_samples = self.audio_buffer.chunk_samples
        sample_rate = self.sample_rate
        chunk_seconds = (
            chunk_samples / sample_rate
            if sample_rate > 0
            else AUDIO_IDLE_GRACE_SECONDS
        )

        if len(inflight) >= 2:
            # Both native slots are still estimated to hold unfinished audio.
            self.audio_idle_since = None
            return

        if self.audio_buffer.pending_samples < chunk_samples:
            # Nothing ready to submit for a slot our own clock believes is
            # free. This is a genuine gap only if it persists.
            idle_since = self.audio_idle_since
            if idle_since is None:
                self.audio_idle_since = now
                return
            if now - idle_since < AUDIO_IDLE_GRACE_SECONDS:
                return
            # Force the channel back to a known state and rebuild the
            # reservoir; fade only the recovered emulated PCM back in. A
            # synthetic tail played after the gap would be too late and
            # could race whatever the native channel is actually doing.
            self.audio_channel.stop()
            self.audio_inflight = []
            self.audio_underruns += 1
            self.audio_started = False
            self.audio_recovering = True
            self.audio_idle_since = None
            return
        self.audio_idle_since = None

        while (
            len(inflight) < 2
            and self.audio_buffer.pending_samples >= chunk_samples
        ):
            pcm_view = self.audio_buffer.peek_chunk()
            if pcm_view is None:
                break
            try:
                # Give the native constructor an immutable owner. pygame-ce
                # copies this buffer again internally, so the small bytes
                # snapshot adds negligible work while removing a borrowed
                # mutable view from the SDL boundary.
                pcm = bytes(pcm_view)
            finally:
                pcm_view.release()
            recovering = bool(getattr(self, "audio_recovering", False))
            if recovering:
                # After a confirmed idle gap the host is already outputting
                # silence. Fade the first recovered PCM chunk up from zero so
                # its first nonzero sample cannot click. This touches only
                # audio after real loss; uninterrupted emulated PCM is exact.
                recovered = array("h")
                recovered.frombytes(pcm)
                count = min(
                    len(recovered),
                    AUDIO_CONCEALMENT_SAMPLES,
                    max(2, chunk_samples // 8),
                )
                if count >= 2:
                    denominator = count - 1
                    for index in range(count):
                        recovered[index] = round(
                            recovered[index] * index / denominator
                        )
                    pcm = recovered.tobytes()
            try:
                sound = self.pg.mixer.Sound(buffer=pcm)
                sound.set_volume(
                    0.0 if self.settings.muted else self.settings.volume
                )
                # Channel.queue fills the next native slot when one is
                # already playing and starts the Sound immediately when the
                # channel is idle. It is called here purely to submit PCM;
                # whether/when SDL_mixer's own get_busy()/get_queue() ever
                # reflect that submission does not gate anything above.
                self.audio_channel.queue(sound)
            except self.pg.error:
                # Keep the exact PCM in staging for the next host pump. Never
                # stop the channel here: playback may still own whatever it
                # already had queued, so keep the stream started and retry
                # this exact chunk at the next service point rather than
                # waiting for a new prebuffer.
                self.audio_start_failures += 1
                break

            # Advance staging only after the mixer accepted this exact chunk.
            self.audio_buffer.discard_chunk()
            start_at = inflight[-1] if inflight else now
            inflight.append(start_at + chunk_seconds)
            if recovering:
                self.audio_recovering = False
                self.audio_recovery_fades = (
                    int(getattr(self, "audio_recovery_fades", 0)) + 1
                )
            self.audio_sound_refs.append(sound)

    @staticmethod
    def _fit_rect(
        source_width: int,
        source_height: int,
        target_width: int,
        target_height: int,
    ) -> tuple[int, int, int, int]:
        scale = min(target_width / source_width, target_height / source_height)
        width = max(1, round(source_width * scale))
        height = max(1, round(source_height * scale))
        return (
            (target_width - width) // 2,
            (target_height - height) // 2,
            width,
            height,
        )

    def _screen_to_ui(self, position: tuple[int, int]) -> tuple[int, int]:
        if self.ui_screen_rect is None:
            return position
        x, y, width, height = self.ui_screen_rect
        if not (x <= position[0] < x + width and y <= position[1] < y + height):
            return (-10_000, -10_000)
        return (
            round((position[0] - x) * UI_WIDTH / width),
            round((position[1] - y) * UI_HEIGHT / height),
        )

    def _font(self, size: int, bold: bool = False):
        key = (size, bold)
        font = self.fonts.get(key)
        if font is None:
            font = self.pg.font.SysFont(
                "dejavusansmono,nimbusmonops,couriernew,courier,monospace",
                size,
                bold=bold,
            )
            self.fonts[key] = font
        return font

    def _text(
        self,
        text: str,
        size: int,
        color: tuple[int, int, int],
        position: tuple[int, int],
        *,
        center: bool = False,
        bold: bool = False,
        surface=None,
    ):
        target = surface or self.ui_surface
        rendered = self._font(size, bold).render(text, False, color)
        rectangle = rendered.get_rect()
        if center:
            rectangle.center = position
        else:
            rectangle.topleft = position
        target.blit(rendered, rectangle)
        return rectangle

    def _ellipsize(self, text: str, size: int, width: int) -> str:
        font = self._font(size)
        if font.size(text)[0] <= width:
            return text
        shortened = text
        while shortened and font.size(shortened + "...")[0] > width:
            shortened = shortened[:-1]
        return shortened + "..."

    @staticmethod
    def _size_label(size_bytes: int) -> str:
        if size_bytes >= 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.1f} MiB"
        if size_bytes >= 1024:
            return f"{size_bytes / 1024:.1f} KiB"
        return f"{size_bytes} B"

    def _panel(
        self,
        rectangle,
        color=PAPER,
        *,
        radius: int = 24,
        border: tuple[int, int, int] | None = None,
    ) -> None:
        del radius
        self._pixel_box(
            self.pg.Rect(rectangle),
            color,
            border or PIXEL_SHADOW,
            shadow=True,
            cut=8,
        )

    def _pixel_box(
        self,
        rectangle,
        color: tuple[int, int, int],
        border: tuple[int, int, int] = PIXEL_SHADOW,
        *,
        shadow: bool = False,
        cut: int = 0,
        width: int = 4,
    ) -> None:
        """Draw a hard-edged box with optional cartridge-like clipped corners."""
        rectangle = self.pg.Rect(rectangle)

        def points(rect):
            corner = max(0, min(cut, rect.width // 4, rect.height // 4))
            return (
                (rect.left + corner, rect.top),
                (rect.right - corner - 1, rect.top),
                (rect.right - 1, rect.top + corner),
                (rect.right - 1, rect.bottom - corner - 1),
                (rect.right - corner - 1, rect.bottom - 1),
                (rect.left + corner, rect.bottom - 1),
                (rect.left, rect.bottom - corner - 1),
                (rect.left, rect.top + corner),
            )

        if shadow:
            shadow_rect = rectangle.move(8, 8)
            self.pg.draw.polygon(
                self.ui_surface,
                PIXEL_SHADOW,
                points(shadow_rect),
            )
        self.pg.draw.polygon(self.ui_surface, border, points(rectangle))
        inner = rectangle.inflate(-width * 2, -width * 2)
        inner_cut = max(0, cut - width)
        original_cut = cut
        cut = inner_cut
        self.pg.draw.polygon(self.ui_surface, color, points(inner))
        cut = original_cut

    def _pixel_background(self, *, overlay: bool = False) -> None:
        """Paint a deterministic tiled backdrop that stays crisp when scaled."""
        alpha = 238 if overlay else 255
        self.ui_surface.fill((*NAVY, alpha))
        tile = 24
        dark = (*PIXEL_SHADOW, alpha)
        blue = (*PIXEL_BLUE, alpha)
        for y in range(0, UI_HEIGHT, tile):
            for x in range(0, UI_WIDTH, tile):
                if ((x // tile) + (y // tile)) % 2 == 0:
                    self.pg.draw.rect(
                        self.ui_surface,
                        blue,
                        (x, y, tile, tile),
                    )
        self.pg.draw.rect(self.ui_surface, dark, (0, 0, UI_WIDTH, 20))
        self.pg.draw.rect(
            self.ui_surface,
            (*CORAL_DARK, alpha),
            (0, 20, UI_WIDTH, 8),
        )
        self.pg.draw.rect(
            self.ui_surface,
            (*CORAL, alpha),
            (0, UI_HEIGHT - 16, UI_WIDTH, 8),
        )
        self.pg.draw.rect(
            self.ui_surface,
            dark,
            (0, UI_HEIGHT - 8, UI_WIDTH, 8),
        )
        for x in range(0, UI_WIDTH, 32):
            color = (*PIXEL_LIGHT, alpha) if (x // 32) % 2 else dark
            self.pg.draw.rect(self.ui_surface, color, (x, 4, 16, 8))

    def _button(
        self,
        rectangle,
        label: str,
        action: str,
        *,
        primary: bool = False,
        enabled: bool = True,
        small: bool = False,
        selected: bool = False,
    ) -> None:
        rectangle = self.pg.Rect(rectangle)
        if enabled:
            self.focusables.append(action)
            if self.focus_action is None:
                self.focus_action = action
        focused = enabled and (
            action == self.focus_action or rectangle.collidepoint(self.mouse_ui)
        )
        if not enabled:
            color, text_color = (PIXEL_GREY, MUTED)
        elif primary:
            color = CORAL_DARK if focused else CORAL
            text_color = WHITE
        elif selected:
            color = MINT_DARK if focused else MINT
            text_color = WHITE if focused else INK
        else:
            color = SKY if focused else PIXEL_LIGHT
            text_color = INK
        self._pixel_box(
            rectangle,
            color,
            SUN if focused else PIXEL_SHADOW,
            shadow=True,
            cut=4,
            width=3,
        )
        if focused:
            marker_x = rectangle.left + 9
            marker_y = rectangle.centery
            self.pg.draw.polygon(
                self.ui_surface,
                WHITE if primary else CORAL_DARK,
                (
                    (marker_x, marker_y - 6),
                    (marker_x + 8, marker_y),
                    (marker_x, marker_y + 6),
                ),
            )
        self._text(
            label,
            17 if small else 21,
            text_color,
            rectangle.center,
            center=True,
            bold=True,
        )
        self.buttons.append((rectangle, action, enabled))

    def _sparkle(
        self,
        x: int,
        y: int,
        color: tuple[int, int, int],
        size: int = 10,
    ) -> None:
        unit = max(2, size // 3)
        self.pg.draw.rect(
            self.ui_surface,
            color,
            (x - unit, y - size, unit * 2, size * 2),
        )
        self.pg.draw.rect(
            self.ui_surface,
            color,
            (x - size, y - unit, size * 2, unit * 2),
        )
        self.pg.draw.rect(
            self.ui_surface,
            WHITE,
            (x - unit, y - unit, unit * 2, unit * 2),
        )

    def _draw_controller(self, center: tuple[int, int], scale: float = 1.0) -> None:
        x, y = center
        body = self.pg.Rect(0, 0, round(250 * scale), round(135 * scale))
        body.center = center
        self._pixel_box(body, PIXEL_GREY, PIXEL_SHADOW, shadow=True, cut=8)
        inner = body.inflate(round(-18 * scale), round(-18 * scale))
        self._pixel_box(inner, PIXEL_LIGHT, INK, cut=4, width=3)
        dpad_x = x - round(62 * scale)
        dpad_y = y
        self.pg.draw.rect(
            self.ui_surface,
            NAVY,
            self.pg.Rect(
                dpad_x - round(12 * scale),
                dpad_y - round(36 * scale),
                round(24 * scale),
                round(72 * scale),
            ),
        )
        self.pg.draw.rect(
            self.ui_surface,
            NAVY,
            self.pg.Rect(
                dpad_x - round(36 * scale),
                dpad_y - round(12 * scale),
                round(72 * scale),
                round(24 * scale),
            ),
        )
        for offset, color in ((35, CORAL_DARK), (72, CORAL)):
            radius = round(15 * scale)
            self.pg.draw.polygon(
                self.ui_surface,
                color,
                (
                    (x + round(offset * scale) - radius // 2, y - round(5 * scale) - radius),
                    (x + round(offset * scale) + radius // 2, y - round(5 * scale) - radius),
                    (x + round(offset * scale) + radius, y - round(5 * scale) - radius // 2),
                    (x + round(offset * scale) + radius, y - round(5 * scale) + radius // 2),
                    (x + round(offset * scale) + radius // 2, y - round(5 * scale) + radius),
                    (x + round(offset * scale) - radius // 2, y - round(5 * scale) + radius),
                    (x + round(offset * scale) - radius, y - round(5 * scale) + radius // 2),
                    (x + round(offset * scale) - radius, y - round(5 * scale) - radius // 2),
                ),
            )
        for offset in (-15, 15):
            self.pg.draw.rect(
                self.ui_surface,
                (198, 190, 207),
                self.pg.Rect(
                    x + round(offset * scale) - round(10 * scale),
                    y + round(34 * scale),
                    round(20 * scale),
                    round(7 * scale),
                ),
            )

    def _draw_home(self) -> None:
        self._pixel_background()
        self.pg.draw.rect(self.ui_surface, CORAL_DARK, (48, 48, 112, 24))
        self.pg.draw.rect(self.ui_surface, CORAL, (64, 40, 80, 40))
        self.pg.draw.rect(self.ui_surface, MINT_DARK, (820, 640, 96, 24))
        self.pg.draw.rect(self.ui_surface, MINT, (836, 624, 64, 56))
        self._sparkle(880, 82, CORAL, 14)
        self._sparkle(105, 630, SUN, 11)
        if self.brand_wordmark is not None:
            wordmark_rect = self.brand_wordmark.get_rect(
                center=(480, 68)
            )
            self.ui_surface.blit(self.brand_wordmark, wordmark_rect)
        else:
            self._text(
                BRAND_NAME.upper(),
                49,
                WHITE,
                (480, 65),
                center=True,
                bold=True,
            )
        self._text(
            "- PYTHON-POWERED 8-BIT EMULATION -",
            17,
            SUN,
            (480, 130),
            center=True,
            bold=True,
        )

        art_panel = self.pg.Rect(55, 165, 350, 480)
        self._panel(art_panel, PAPER, border=CORAL)
        logo_frame = self.pg.Rect(88, 190, 284, 298)
        self._pixel_box(
            logo_frame,
            NAVY,
            MINT_DARK,
            shadow=True,
            cut=6,
        )
        if self.brand_mascot is not None:
            mascot_rect = self.brand_mascot.get_rect(
                center=logo_frame.center
            )
            self.ui_surface.blit(self.brand_mascot, mascot_rect)
        else:
            self._draw_controller(logo_frame.center, 0.82)
        self._text(
            "PYTHON POWERED",
            20,
            CORAL_DARK,
            (230, 522),
            center=True,
            bold=True,
        )
        self._text(
            "8-BIT EMULATION",
            20,
            MINT_DARK,
            (230, 552),
            center=True,
            bold=True,
        )
        self._text(
            "READY PLAYER 1",
            16,
            INK,
            (230, 610),
            center=True,
            bold=True,
        )

        library_panel = self.pg.Rect(430, 165, 475, 480)
        self._panel(library_panel, PAPER, border=SKY)
        self._text("GAME SELECT", 29, INK, (465, 190), bold=True)
        self._button(
            (745, 185, 125, 48),
            "LOAD ROM",
            "open",
            primary=True,
        )
        self._text("RECENT CARTRIDGES", 16, MUTED, (465, 249), bold=True)

        recents = self.settings.recent_roms[:4]
        if not recents:
            self._text(
                "NO GAMES FOUND",
                22,
                MUTED,
                (667, 360),
                center=True,
            )
            self._text(
                "LOAD HOMEBREW OR YOUR OWN CARTRIDGE DUMP",
                14,
                MUTED,
                (667, 395),
                center=True,
            )
        for index, path_text in enumerate(recents):
            path = Path(path_text)
            available = path.is_file()
            y = 270 + index * 72
            card = self.pg.Rect(460, y, 415, 58)
            color = PIXEL_LIGHT if available else PIXEL_GREY
            self._pixel_box(card, color, PIXEL_SHADOW, cut=3, width=2)
            self.pg.draw.rect(
                self.ui_surface,
                CORAL if available else (180, 176, 183),
                (475, y + 17, 24, 24),
            )
            self._text(
                self._ellipsize(path.stem, 19, 235),
                19,
                INK if available else MUTED,
                (512, y + 10),
                bold=True,
            )
            self._text(
                self._ellipsize(str(path.parent), 13, 235),
                13,
                MUTED,
                (512, y + 34),
            )
            self._button(
                (780, y + 10, 78, 38),
                "PLAY",
                f"recent:{index}",
                primary=available,
                enabled=available,
                small=True,
            )
        self._button((460, 585, 125, 40), "OPTIONS", "settings", small=True)
        self._button((595, 585, 125, 40), "CONTROLS", "controls", small=True)
        self._button((730, 585, 140, 40), "POWER OFF", "quit", small=True)

    def _draw_game_preview(self, rectangle) -> None:
        self._pixel_box(rectangle, NAVY, PIXEL_SHADOW, shadow=True, cut=4)
        if self.console is None:
            return
        target = rectangle.inflate(-16, -16)
        scaled = self.pg.transform.scale(self.frame_surface, target.size)
        self.ui_surface.blit(scaled, target)
        self.pg.draw.rect(self.ui_surface, WHITE, target, width=3)

    def _draw_pause(self) -> None:
        self._pixel_background(overlay=True)
        panel = self.pg.Rect(110, 62, 740, 596)
        self._panel(panel, CREAM, border=CORAL)
        self._text("GAME PAUSED", 40, INK, (480, 100), center=True, bold=True)
        self._sparkle(325, 100, CORAL, 10)
        self._sparkle(635, 100, MINT_DARK, 10)
        preview = self.pg.Rect(155, 150, 340, 320)
        self._draw_game_preview(preview)
        title = self.console.cartridge.title if self.console else "No game"
        self._text(
            self._ellipsize(title, 25, 330),
            25,
            INK,
            (325, 500),
            center=True,
            bold=True,
        )
        self._text(
            f"ACTIVE SAVE SLOT: {self.slot}",
            17,
            MUTED,
            (325, 535),
            center=True,
        )
        self._text(
            "BACKSPACE REWIND // F5 SAVE // F9 LOAD",
            14,
            MUTED,
            (325, 566),
            center=True,
        )

        x = 540
        self._button((x, 150, 260, 55), "CONTINUE", "resume", primary=True)
        self._button(
            (x, 218, 260, 46),
            "REWIND 1 SECOND",
            "rewind",
            enabled=len(self.rewind_buffer) > 1,
        )
        self._button((x, 276, 260, 46), "SAVE SLOTS", "states")
        self._button((x, 334, 260, 46), "OPTIONS", "settings")
        self._button((x, 392, 260, 46), "CONTROLS", "controls")
        self._button((x, 450, 125, 42), "RELOAD", "reload", small=True)
        self._button((675, 450, 125, 42), "EJECT", "unload", small=True)
        self._button((x, 504, 260, 42), "CHANGE GAME", "open", small=True)
        self._button((x, 558, 260, 38), "POWER OFF", "quit", small=True)

    def _thumbnail(self, info: StateSlotInfo, size: tuple[int, int]):
        if not info.exists or not info.thumbnail_path.exists():
            return None
        try:
            modified = info.thumbnail_path.stat().st_mtime
        except OSError:
            return None
        key = (str(info.thumbnail_path), modified, size)
        cached = self.thumbnails.get(key)
        if cached is not None:
            return cached
        try:
            image = self.pg.image.load(info.thumbnail_path)
            scaled = self.pg.transform.scale(image, size)
        except (OSError, self.pg.error):
            return None
        self.thumbnails[key] = scaled
        return scaled

    def _draw_states(self) -> None:
        self._pixel_background(overlay=True)
        panel = self.pg.Rect(35, 34, 890, 652)
        self._panel(panel, CREAM, border=MINT)
        self._text("SAVE SLOT SELECT", 38, INK, (480, 70), center=True, bold=True)
        self._text(
            "CHOOSE A MEMORY SLOT // SAVE OR RESTORE",
            18,
            MUTED,
            (480, 106),
            center=True,
        )
        if self.console is None:
            return
        slots = state_slots(self.state_directory, self.console.cartridge.sha256)
        for info in slots:
            column = info.slot // 5
            row = info.slot % 5
            x = 62 + column * 440
            y = 137 + row * 91
            card = self.pg.Rect(x, y, 414, 78)
            card_color = (192, 224, 176) if info.exists else PIXEL_LIGHT
            self._pixel_box(card, card_color, PIXEL_SHADOW, cut=4, width=2)
            if info.slot == self.slot:
                self.pg.draw.rect(self.ui_surface, CORAL, card, width=4)
            thumb_rect = self.pg.Rect(x + 8, y + 8, 76, 62)
            thumb = self._thumbnail(info, thumb_rect.size)
            if thumb is not None:
                self.ui_surface.blit(thumb, thumb_rect)
            else:
                self._pixel_box(
                    thumb_rect,
                    LAVENDER if info.exists else PIXEL_GREY,
                    PIXEL_SHADOW,
                    cut=2,
                    width=2,
                )
                self._text(
                    str(info.slot),
                    28,
                    WHITE if info.exists else MUTED,
                    thumb_rect.center,
                    center=True,
                    bold=True,
                )
            self._text(f"Slot {info.slot}", 19, INK, (x + 96, y + 10), bold=True)
            self._text(info.timestamp_label, 12, MUTED, (x + 96, y + 37))
            self._button(
                (x + 240, y + 10, 72, 26),
                "SAVE",
                f"save:{info.slot}",
                primary=info.slot == self.slot,
                small=True,
            )
            self._button(
                (x + 322, y + 10, 78, 26),
                "LOAD",
                f"load:{info.slot}",
                enabled=info.exists,
                small=True,
            )
            self._button(
                (x + 240, y + 43, 160, 25),
                "CLEAR SLOT",
                f"delete:{info.slot}",
                enabled=info.exists,
                small=True,
            )
        self._button((395, 615, 170, 44), "BACK", "back", primary=True)

    def _draw_settings(self) -> None:
        self._pixel_background(overlay=True)
        panel = self.pg.Rect(125, 55, 710, 610)
        self._panel(panel, CREAM, border=SUN)
        self._text("SYSTEM OPTIONS", 40, INK, (480, 100), center=True, bold=True)
        self._sparkle(310, 100, SUN, 10)
        self._sparkle(650, 100, CORAL, 10)

        self._text("SOUND LEVEL", 25, INK, (180, 165), bold=True)
        status = (
            f"{round(self.settings.volume * 100)}%"
            if self.audio_available
            else "Audio disabled by launch option"
        )
        self._text(status, 15, MUTED, (650, 170), center=True)
        self.volume_rect = self.pg.Rect(230, 220, 420, 14)
        self.pg.draw.rect(self.ui_surface, PIXEL_SHADOW, self.volume_rect)
        self.pg.draw.rect(
            self.ui_surface,
            PIXEL_GREY,
            self.volume_rect.inflate(-4, -4),
        )
        fill_width = round(self.volume_rect.width * self.settings.volume)
        if fill_width:
            fill = self.volume_rect.copy()
            fill.width = fill_width
            self.pg.draw.rect(self.ui_surface, CORAL, fill)
        knob_x = self.volume_rect.left + fill_width
        knob = self.pg.Rect(0, 0, 22, 30)
        knob.center = (knob_x, self.volume_rect.centery)
        self._pixel_box(
            knob,
            WHITE,
            CORAL_DARK,
            cut=3,
            width=3,
        )
        volume_hit = self.volume_rect.inflate(0, 32)
        self.buttons.append((volume_hit, "volume", self.audio_available))
        if self.audio_available:
            self.focusables.append("volume")
            if self.focus_action is None:
                self.focus_action = "volume"
        self._button(
            (680, 205, 105, 44),
            "UNMUTE" if self.settings.muted else "MUTE",
            "mute",
            selected=self.settings.muted,
            enabled=self.audio_available,
            small=True,
        )

        self.pg.draw.line(
            self.ui_surface,
            PIXEL_GREY,
            (175, 290),
            (785, 290),
            4,
        )
        self._text("DISPLAY SIZE", 25, INK, (180, 326), bold=True)
        for index, scale in enumerate(WINDOW_SCALES):
            self._button(
                (360 + index * 125, 315, 105, 48),
                f"{scale}x",
                f"scale:{scale}",
                selected=self.settings.scale == scale
                and not self.settings.fullscreen,
            )
        resolution = (
            "Fullscreen"
            if self.settings.fullscreen
            else f"{PPU_WIDTH * self.settings.scale} x "
            f"{PPU_HEIGHT * self.settings.scale}"
        )
        self._text(resolution, 16, MUTED, (685, 380), center=True)
        self._button(
            (360, 385, 355, 50),
            "WINDOW MODE" if self.settings.fullscreen else "FULL SCREEN",
            "fullscreen",
            selected=self.settings.fullscreen,
        )

        self.pg.draw.line(
            self.ui_surface,
            PIXEL_GREY,
            (175, 475),
            (785, 475),
            4,
        )
        self._text("INPUT & SHORTCUTS", 25, INK, (180, 505), bold=True)
        self._button(
            (555, 492, 225, 44),
            "REMAP CONTROLS",
            "controls",
            small=True,
        )
        self._text(
            "ESC MENU // F5 SAVE // F9 LOAD // F11 SCREEN // F12 SHOT",
            15,
            MUTED,
            (480, 557),
            center=True,
        )
        self._text(
            "OPTIONS AUTO-SAVE",
            15,
            MINT_DARK,
            (480, 586),
            center=True,
            bold=True,
        )
        self._button((395, 610, 170, 42), "BACK", "back", primary=True)

    @staticmethod
    def _pretty_key(name: str) -> str:
        replacements = {
            "return": "Enter",
            "right shift": "Right Shift",
            "left shift": "Left Shift",
            "up": "Up Arrow",
            "down": "Down Arrow",
            "left": "Left Arrow",
            "right": "Right Arrow",
            "space": "Space",
        }
        return replacements.get(name, name.upper() if len(name) == 1 else name.title())

    def _draw_controls(self) -> None:
        self._pixel_background(overlay=True)
        mode = self.controls_input_mode
        self._text("CONTROLLER SETUP", 38, WHITE, (480, 34), center=True, bold=True)
        self._text(
            (
                "SELECT AN ACTION, THEN PRESS A NEW KEY"
                if mode == "keyboard"
                else "SELECT AN ACTION, THEN MOVE OR PRESS ITS GAMEPAD INPUT"
            ),
            16,
            SUN,
            (480, 70),
            center=True,
        )
        self._button(
            (300, 94, 170, 38),
            "KEYBOARD",
            "controls_mode:keyboard",
            selected=mode == "keyboard",
            small=True,
        )
        self._button(
            (490, 94, 170, 38),
            "GAMEPAD",
            "controls_mode:gamepad",
            selected=mode == "gamepad",
            small=True,
        )
        for player in range(2):
            x = 45 + player * 455
            panel = self.pg.Rect(x, 145, 415, 430)
            self._panel(panel, CREAM, border=CORAL if player == 0 else MINT)
            self._text(
                f"PLAYER {player + 1}",
                25,
                INK,
                (x + 207, 168),
                center=True,
                bold=True,
            )
            rows = [row for row in CONTROL_ROWS if row[2] == player]
            for index, (action, label, _player, _button) in enumerate(rows):
                y = 197 + index * 44
                self._text(label, 16, INK, (x + 25, y + 8), bold=True)
                if mode == "keyboard":
                    binding_label = self._pretty_key(
                        self.settings.key_bindings[action]
                    )
                else:
                    sources = self.settings.gamepad_bindings[action]
                    binding_label = self._pretty_gamepad_source(sources[0])
                    if len(sources) > 1:
                        binding_label += f" +{len(sources) - 1}"
                self._button(
                    (x + 205, y, 185, 34),
                    self._ellipsize(binding_label, 15, 160),
                    f"rebind:{action}",
                    selected=(
                        self.capture_binding == action
                        and self.capture_device == mode
                    ),
                    small=True,
                )
        self._text(
            (
                f"{len(self.joysticks)} GAMEPAD"
                f"{'' if len(self.joysticks) == 1 else 'S'} CONNECTED"
                if mode == "gamepad"
                else "GAMEPAD REMAPPING IS AVAILABLE ON THE GAMEPAD TAB"
            ),
            13,
            WHITE,
            (480, 606),
            center=True,
        )
        self._button(
            (250, 650, 210, 42),
            "RESET KEYS" if mode == "keyboard" else "RESET GAMEPAD",
            (
                "reset_controls"
                if mode == "keyboard"
                else "reset_gamepad_controls"
            ),
            small=True,
        )
        self._button((500, 650, 210, 42), "BACK", "back", primary=True, small=True)

    def _draw_browser(self) -> None:
        self._pixel_background(overlay=self.has_game)
        panel = self.pg.Rect(75, 45, 810, 630)
        self._panel(panel, PAPER, border=SKY)
        self._text("LOAD CARTRIDGE", 38, INK, (480, 82), center=True, bold=True)
        self._text(
            "SELECT .NES ROM OR .ZIP COLLECTION",
            16,
            MUTED,
            (480, 116),
            center=True,
        )
        path_text = (
            "THIS PC // SELECT A DRIVE"
            if self.browser_showing_roots
            else self._ellipsize(str(self.browser_directory), 16, 440)
        )
        self._pixel_box(
            self.pg.Rect(115, 145, 470, 42),
            PIXEL_LIGHT,
            PIXEL_SHADOW,
            cut=3,
            width=2,
        )
        self._text(path_text, 16, INK, (130, 156))
        self._button(
            (600, 145, 110, 42),
            "DRIVES",
            "browse_roots",
            selected=self.browser_showing_roots,
            small=True,
        )
        self._button(
            (725, 145, 115, 42),
            "UP",
            "browse_parent",
            enabled=not self.browser_showing_roots,
            small=True,
        )

        visible = self.browser_items[
            self.browser_scroll:self.browser_scroll + 9
        ]
        if not visible:
            self._text(
                (
                    "NO DRIVES AVAILABLE"
                    if self.browser_showing_roots
                    else "NO .NES OR .ZIP FILES HERE"
                ),
                22,
                MUTED,
                (480, 370),
                center=True,
            )
        for row, entry in enumerate(visible):
            absolute_index = self.browser_scroll + row
            y = 205 + row * 45
            rectangle = self.pg.Rect(115, y, 725, 38)
            is_hovered = rectangle.collidepoint(self.mouse_ui)
            if entry.is_directory:
                color = (192, 224, 176)
                active_color = MINT
                badge = "DRV" if self.browser_showing_roots else "DIR"
                badge_color = MINT_DARK
            elif entry.is_archive:
                color = (184, 216, 232)
                active_color = SKY
                badge = "ZIP"
                badge_color = (47, 132, 171)
            else:
                color = (240, 200, 184)
                active_color = PINK
                badge = "NES"
                badge_color = CORAL_DARK
            if is_hovered or self.focus_action == f"browse:{absolute_index}":
                color = active_color
            self._pixel_box(
                rectangle,
                color,
                SUN if is_hovered else PIXEL_SHADOW,
                cut=3,
                width=2,
            )
            self.pg.draw.rect(
                self.ui_surface,
                badge_color,
                self.pg.Rect(125, y + 6, 52, 26),
            )
            self._text(badge, 13, WHITE, (151, y + 19), center=True, bold=True)
            self._text(
                self._ellipsize(entry.label, 18, 615),
                18,
                INK,
                (190, y + 8),
                bold=not entry.is_directory,
            )
            action = f"browse:{absolute_index}"
            self.buttons.append((rectangle, action, True))
            self.focusables.append(action)
            if self.focus_action is None:
                self.focus_action = action
        if len(self.browser_items) > 9:
            count = len(self.browser_items)
            track = self.pg.Rect(850, 205, 8, 398)
            self.pg.draw.rect(self.ui_surface, PIXEL_GREY, track)
            thumb_height = max(35, round(track.height * 9 / count))
            travel = track.height - thumb_height
            maximum = max(1, count - 9)
            thumb_y = track.y + round(travel * self.browser_scroll / maximum)
            self.pg.draw.rect(
                self.ui_surface,
                CORAL,
                self.pg.Rect(track.x, thumb_y, track.width, thumb_height),
            )
        self._button((395, 620, 170, 42), "BACK", "back", primary=True)

    def _draw_archive(self) -> None:
        self._pixel_background(overlay=self.has_game)
        panel = self.pg.Rect(75, 45, 810, 630)
        self._panel(panel, PAPER, border=SKY)
        self._text("ZIP GAME SELECT", 38, INK, (480, 82), center=True, bold=True)
        archive_name = (
            self.pending_archive_path.name
            if self.pending_archive_path is not None
            else "ZIP collection"
        )
        self._text(
            f"{archive_name} // {len(self.archive_items)} NES ROMS",
            16,
            MUTED,
            (480, 116),
            center=True,
        )
        self._pixel_box(
            self.pg.Rect(115, 145, 725, 42),
            PIXEL_LIGHT,
            PIXEL_SHADOW,
            cut=3,
            width=2,
        )
        archive_path = (
            str(self.pending_archive_path)
            if self.pending_archive_path is not None
            else archive_name
        )
        self._text(
            self._ellipsize(archive_path, 16, 690),
            16,
            INK,
            (130, 156),
        )

        visible = self.archive_items[
            self.archive_scroll:self.archive_scroll + 9
        ]
        for row, entry in enumerate(visible):
            absolute_index = self.archive_scroll + row
            y = 205 + row * 45
            rectangle = self.pg.Rect(115, y, 725, 38)
            focused = (
                rectangle.collidepoint(self.mouse_ui)
                or self.focus_action == f"archive:{absolute_index}"
            )
            self._pixel_box(
                rectangle,
                PINK if focused else (240, 200, 184),
                SUN if focused else PIXEL_SHADOW,
                cut=3,
                width=2,
            )
            self.pg.draw.rect(
                self.ui_surface,
                CORAL_DARK,
                self.pg.Rect(125, y + 6, 52, 26),
            )
            self._text("NES", 13, WHITE, (151, y + 19), center=True, bold=True)
            self._text(
                self._ellipsize(entry.member_name, 18, 515),
                18,
                INK,
                (190, y + 8),
                bold=True,
            )
            self._text(
                self._size_label(entry.size_bytes),
                14,
                MUTED,
                (785, y + 19),
                center=True,
            )
            action = f"archive:{absolute_index}"
            self.buttons.append((rectangle, action, True))
            self.focusables.append(action)
            if self.focus_action is None:
                self.focus_action = action
        if len(self.archive_items) > 9:
            count = len(self.archive_items)
            track = self.pg.Rect(850, 205, 8, 398)
            self.pg.draw.rect(
                self.ui_surface,
                PIXEL_GREY,
                track,
            )
            thumb_height = max(35, round(track.height * 9 / count))
            travel = track.height - thumb_height
            maximum = max(1, count - 9)
            thumb_y = track.y + round(
                travel * self.archive_scroll / maximum
            )
            self.pg.draw.rect(
                self.ui_surface,
                CORAL,
                self.pg.Rect(track.x, thumb_y, track.width, thumb_height),
            )
        self._button((395, 620, 170, 42), "BACK", "back", primary=True)

    def _wrapped_text(
        self,
        text: str,
        size: int,
        color: tuple[int, int, int],
        center_x: int,
        top: int,
        width: int,
    ) -> None:
        font = self._font(size)
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and font.size(candidate)[0] > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        for index, line in enumerate(lines):
            self._text(
                line,
                size,
                color,
                (center_x, top + index * (size + 7)),
                center=True,
            )

    def _draw_confirmation(self) -> None:
        if self.confirmation is None:
            return
        title, body, _action = self.confirmation
        overlay = self.pg.Surface((UI_WIDTH, UI_HEIGHT), self.pg.SRCALPHA)
        overlay.fill((18, 16, 34, 185))
        self.ui_surface.blit(overlay, (0, 0))
        self.buttons.clear()
        self.focusables.clear()
        if self.focus_action not in ("confirm_cancel", "confirm_yes"):
            self.focus_action = "confirm_cancel"
        panel = self.pg.Rect(235, 205, 490, 300)
        self._panel(panel, CREAM, border=CORAL)
        self._text(title.upper(), 32, INK, (480, 255), center=True, bold=True)
        self._wrapped_text(body, 18, MUTED, 480, 315, 390)
        self._button((285, 430, 185, 48), "CANCEL", "confirm_cancel")
        self._button(
            (490, 430, 185, 48),
            "YES / CONTINUE",
            "confirm_yes",
            primary=True,
        )

    def _draw_binding_capture(self) -> None:
        if self.capture_binding is None:
            return
        overlay = self.pg.Surface((UI_WIDTH, UI_HEIGHT), self.pg.SRCALPHA)
        overlay.fill((18, 16, 34, 195))
        self.ui_surface.blit(overlay, (0, 0))
        self.buttons.clear()
        self.focusables.clear()
        panel = self.pg.Rect(245, 235, 470, 235)
        self._panel(panel, CREAM, border=MINT)
        gamepad_capture = self.capture_device == "gamepad"
        self._text(
            "PRESS / MOVE GAMEPAD" if gamepad_capture else "PRESS NEW KEY",
            31 if gamepad_capture else 34,
            INK,
            (480, 285),
            center=True,
            bold=True,
        )
        self._text(
            (
                "BUTTONS, D-PAD, HATS, AND STICKS ARE SUPPORTED"
                if gamepad_capture
                else "DUPLICATE BINDINGS WILL BE SWAPPED"
            ),
            16 if gamepad_capture else 18,
            MUTED,
            (480, 345),
            center=True,
        )
        self._text(
            "ESC TO CANCEL",
            16,
            CORAL_DARK,
            (480, 410),
            center=True,
            bold=True,
        )

    def _draw_menu(self) -> None:
        self.buttons.clear()
        self.focusables.clear()
        self.volume_rect = None
        if self.view == "home":
            self._draw_home()
        elif self.view == "pause":
            self._draw_pause()
        elif self.view == "states":
            self._draw_states()
        elif self.view == "settings":
            self._draw_settings()
        elif self.view == "controls":
            self._draw_controls()
        elif self.view == "browser":
            self._draw_browser()
        elif self.view == "archive":
            self._draw_archive()
        self._draw_confirmation()
        self._draw_binding_capture()

    def _draw_game_to_screen(self) -> None:
        if self.console is None:
            self.screen.fill((8, 10, 14))
            return
        source = self.frame_surface
        target = self.game_screen_rect
        if target is None:
            target = self._fit_rect(
                PPU_WIDTH,
                PPU_HEIGHT,
                *self.screen.get_size(),
            )
            self.game_screen_rect = target
        if getattr(self, "presentation_backend", "surface") == "sdl-texture":
            if self.game_frame_dirty:
                self.game_texture.update(source)
                self.game_frame_dirty = False
            return
        if self.game_scaled_surface is None:
            self.game_scaled_surface = self.pg.Surface(
                target[2:],
                depth=self.game_native_surface,
            )
            self.game_frame_dirty = True
        if target[2:] != self.screen.get_size():
            self.screen.fill((8, 10, 14))
        if self.game_frame_dirty:
            self.game_native_surface.blit(source, (0, 0))
            self.pg.transform.scale(
                self.game_native_surface,
                target[2:],
                self.game_scaled_surface,
            )
            self.game_frame_dirty = False
        self.screen.blit(self.game_scaled_surface, target[:2])

    def _draw_toast(self, *, on_ui: bool) -> None:
        if not self.message or time.monotonic() >= self.message_until:
            return
        if on_ui:
            text = self._ellipsize(self.message, 18, 650)
            rendered = self._font(18, True).render(text, False, WHITE)
            rectangle = rendered.get_rect(center=(480, 688))
            background = rectangle.inflate(32, 16)
            self._pixel_box(background, NAVY, SUN, cut=4, width=3)
            self.ui_surface.blit(rendered, rectangle)
            return
        screen_width, screen_height = self.screen.get_size()
        font = self._font(max(18, screen_height // 30), True)
        rendered = font.render(self.message, False, WHITE)
        max_width = max(100, screen_width - 40)
        if rendered.get_width() > max_width:
            scale = max_width / rendered.get_width()
            rendered = self.pg.transform.smoothscale(
                rendered,
                (max_width, max(1, round(rendered.get_height() * scale))),
            )
        if getattr(self, "presentation_backend", "surface") == "sdl-texture":
            key = (
                self.message,
                screen_width,
                screen_height,
                rendered.get_size(),
            )
            if self.toast_texture_key != key:
                from pygame._sdl2.video import Texture

                background_size = (
                    rendered.get_width() + 28,
                    rendered.get_height() + 16,
                )
                toast_surface = self.pg.Surface(
                    background_size,
                    self.pg.SRCALPHA,
                )
                toast_surface.fill((18, 16, 34, 232))
                self.pg.draw.rect(
                    toast_surface,
                    (239, 184, 77, 255),
                    toast_surface.get_rect(),
                    width=2,
                )
                toast_surface.blit(rendered, (14, 8))
                self.toast_texture = Texture.from_surface(
                    self.renderer,
                    toast_surface,
                )
                self.toast_texture_rect = (
                    (screen_width - background_size[0]) // 2,
                    screen_height - background_size[1] - 8,
                    *background_size,
                )
                self.toast_texture_key = key
            return
        rectangle = rendered.get_rect()
        rectangle.centerx = screen_width // 2
        rectangle.bottom = screen_height - 16
        background = rectangle.inflate(28, 16)
        overlay = self.pg.Surface(background.size, self.pg.SRCALPHA)
        overlay.fill((18, 16, 34, 215))
        self.screen.blit(overlay, background)
        self.screen.blit(rendered, rectangle)

    def _present_display(self, *, gameplay: bool) -> None:
        """Present either the native game texture or the composed UI surface."""
        if getattr(self, "presentation_backend", "surface") != "sdl-texture":
            self.pg.display.flip()
            return
        renderer = self.renderer
        renderer.draw_color = (8, 10, 14, 255)
        renderer.clear()
        if gameplay and self.console is not None:
            self.game_texture.draw(dstrect=self.game_screen_rect)
            if (
                self.message
                and time.monotonic() < self.message_until
                and self.toast_texture is not None
            ):
                self.toast_texture.draw(dstrect=self.toast_texture_rect)
        else:
            self.screen_texture.update(self.screen)
            self.screen_texture.draw()
        renderer.present()

    def _draw(self) -> None:
        self._draw_game_to_screen()
        if self.view == "game":
            self._draw_toast(on_ui=False)
            self._present_display(gameplay=True)
            return
        self.ui_surface.fill((0, 0, 0, 0))
        self._draw_menu()
        self._draw_toast(on_ui=True)
        self.ui_screen_rect = self._fit_rect(
            UI_WIDTH,
            UI_HEIGHT,
            *self.screen.get_size(),
        )
        scaled = self._scale_ui_surface(self.ui_screen_rect[2:])
        self.screen.blit(scaled, self.ui_screen_rect[:2])
        self._present_display(gameplay=False)

    def _scale_ui_surface(self, size: tuple[int, int]):
        """Scale menus without destroying glyph strokes at fractional sizes."""
        source_size = self.ui_surface.get_size()
        if tuple(size) == tuple(source_size):
            return self.ui_surface
        width_scale = size[0] / source_size[0]
        height_scale = size[1] / source_size[1]
        integer_scale = (
            width_scale >= 1.0
            and height_scale >= 1.0
            and width_scale.is_integer()
            and height_scale.is_integer()
        )
        if integer_scale:
            return self.pg.transform.scale(self.ui_surface, size)
        # Nearest-neighbor downscaling discarded complete rows and columns
        # from the aliased font glyphs. Filter only fractional menu scaling;
        # exact and integer-upscaled pixel art stays untouched.
        return self.pg.transform.smoothscale(self.ui_surface, size)

    def _shutdown(self) -> None:
        """Release every SDL-backed object on the main thread, in order."""
        try:
            self._stop_emulation_worker()
        except Exception:
            pass
        try:
            self._reset_audio_stream(drain_core=True)
        except Exception:
            # Cleanup must not replace the original exception from the run
            # loop. Remaining subsystems are still released below.
            pass

        try:
            if self.pg.mixer.get_init():
                self.pg.mixer.stop()
                self.pg.mixer.quit()
        except Exception:
            pass
        self.audio_available = False
        self.audio_channel = None
        self.audio_sound_refs.clear()

        for gamepad in self.joysticks:
            try:
                gamepad.handle.quit()
            except Exception:
                pass
        self.joysticks.clear()
        try:
            if self.pg.joystick.get_init():
                self.pg.joystick.quit()
        except Exception:
            pass

        # Drop native surfaces while SDL video is still initialized. The RGB
        # bytearray that backs frame_surface remains alive through this step.
        self.thumbnails.clear()
        self.fonts.clear()
        self.brand_wordmark = None
        self.brand_mascot = None
        self.brand_icon = None
        self.game_scaled_surface = None
        self.game_native_surface = None
        self.frame_surface = None
        self.ui_surface = None
        self.screen = None
        self._release_texture_display()
        try:
            if self.pg.font.get_init():
                self.pg.font.quit()
        except Exception:
            pass
        try:
            if self.pg.display.get_init():
                self.pg.display.quit()
        except Exception:
            pass
        try:
            self.pg.quit()
        except Exception:
            pass
        finally:
            if self._gc_suspended:
                self._gc_suspended = False
                if self._gc_was_enabled:
                    gc.enable()

    def _wait_until_frame_deadline(self, deadline: float) -> float:
        """Sleep coarsely, then use a tiny bounded tail for stable pacing."""
        perf_counter = time.perf_counter
        sleep = time.sleep
        while True:
            now = perf_counter()
            remaining = deadline - now
            if remaining <= 0:
                return now
            self._pump_audio()
            if remaining > PACING_SPIN_SECONDS:
                sleep(
                    min(
                        AUDIO_PUMP_INTERVAL,
                        remaining - PACING_SPIN_SECONDS,
                    )
                )
                continue
            while True:
                now = perf_counter()
                remaining = deadline - now
                if remaining <= 0:
                    return now
                if remaining > PACING_MICRO_SLEEP_SECONDS:
                    sleep(PACING_MICRO_SLEEP_SECONDS)

    def run(self) -> int:
        frame_period = 1.0 / NTSC_FRAME_RATE
        clean_exit = False
        try:
            self._initialize()
            self.running = True
            if self.console is not None and self.view == "game":
                self._start_emulation_worker()
            next_frame = time.perf_counter() + frame_period
            while self.running:
                loop_started = (
                    time.perf_counter() if self.diagnostics else 0.0
                )
                emulated_this_loop = False
                for event in self.pg.event.get():
                    self._handle_event(event)
                if self._pacing_segment_reset:
                    self.frame_pacer.reset_segment()
                    worker = self.emulation_worker
                    warmup_periods = (
                        2
                        if (
                            self.console is not None
                            and self.view == "game"
                            and worker is not None
                            and worker.ready_frames == 0
                        )
                        else 1
                    )
                    next_frame = (
                        time.perf_counter()
                        + frame_period * warmup_periods
                    )
                    self._pacing_segment_reset = False
                self._poll_inputs()
                gameplay_presentation = (
                    self.console is not None and self.view == "game"
                )
                pacing_wait_seconds = 0.0
                if gameplay_presentation:
                    if self.emulation_worker is None:
                        self._start_emulation_worker()
                    draw_deadline = self.frame_pacer.draw_deadline(
                        next_frame,
                        gameplay=True,
                    )
                    worker_wait_started = time.perf_counter()
                    packet = self._wait_for_emulated_frame(draw_deadline)
                    pacing_wait_seconds = (
                        time.perf_counter() - worker_wait_started
                    )
                    if packet is not None:
                        emulated_this_loop = True
                        self._accept_emulated_frame(packet, frame_period)
                    else:
                        # Preserve cadence and repeat the last complete frame
                        # rather than exposing a partial PPU buffer or changing
                        # emulation speed.
                        self.emulation_queue_starves += 1
                    deadline_wait_started = time.perf_counter()
                    self._wait_until_frame_deadline(draw_deadline)
                    pacing_wait_seconds += (
                        time.perf_counter() - deadline_wait_started
                    )

                if gameplay_presentation:
                    # Prepare the software-scaled frame before the deadline,
                    # then wait again and make only the final display flip
                    # deadline-sensitive. Scale-time variance is therefore
                    # hidden instead of translated into visible motion jitter.
                    toast_visible = bool(
                        self.message
                        and time.monotonic() < self.message_until
                    )
                    needs_present = bool(
                        self.game_display_dirty
                        or toast_visible != self._toast_was_visible
                    )
                    if needs_present:
                        draw_started = time.perf_counter()
                        self._draw_game_to_screen()
                        self._draw_toast(on_ui=False)
                        prepared_at = time.perf_counter()
                        final_wait_started = prepared_at
                        self._wait_until_frame_deadline(next_frame)
                        pacing_wait_seconds += (
                            time.perf_counter() - final_wait_started
                        )
                        flip_started = time.perf_counter()
                        self._present_display(gameplay=True)
                        presented_at = time.perf_counter()
                        prepare_seconds = prepared_at - draw_started
                        flip_seconds = presented_at - flip_started
                        draw_seconds = prepare_seconds + flip_seconds
                        self.game_display_dirty = False
                        self._toast_was_visible = toast_visible
                        self.frame_pacer.record_present(
                            draw_seconds,
                            completed_at=presented_at,
                        )
                        if self.diagnostics:
                            self.draw_seconds += draw_seconds
                            self.slowest_draw_seconds = max(
                                self.slowest_draw_seconds,
                                draw_seconds,
                            )
                            self.draw_prepare_seconds += prepare_seconds
                            self.slowest_draw_prepare_seconds = max(
                                self.slowest_draw_prepare_seconds,
                                prepare_seconds,
                            )
                            self.flip_seconds += flip_seconds
                            self.slowest_flip_seconds = max(
                                self.slowest_flip_seconds,
                                flip_seconds,
                            )
                    else:
                        final_wait_started = time.perf_counter()
                        presented_at = self._wait_until_frame_deadline(
                            next_frame
                        )
                        pacing_wait_seconds += (
                            presented_at - final_wait_started
                        )
                        draw_seconds = 0.0
                        self.unchanged_display_skips += 1
                else:
                    draw_started = time.perf_counter()
                    self._draw()
                    presented_at = time.perf_counter()
                    draw_seconds = presented_at - draw_started
                    self.frame_pacer.skip_streak = 0

                if self.diagnostics and emulated_this_loop:
                    active_seconds = max(
                        0.0,
                        presented_at - loop_started - pacing_wait_seconds,
                    )
                    self.active_loop_seconds += active_seconds
                    self.slowest_active_loop_seconds = max(
                        self.slowest_active_loop_seconds,
                        active_seconds,
                    )

                if gameplay_presentation:
                    now = presented_at
                else:
                    # Menus have no emulated motion. Keep their inexpensive
                    # redraw loop capped without adding a pre-draw input delay.
                    now = self._wait_until_frame_deadline(next_frame)
                lateness = max(0.0, presented_at - next_frame)
                if self.diagnostics and emulated_this_loop:
                    self.worst_pacing_lateness = max(
                        self.worst_pacing_lateness,
                        lateness,
                    )
                    if lateness >= PACING_LATE_THRESHOLD:
                        self.pacing_late_frames += 1
                if gameplay_presentation:
                    next_frame = self.frame_pacer.schedule_next(
                        presented_at,
                        next_frame,
                        frame_period,
                    )
                else:
                    next_frame = now + frame_period
            clean_exit = True
        finally:
            try:
                self._save_current_battery()
                self._save_settings()
            finally:
                self._shutdown()
                if self.diagnostics:
                    self._print_final_diagnostics(
                        clean_shutdown=clean_exit,
                    )
        return 0
