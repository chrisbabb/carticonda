from __future__ import annotations

import contextlib
import io
import unittest
from array import array
from types import SimpleNamespace
from unittest.mock import patch

from nes_from_scratch.audio_stream import PcmBuffer
from nes_from_scratch.constants import Button, NTSC_FRAME_RATE
from nes_from_scratch.frontend import (
    AUDIO_CHUNK_SAMPLES,
    AUDIO_PREBUFFER_CHUNKS,
    PygameFrontend,
    _GamepadState,
    _PresentationPacer,
)
from nes_from_scratch.ui_model import FrontendSettings


class PcmBufferTests(unittest.TestCase):
    def test_samples_wait_until_a_complete_chunk_is_available(self) -> None:
        buffer = PcmBuffer(chunk_samples=4, max_chunks=3)
        buffer.append(array("h", (1, 2, 3)))
        self.assertIsNone(buffer.pop_chunk())
        buffer.append(array("h", (4, 5)))
        self.assertEqual(
            array("h", buffer.pop_chunk()).tolist(),
            [1, 2, 3, 4],
        )
        self.assertEqual(buffer.pending_samples, 1)

    def test_full_host_queue_does_not_discard_pending_samples(self) -> None:
        buffer = PcmBuffer(chunk_samples=4, max_chunks=4)
        buffer.append(array("h", range(7)))
        self.assertEqual(buffer.pending_samples, 7)
        self.assertEqual(
            array("h", buffer.pop_chunk()).tolist(),
            [0, 1, 2, 3],
        )
        self.assertEqual(buffer.pending_samples, 3)

    def test_runaway_latency_keeps_newest_audio_and_reports_resync(self) -> None:
        buffer = PcmBuffer(chunk_samples=2, max_chunks=3)
        self.assertTrue(buffer.append(array("h", range(7))))
        self.assertEqual(buffer.resyncs, 1)
        self.assertEqual(buffer.pending_samples, 4)
        self.assertEqual(
            array("h", buffer.pop_chunk()).tolist(),
            [3, 4],
        )

    def test_clear_removes_partial_audio(self) -> None:
        buffer = PcmBuffer(chunk_samples=4, max_chunks=3)
        buffer.append(array("h", (1, 2)))
        buffer.clear()
        self.assertEqual(buffer.pending_samples, 0)

    def test_borrowed_chunk_is_discarded_only_after_release(self) -> None:
        buffer = PcmBuffer(chunk_samples=4, max_chunks=3)
        buffer.append(array("h", range(6)))
        view = buffer.peek_chunk()
        self.assertIsNotNone(view)
        self.assertEqual(array("h", view.tobytes()).tolist(), [0, 1, 2, 3])
        with self.assertRaises(BufferError):
            buffer.discard_chunk()
        view.release()
        buffer.discard_chunk()
        self.assertEqual(buffer.pending_samples, 2)


class _FakeApu:
    def __init__(self, samples: array) -> None:
        self.samples = samples
        self.sample_rate = 44_100

    def drain_samples(self) -> array:
        samples = self.samples
        self.samples = array("h")
        return samples


class _FakeChannel:
    def __init__(self) -> None:
        self.busy = True
        self.queued = object()
        self.played: list[bytes] = []
        self.queued_pcm: list[bytes] = []
        self.queue_failures_remaining = 0
        self.volume = 1.0
        self.stop_calls = 0

    def get_busy(self) -> bool:
        return self.busy

    def get_queue(self):
        return self.queued

    def play(self, sound) -> None:
        raise AssertionError("the unsafe explicit Channel.play path was used")

    def queue(self, sound) -> None:
        if self.queue_failures_remaining:
            self.queue_failures_remaining -= 1
            raise RuntimeError("simulated native queue failure")
        pcm = bytes(sound)
        if self.busy:
            self.queued = sound
            self.queued_pcm.append(pcm)
        else:
            # pygame's documented Channel.queue behavior starts immediately
            # when there is no current Sound.
            self.busy = True
            self.queued = None
            self.played.append(pcm)

    def stop(self) -> None:
        self.stop_calls += 1
        self.busy = False
        self.queued = None

    def set_volume(self, volume: float) -> None:
        self.volume = float(volume)

    def finish_current(self) -> None:
        """Advance the fake device without ever making a queued stream idle."""
        if self.queued is None:
            self.busy = False
        else:
            self.queued = None
            self.busy = True


class _FakeSound:
    def __init__(self, channel: _FakeChannel, pcm) -> None:
        self.channel = channel
        self.pcm = bytes(pcm)
        self.volume = 1.0

    def __bytes__(self) -> bytes:
        return self.pcm

    def set_volume(self, volume: float) -> None:
        self.volume = float(volume)


class _NeverBusyChannel(_FakeChannel):
    """Simulate a driver whose get_busy() never reports a promoted Sound.

    Some WASAPI configurations never observe a queued Sound transition to
    "busy" through pygame-ce's Channel.get_busy(), unlike the ordinary fake
    channel above which always promotes a queued Sound once queue() is
    called from an idle state.
    """

    def queue(self, sound) -> None:
        if self.queue_failures_remaining:
            self.queue_failures_remaining -= 1
            raise RuntimeError("simulated native queue failure")
        self.queued = sound
        self.queued_pcm.append(bytes(sound))

    def stop(self) -> None:
        self.stop_calls += 1
        self.busy = False
        self.queued = None


class FrontendAudioQueueTests(unittest.TestCase):
    def test_native_queue_covers_long_spikes_without_more_prebuffer(self) -> None:
        self.assertEqual(AUDIO_CHUNK_SAMPLES, 2048)
        self.assertEqual(AUDIO_PREBUFFER_CHUNKS, 2)
        self.assertEqual(
            AUDIO_CHUNK_SAMPLES * AUDIO_PREBUFFER_CHUNKS,
            4096,
        )

    def frontend(
        self,
        samples: array,
        *,
        prebuffer_samples: int = 4,
    ) -> PygameFrontend:
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.console = SimpleNamespace(apu=_FakeApu(samples))
        frontend.audio_available = True
        channel = _FakeChannel()
        frontend.audio_channel = channel
        frontend.audio_buffer = PcmBuffer(chunk_samples=4, max_chunks=4)
        frontend.audio_started = False
        frontend.audio_underruns = 0
        frontend.audio_concealments = 0
        frontend.audio_recovery_fades = 0
        frontend.audio_handoff_waits = 0
        frontend.audio_grace_restarts = 0
        frontend.audio_start_failures = 0
        frontend.audio_recovering = False
        frontend.audio_idle_since = None
        frontend.audio_prebuffer_samples = prebuffer_samples
        frontend.sample_rate = 44_100
        frontend.settings = SimpleNamespace(muted=False, volume=1.0)
        frontend.pg = SimpleNamespace(
            error=RuntimeError,
            mixer=SimpleNamespace(
                Sound=lambda *, buffer: _FakeSound(channel, buffer)
            )
        )
        return frontend

    def test_full_mixer_queue_retains_samples_for_a_later_host_frame(self) -> None:
        frontend = self.frontend(array("h", range(6)))
        frontend._queue_audio()
        self.assertEqual(frontend.audio_buffer.pending_samples, 6)
        self.assertEqual(frontend.audio_channel.queued_pcm, [])

        frontend.audio_channel.queued = None
        frontend._queue_audio()
        self.assertEqual(frontend.audio_buffer.pending_samples, 2)
        self.assertEqual(
            array("h", frontend.audio_channel.queued_pcm[0]).tolist(),
            [0, 1, 2, 3],
        )

    def test_pump_continuously_refills_the_second_mixer_slot(self) -> None:
        frontend = self.frontend(array("h", range(16)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None

        frontend._queue_audio()
        self.assertEqual(len(frontend.audio_channel.played), 1)
        self.assertEqual(len(frontend.audio_channel.queued_pcm), 1)
        self.assertEqual(frontend.audio_buffer.pending_samples, 8)

        frontend.audio_channel.finish_current()
        frontend._pump_audio()
        frontend.audio_channel.finish_current()
        frontend._pump_audio()

        self.assertEqual(len(frontend.audio_channel.played), 1)
        self.assertEqual(len(frontend.audio_channel.queued_pcm), 3)
        self.assertEqual(frontend.audio_buffer.pending_samples, 0)
        self.assertEqual(frontend.audio_underruns, 0)
        submitted = (
            frontend.audio_channel.played
            + frontend.audio_channel.queued_pcm
        )
        self.assertEqual(
            [
                sample
                for pcm in submitted
                for sample in array("h", pcm)
            ],
            list(range(16)),
        )
        self.assertGreaterEqual(len(frontend.audio_sound_refs), 2)

    def test_stream_reset_releases_retained_native_sounds(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend._queue_audio()
        self.assertTrue(frontend.audio_sound_refs)
        frontend._reset_audio_stream()
        self.assertFalse(frontend.audio_sound_refs)
        self.assertFalse(frontend.audio_channel.get_busy())

    def test_playback_waits_for_prebuffer_after_an_underrun(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend._queue_audio()
        self.assertTrue(frontend.audio_started)

        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend.audio_buffer.clear()
        frontend.audio_buffer.append(array("h", (20, 21)))
        with patch(
            "nes_from_scratch.frontend.time.perf_counter",
            side_effect=(10.0, 10.020),
        ):
            frontend._pump_audio()
            # A single idle observation is not enough to call a transient
            # SDL_mixer handoff an underrun.
            self.assertTrue(frontend.audio_started)
            frontend._pump_audio()
        self.assertFalse(frontend.audio_started)
        self.assertEqual(frontend.audio_underruns, 1)
        self.assertEqual(frontend.audio_concealments, 0)
        self.assertEqual(len(frontend.audio_channel.played), 1)

        frontend.audio_buffer.append(array("h", range(22, 28)))
        frontend._pump_audio()
        self.assertTrue(frontend.audio_started)
        self.assertEqual(len(frontend.audio_channel.played), 2)
        self.assertFalse(frontend.audio_recovering)
        self.assertEqual(frontend.audio_recovery_fades, 1)
        self.assertEqual(
            array("h", frontend.audio_channel.played[-1]).tolist(),
            [0, 21, 22, 23],
        )

    def test_native_queue_handoff_never_restarts_or_stops_valid_pcm(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = object()

        frontend._queue_audio()

        self.assertTrue(frontend.audio_started)
        self.assertEqual(frontend.audio_underruns, 0)
        self.assertEqual(frontend.audio_handoff_waits, 1)
        self.assertEqual(frontend.audio_channel.played, [])
        self.assertEqual(frontend.audio_channel.queued_pcm, [])
        self.assertEqual(frontend.audio_buffer.pending_samples, 8)

        # Once SDL reports the promoted sound as busy, only the empty queue
        # slot is refilled. The playing sound is never stopped or replaced.
        frontend.audio_channel.busy = True
        frontend.audio_channel.queued = None
        frontend._pump_audio()
        self.assertEqual(len(frontend.audio_channel.queued_pcm), 1)
        self.assertEqual(frontend.audio_buffer.pending_samples, 4)
        self.assertEqual(frontend.audio_underruns, 0)

    def test_stuck_handoff_recovers_instead_of_stalling_forever(self) -> None:
        """A get_busy() that never reports a promotion must not stay silent.

        Some WASAPI configurations never observe a queued Sound becoming
        "busy". Treating that as an always-transient handoff (the previous
        behavior) waited forever and never queued another chunk, so nothing
        after the very first chunk was ever audible. The pump must give the
        handoff a bounded grace period and then force a clean restart.
        """
        frontend = self.frontend(array("h", range(16)), prebuffer_samples=8)
        frontend.audio_channel = _NeverBusyChannel()
        channel = frontend.audio_channel
        channel.busy = False
        channel.queued = None

        frontend._queue_audio()
        self.assertEqual(len(channel.queued_pcm), 1)
        self.assertEqual(channel.played, [])
        self.assertEqual(frontend.audio_handoff_waits, 0)

        with patch(
            "nes_from_scratch.frontend.time.perf_counter",
            side_effect=(10.000, 10.004, 10.009, 10.013),
        ):
            frontend._pump_audio()
            self.assertEqual(frontend.audio_handoff_waits, 1)
            self.assertEqual(channel.stop_calls, 0)
            frontend._pump_audio()
            self.assertEqual(frontend.audio_handoff_waits, 2)
            frontend._pump_audio()
            self.assertEqual(frontend.audio_handoff_waits, 3)
            # 10.013 - 10.000 = 13ms, past the grace period: stop waiting for
            # a promotion that this simulated driver will never report.
            frontend._pump_audio()
        self.assertEqual(channel.stop_calls, 1)
        self.assertFalse(frontend.audio_started)
        self.assertTrue(frontend.audio_recovering)
        self.assertEqual(frontend.audio_underruns, 1)

        # The stuck Sound is gone and the channel reports idle again, so the
        # next pump must resume playback instead of staying silent forever.
        frontend._pump_audio()
        self.assertTrue(frontend.audio_started)
        self.assertEqual(len(channel.queued_pcm), 2)

    def test_pcm_arriving_during_idle_grace_resumes_without_underrun(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend._queue_audio()
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend.audio_buffer.clear()
        frontend.audio_buffer.append(array("h", (20, 21)))

        with patch(
            "nes_from_scratch.frontend.time.perf_counter",
            return_value=10.0,
        ):
            frontend._pump_audio()
        frontend.audio_buffer.append(array("h", (22, 23)))
        frontend._pump_audio()

        self.assertTrue(frontend.audio_started)
        self.assertEqual(frontend.audio_underruns, 0)
        self.assertEqual(frontend.audio_grace_restarts, 1)
        self.assertEqual(len(frontend.audio_channel.played), 2)
        self.assertEqual(
            array("h", frontend.audio_channel.played[-1]).tolist(),
            [20, 21, 22, 23],
        )

    def test_long_stream_survives_native_handoffs_without_pcm_loss(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        channel = frontend.audio_channel
        channel.busy = False
        channel.queued = None
        frontend._queue_audio()

        for block in range(2, 202):
            if block % 7 == 0:
                # Model the short pygame/SDL observation window that caused
                # 2.2.8 to stop a valid promoted Sound hundreds of times.
                channel.busy = False
                frontend._pump_audio()
                self.assertGreaterEqual(frontend.audio_handoff_waits, 1)
            channel.finish_current()
            first = block * 4
            frontend.audio_buffer.append(
                array("h", range(first, first + 4))
            )
            frontend._pump_audio()

        submitted = channel.played + channel.queued_pcm
        self.assertEqual(
            [sample for pcm in submitted for sample in array("h", pcm)],
            list(range(808)),
        )
        self.assertEqual(frontend.audio_underruns, 0)
        self.assertEqual(channel.stop_calls, 0)

    def test_failed_sound_start_is_safe_and_keeps_pcm_for_retry(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend.audio_channel.queue_failures_remaining = 1

        frontend._queue_audio()

        self.assertTrue(frontend.audio_started)
        self.assertEqual(frontend.audio_start_failures, 1)
        self.assertEqual(frontend.audio_buffer.pending_samples, 8)
        self.assertEqual(frontend.audio_channel.played, [])

        frontend._pump_audio()
        self.assertTrue(frontend.audio_started)
        self.assertEqual(frontend.audio_buffer.pending_samples, 0)
        self.assertEqual(len(frontend.audio_channel.played), 1)

    def test_transient_wasapi_start_race_retries_without_losing_pcm(
        self,
    ) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend.audio_channel.queue_failures_remaining = 1

        frontend._queue_audio()

        self.assertTrue(frontend.audio_started)
        self.assertEqual(frontend.audio_start_failures, 1)
        self.assertEqual(frontend.audio_buffer.pending_samples, 8)

        frontend._pump_audio()
        self.assertTrue(frontend.audio_started)
        self.assertEqual(frontend.audio_buffer.pending_samples, 0)
        self.assertEqual(len(frontend.audio_channel.played), 1)
        self.assertEqual(len(frontend.audio_channel.queued_pcm), 1)

    def test_muting_skips_host_pcm_generation_until_unmuted(self) -> None:
        frontend = self.frontend(array("h", (1, 2, 3)))
        frontend.sample_rate = 48_000
        frontend.settings.muted = True
        frontend._sync_core_sample_generation()
        self.assertEqual(frontend.console.apu.sample_rate, 0)
        self.assertEqual(frontend.console.apu.samples, array("h"))

        frontend.settings.muted = False
        frontend._sync_core_sample_generation()
        self.assertEqual(frontend.console.apu.sample_rate, 48_000)

    def test_volume_is_attached_to_each_chunk_across_queue_handoffs(self) -> None:
        frontend = self.frontend(array("h", range(8)), prebuffer_samples=8)
        frontend.settings.volume = 0.375
        frontend.audio_channel.busy = False
        frontend.audio_channel.queued = None
        frontend._queue_audio()

        sounds = tuple(frontend.audio_sound_refs)
        self.assertEqual(len(sounds), 2)
        self.assertTrue(all(sound.volume == 0.375 for sound in sounds))

        frontend.settings.volume = 0.625
        frontend._apply_volume()
        self.assertEqual(frontend.audio_channel.volume, 1.0)
        self.assertTrue(all(sound.volume == 0.625 for sound in sounds))


class GamepadEventStateTests(unittest.TestCase):
    def test_hotplug_events_open_and_close_only_the_named_instance(self) -> None:
        class Handle:
            def __init__(self, instance_id: int) -> None:
                self.instance_id = instance_id
                self.quit_calls = 0

            def get_instance_id(self) -> int:
                return self.instance_id

            def quit(self) -> None:
                self.quit_calls += 1

        first = Handle(17)
        duplicate = Handle(17)
        handles = iter((first, duplicate))
        startup_add = SimpleNamespace(type=100, device_index=3)
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.gamepads_requested = True
        frontend.gamepad_events = 0
        frontend.joysticks = []
        frontend.pg = SimpleNamespace(
            error=RuntimeError,
            JOYDEVICEADDED=100,
            JOYDEVICEREMOVED=101,
            event=SimpleNamespace(
                pump=lambda: None,
                get=lambda event_types: (startup_add,),
            ),
            joystick=SimpleNamespace(
                Joystick=lambda device_index: next(handles),
            ),
        )

        frontend._prime_gamepad_events()
        self.assertEqual([state.instance_id for state in frontend.joysticks], [17])
        self.assertEqual(frontend.gamepad_events, 1)

        frontend._handle_gamepad_device_event(startup_add)
        self.assertEqual(len(frontend.joysticks), 1)
        self.assertEqual(duplicate.quit_calls, 1)

        frontend._handle_gamepad_device_event(
            SimpleNamespace(type=101, instance_id=17)
        )
        self.assertEqual(frontend.joysticks, [])
        self.assertEqual(first.quit_calls, 1)

    def test_events_rebuild_nes_buttons_without_native_polling(self) -> None:
        state = _GamepadState(SimpleNamespace(), 7)
        state.set_button(0, True)
        state.set_button(7, True)
        state.set_axis(0, -0.8)
        state.set_axis(1, 0.9)
        self.assertEqual(
            state.nes_buttons,
            int(Button.A | Button.START | Button.LEFT | Button.DOWN),
        )

        state.set_hat(0, (1, 1))
        state.set_axis(0, 0.0)
        state.set_axis(1, 0.0)
        self.assertEqual(
            state.nes_buttons,
            int(Button.A | Button.START | Button.RIGHT | Button.UP),
        )

    def test_poll_inputs_reads_only_cached_python_state(self) -> None:
        class NativeHandle:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected native poll: {name}")

        player1 = SimpleNamespace(buttons=None)
        player2 = SimpleNamespace(buttons=None)
        player1.set_buttons = lambda value: setattr(player1, "buttons", value)
        player2.set_buttons = lambda value: setattr(player2, "buttons", value)
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.console = SimpleNamespace(
            controller1=player1,
            controller2=player2,
        )
        frontend.view = "game"
        frontend.keyboard_buttons = [0, 0]
        gamepad = _GamepadState(NativeHandle(), 11)
        gamepad.set_button(1, True)
        frontend.joysticks = [gamepad]

        frontend._poll_inputs()

        self.assertEqual(player1.buttons, int(Button.B))
        self.assertEqual(player2.buttons, 0)

    def test_compiled_custom_sources_replace_the_default_layout(self) -> None:
        player1 = SimpleNamespace(buttons=None)
        player2 = SimpleNamespace(buttons=None)
        player1.set_buttons = lambda value: setattr(player1, "buttons", value)
        player2.set_buttons = lambda value: setattr(player2, "buttons", value)
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.console = SimpleNamespace(
            controller1=player1,
            controller2=player2,
        )
        frontend.view = "game"
        frontend.keyboard_buttons = [0, 0]
        frontend.settings = FrontendSettings()
        frontend.settings.gamepad_bindings["p1_a"] = ["button:5"]
        frontend.gamepad_source_masks = [{}, {}]
        frontend._rebuild_gamepad_bindings()
        gamepad = _GamepadState(SimpleNamespace(), 11)
        frontend.joysticks = [gamepad]

        gamepad.set_button(0, True)
        gamepad.set_button(5, True)
        gamepad.set_axis(2, -0.9)
        frontend._poll_inputs()

        self.assertEqual(player1.buttons, int(Button.A))
        self.assertEqual(player2.buttons, 0)
        self.assertIn("axis:2:-", gamepad.active_sources)

    def test_capture_swaps_conflicting_buttons_and_persists_mapping(self) -> None:
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.settings = FrontendSettings()
        gamepad = _GamepadState(SimpleNamespace(), 11)
        frontend.joysticks = [gamepad]
        frontend.capture_binding = "p1_b"
        frontend.capture_device = "gamepad"
        frontend.gamepad_source_masks = [{}, {}]
        frontend._save_settings = lambda: None
        messages = []
        frontend._show_message = lambda message, seconds=2.5: messages.append(message)

        captured = frontend._capture_gamepad_source(gamepad, "button:0")

        self.assertTrue(captured)
        self.assertEqual(
            frontend.settings.gamepad_bindings["p1_b"],
            ["button:0"],
        )
        self.assertEqual(
            frontend.settings.gamepad_bindings["p1_a"],
            ["button:1"],
        )
        self.assertIsNone(frontend.capture_binding)
        self.assertIsNone(frontend.capture_device)
        self.assertTrue(messages)


class PresentationPacerTests(unittest.TestCase):
    def test_draw_deadline_targets_flip_without_skipping(self) -> None:
        pacer = _PresentationPacer()
        deadline = 10.0
        self.assertAlmostEqual(
            pacer.draw_deadline(deadline, gameplay=True),
            deadline - 0.0032,
        )
        pacer.record_present(0.004, completed_at=deadline)

        self.assertEqual(pacer.presentation_skips, 0)
        self.assertEqual(pacer.presented_frames, 1)
        self.assertEqual(pacer.max_skip_streak, 0)
        self.assertGreater(pacer.draw_estimate, 0.003)

    def test_late_frame_never_changes_the_exact_long_term_clock(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        deadline = 5.0
        completed = deadline + 0.003
        next_deadline = pacer.schedule_next(
            completed,
            deadline,
            frame_period,
        )

        self.assertAlmostEqual(
            next_deadline,
            deadline + frame_period,
        )
        self.assertEqual(pacer.timing_debt, 0.0)
        self.assertEqual(pacer.presentation_skips, 0)

    def test_exact_clock_has_no_accumulating_debt_over_an_hour(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        deadline = 1000.0
        start = deadline
        frames = round(NTSC_FRAME_RATE * 3600)
        for _ in range(frames):
            deadline = pacer.schedule_next(
                deadline,
                deadline,
                frame_period,
            )

        expected = start + frames * frame_period
        self.assertAlmostEqual(deadline, expected, places=6)
        self.assertEqual(pacer.timing_debt, 0.0)

    def test_exceptional_pause_discards_old_timing_debt(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        next_deadline = pacer.schedule_next(
            2.0,
            1.0,
            frame_period,
        )
        self.assertAlmostEqual(next_deadline, 2.0 + frame_period)
        self.assertEqual(pacer.timing_debt, 0.0)

    def test_presentation_jitter_is_measured_from_flip_intervals(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        pacer.record_present(0.003, completed_at=1.0)
        pacer.record_present(
            0.003,
            completed_at=1.0 + frame_period + 0.0006,
        )

        self.assertEqual(pacer.presentation_intervals, 1)
        self.assertAlmostEqual(pacer.worst_presentation_jitter, 0.0006)
        self.assertEqual(pacer.jitter_percentile(0.95), 0.75)

    def test_variable_workload_never_drifts_or_drops_a_frame(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        clock = 0.0
        deadline = frame_period
        presented = []
        # A tail frame exceeds the display deadline once scaling is included;
        # surrounding frames retain ample average headroom.
        for emulation_seconds in (0.006, 0.0155, 0.005, 0.007, 0.006):
            clock += emulation_seconds
            clock = max(
                clock,
                pacer.draw_deadline(deadline, gameplay=True),
            )
            clock += 0.0031
            presented.append(clock)
            pacer.record_present(0.0031, completed_at=clock)
            deadline = pacer.schedule_next(
                clock,
                deadline,
                frame_period,
            )

        intervals = [
            current - previous
            for previous, current in zip(presented, presented[1:])
        ]
        self.assertEqual(pacer.presented_frames, 5)
        self.assertEqual(pacer.presentation_skips, 0)
        self.assertGreater(max(intervals), frame_period)
        self.assertAlmostEqual(deadline, frame_period * 6)
        self.assertEqual(pacer.timing_debt, 0.0)

    def test_pause_reset_excludes_the_gap_from_jitter(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        pacer.record_present(0.003, completed_at=1.0)
        pacer.reset_segment()
        pacer.record_present(0.003, completed_at=20.0)
        pacer.record_present(0.003, completed_at=20.0 + frame_period)

        self.assertEqual(pacer.presentation_intervals, 1)
        self.assertLess(pacer.worst_presentation_jitter, 1e-9)

    def test_system_suspend_gap_is_not_reported_as_gameplay_jitter(self) -> None:
        pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        pacer.record_present(0.003, completed_at=1.0)
        pacer.record_present(0.003, completed_at=18.0)
        pacer.record_present(0.003, completed_at=18.0 + frame_period)

        self.assertEqual(pacer.presentation_intervals, 1)
        self.assertLess(pacer.worst_presentation_jitter, 1e-9)

    def test_menu_draw_deadline_does_not_add_gameplay_lead(self) -> None:
        pacer = _PresentationPacer()
        self.assertEqual(
            pacer.draw_deadline(4.0, gameplay=False),
            4.0,
        )


class FrontendFrameBufferTests(unittest.TestCase):
    def test_windowed_sizes_preserve_ui_aspect_and_exact_game_scale(self) -> None:
        self.assertEqual(PygameFrontend._windowed_size(2), (640, 480))
        self.assertEqual(PygameFrontend._windowed_size(3), (960, 720))
        self.assertEqual(PygameFrontend._windowed_size(4), (1280, 960))
        for scale in (2, 3, 4):
            target = PygameFrontend._fit_rect(
                256,
                240,
                *PygameFrontend._windowed_size(scale),
            )
            self.assertEqual(target[2:], (256 * scale, 240 * scale))

    def test_fractional_menu_scaling_filters_text_strokes(self) -> None:
        calls = []
        source = SimpleNamespace(get_size=lambda: (960, 720))
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.ui_surface = source
        frontend.pg = SimpleNamespace(
            transform=SimpleNamespace(
                scale=lambda surface, size: calls.append(
                    ("nearest", surface, size)
                ),
                smoothscale=lambda surface, size: calls.append(
                    ("filtered", surface, size)
                ),
            )
        )

        frontend._scale_ui_surface((640, 480))
        self.assertEqual(calls, [("filtered", source, (640, 480))])

        calls.clear()
        self.assertIs(frontend._scale_ui_surface((960, 720)), source)
        self.assertEqual(calls, [])

        frontend._scale_ui_surface((1920, 1440))
        self.assertEqual(calls, [("nearest", source, (1920, 1440))])

    def test_surface_is_rebound_only_when_frame_storage_changes(self) -> None:
        calls = []

        def frombuffer(buffer, size, pixel_format):
            surface = SimpleNamespace(
                buffer=buffer,
                size=size,
                pixel_format=pixel_format,
            )
            calls.append(surface)
            return surface

        frontend = PygameFrontend.__new__(PygameFrontend)
        first = bytearray(256 * 240 * 3)
        frontend.frame = first
        frontend.frame_surface = None
        frontend.pg = SimpleNamespace(
            display=SimpleNamespace(get_init=lambda: True),
            image=SimpleNamespace(frombuffer=frombuffer),
        )

        frontend._set_frame_source(first)
        frontend._set_frame_source(first)
        self.assertEqual(len(calls), 1)
        self.assertIs(frontend.frame_surface.buffer, first)

        second = bytearray(256 * 240 * 3)
        frontend._set_frame_source(second)
        self.assertEqual(len(calls), 2)
        self.assertIs(frontend.frame_surface.buffer, second)

    def test_unchanged_game_frame_reuses_scaled_surface(self) -> None:
        scales = []
        blits = []
        native_blits = []
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.console = object()
        frontend.frame_surface = object()
        frontend.game_screen_rect = (0, 0, 512, 480)
        frontend.game_native_surface = SimpleNamespace(
            blit=lambda source, position: native_blits.append(
                (source, position)
            )
        )
        frontend.game_scaled_surface = object()
        frontend.game_frame_dirty = True
        frontend.screen = SimpleNamespace(
            get_size=lambda: (512, 480),
            fill=lambda _color: None,
            blit=lambda surface, position: blits.append(
                (surface, position)
            ),
        )
        frontend.pg = SimpleNamespace(
            transform=SimpleNamespace(
                scale=lambda source, size, destination: scales.append(
                    (source, size, destination)
                )
            )
        )

        frontend._draw_game_to_screen()
        frontend._draw_game_to_screen()

        self.assertEqual(len(scales), 1)
        self.assertEqual(len(native_blits), 1)
        self.assertIs(scales[0][0], frontend.game_native_surface)
        self.assertEqual(len(blits), 2)
        self.assertFalse(frontend.game_frame_dirty)

    def test_texture_presenter_uploads_only_the_native_dirty_frame(
        self,
    ) -> None:
        updates = []
        draws = []
        calls = []
        texture = SimpleNamespace(
            update=lambda surface: updates.append(surface),
            draw=lambda **options: draws.append(options),
        )
        renderer = SimpleNamespace(
            draw_color=None,
            clear=lambda: calls.append("clear"),
            present=lambda: calls.append("present"),
        )
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.console = object()
        frontend.presentation_backend = "sdl-texture"
        frontend.frame_surface = object()
        frontend.game_frame_dirty = True
        frontend.game_screen_rect = (32, 0, 512, 480)
        frontend.game_texture = texture
        frontend.renderer = renderer
        frontend.message = ""
        frontend.message_until = 0.0
        frontend.screen = SimpleNamespace(get_size=lambda: (576, 480))
        frontend.pg = SimpleNamespace(
            display=SimpleNamespace(
                flip=lambda: self.fail("software flip was used")
            )
        )

        frontend._draw_game_to_screen()
        frontend._draw_game_to_screen()
        frontend._present_display(gameplay=True)

        self.assertEqual(updates, [frontend.frame_surface])
        self.assertEqual(draws, [{"dstrect": frontend.game_screen_rect}])
        self.assertEqual(calls, ["clear", "present"])
        self.assertFalse(frontend.game_frame_dirty)


class FrontendInitializationTests(unittest.TestCase):
    def test_muted_keyboard_mode_opens_no_mixer_or_joystick(self) -> None:
        calls = []
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.pg = SimpleNamespace(
            error=RuntimeError,
            display=SimpleNamespace(init=lambda: calls.append("display")),
            font=SimpleNamespace(init=lambda: calls.append("font")),
            mixer=SimpleNamespace(
                init=lambda: calls.append("mixer"),
                get_init=lambda: None,
            ),
            joystick=SimpleNamespace(
                init=lambda: calls.append("joystick"),
            ),
        )
        frontend.audio_requested = False
        frontend.gamepads_requested = False
        frontend.diagnostics = False
        frontend.console = None
        frontend.startup_rom_path = None
        frontend._set_display = lambda: None
        frontend._bind_frame_surface = lambda: None
        frontend._load_brand_assets = lambda: None
        frontend._set_caption = lambda: None
        frontend._rebuild_key_bindings = lambda: None
        frontend._sync_core_sample_generation = lambda: calls.append(
            "core-audio-off"
        )
        frontend._sync_gc_mode = lambda: None

        frontend._initialize()
        self.assertEqual(
            calls,
            ["display", "font", "core-audio-off"],
        )


class FrontendDiagnosticsTests(unittest.TestCase):
    def test_histogram_reports_tail_latency_and_extended_phase_fields(self) -> None:
        frontend = PygameFrontend.__new__(PygameFrontend)
        frontend.emulated_frames = 0
        frontend.emulation_seconds = 0.0
        frontend.emulation_cpu_seconds = 0.0
        frontend.slowest_frame_seconds = 0.0
        frontend.slowest_cpu_frame_seconds = 0.0
        frontend.frame_budget_misses = 2
        frontend.emulation_histogram = [0] * 256
        frontend.draw_seconds = 0.006
        frontend.slowest_draw_seconds = 0.004
        frontend.active_loop_seconds = 0.040
        frontend.slowest_active_loop_seconds = 0.025
        frontend.pacing_late_frames = 1
        frontend.worst_pacing_lateness = 0.003
        frontend.frame_pacer = _PresentationPacer()
        frame_period = 1.0 / NTSC_FRAME_RATE
        frontend.frame_pacer.record_present(0.002, completed_at=1.0)
        frontend.frame_pacer.record_present(
            0.002,
            completed_at=1.0 + frame_period + 0.0005,
        )
        frontend.audio_underruns = 0
        frontend.audio_handoff_waits = 0
        frontend.audio_grace_restarts = 0
        frontend.audio_start_failures = 0
        frontend.audio_buffer = PcmBuffer()
        frontend.gamepad_events = 0

        for seconds in (0.010, 0.011, 0.020):
            frontend._record_emulation_time(seconds)
        self.assertGreaterEqual(frontend._diagnostic_percentile(0.95), 20.0)
        self.assertLess(frontend._diagnostic_percentile(0.95), 20.5)

        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            frontend._print_final_diagnostics(clean_shutdown=True)
        line = output.getvalue()
        self.assertIn("emulation-p95-ms=", line)
        self.assertIn("emulation-p99-ms=", line)
        self.assertIn("emulation-cpu-average-ms=", line)
        self.assertIn("emulation-cpu-max-ms=", line)
        self.assertIn("draw-average-ms=", line)
        self.assertIn("active-average-ms=", line)
        self.assertIn("late-frames=1", line)
        self.assertIn("presentation-skips=0", line)
        self.assertIn("presentation-jitter-p95-ms=", line)
        self.assertIn("presentation-jitter-max-ms=", line)
        self.assertIn("pacing-debt-ms=", line)
        self.assertIn("rewind-deferrals=", line)
        self.assertIn("rewind-compression-deferrals=", line)
        self.assertIn("rewind-captures=", line)
        self.assertIn("unchanged-video-frames=", line)
        self.assertIn("ppu-scanline-renders=", line)
        self.assertIn("ppu-scanline-replays=", line)
        self.assertIn("ppu-cache-invalidations=", line)
        self.assertIn("ppu-cache-coalesces=", line)
        self.assertIn("ppu-world-cache-misses=", line)
        self.assertIn("ppu-world-cache-evictions=", line)
        self.assertIn("ppu-world-cache-admission-deferrals=", line)
        self.assertIn("ppu-world-cache-size=", line)
        self.assertIn("ppu-deferred-data-writes=", line)
        self.assertIn("cpu-ppu-stream-batches=", line)
        self.assertIn("cpu-dmc-poll-batches=", line)
        self.assertIn("cpu-dmc-poll-fetches=", line)
        self.assertIn("cpu-translated-blocks=", line)
        self.assertIn("cpu-block-deferrals=", line)
        self.assertIn("cpu-block-compile-peak=", line)
        self.assertIn("cpu-bank-compiles=", line)
        self.assertIn("cpu-bank-deferrals=", line)
        self.assertIn("audio-concealments=", line)
        self.assertIn("audio-recovery-fades=", line)
        self.assertIn("audio-handoff-waits=", line)
        self.assertIn("audio-grace-restarts=", line)
        self.assertIn("ppu-status-probes=", line)
        self.assertIn("ppu-mapper-invalidations=", line)
        self.assertIn("clean-shutdown=yes", line)


if __name__ == "__main__":
    unittest.main()
