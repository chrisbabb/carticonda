"""Ricoh 2A03 audio processing unit and five-channel nonlinear mixer."""

from __future__ import annotations

from array import array
from collections.abc import Callable
from math import pi

from .constants import CPU_HZ, DEFAULT_SAMPLE_RATE


LENGTH_TABLE = (
    10, 254, 20, 2, 40, 4, 80, 6,
    160, 8, 60, 10, 14, 12, 26, 14,
    12, 16, 24, 18, 48, 20, 96, 22,
    192, 24, 72, 26, 16, 28, 32, 30,
)
NOISE_PERIODS = (
    4, 8, 16, 32, 64, 96, 128, 160,
    202, 254, 380, 508, 762, 1016, 2034, 4068,
)
DMC_PERIODS = (
    428, 380, 340, 320, 286, 254, 226, 214,
    190, 160, 142, 128, 106, 84, 72, 54,
)
# NTSC frame-sequencer events expressed in CPU cycles. The sequencer itself
# advances on alternating CPU phases; the older half-sized values were APU
# cycles and caused envelopes, length counters, sweeps, and frame IRQs to run
# at twice their hardware rate.
FRAME_Q1 = 7457
FRAME_Q2 = 14913
FRAME_Q3 = 22371
FRAME_4_IRQ_START = 29828
FRAME_4_CLOCK = 29829
FRAME_4_END = 29830
FRAME_5_CLOCK = 37281
FRAME_5_END = 37282
DUTY_TABLE = (
    (0, 1, 0, 0, 0, 0, 0, 0),
    (0, 1, 1, 0, 0, 0, 0, 0),
    (0, 1, 1, 1, 1, 0, 0, 0),
    (1, 0, 0, 1, 1, 1, 1, 1),
)
TRIANGLE_TABLE = tuple(range(15, -1, -1)) + tuple(range(16))
PULSE_MIX_TABLE = tuple(
    0.0 if level == 0 else 95.88 / (8128.0 / level + 100.0)
    for level in range(31)
)
TND_MIX_TABLE = tuple(
    (
        0.0
        if (triangle == 0 and noise == 0 and dmc == 0)
        else 159.79
        / (
            1.0
            / (
                triangle / 8227.0
                + noise / 12241.0
                + dmc / 22638.0
            )
            + 100.0
        )
    )
    for triangle in range(16)
    for noise in range(16)
    for dmc in range(128)
)


class Envelope:
    __slots__ = (
        "start",
        "loop",
        "constant",
        "period",
        "divider",
        "decay",
    )

    def __init__(self) -> None:
        self.start = False
        self.loop = False
        self.constant = False
        self.period = 0
        self.divider = 0
        self.decay = 0

    @property
    def output(self) -> int:
        return self.period if self.constant else self.decay

    def configure(self, value: int) -> None:
        self.loop = bool(value & 0x20)
        self.constant = bool(value & 0x10)
        self.period = value & 0x0F

    def clock(self) -> None:
        if self.start:
            self.start = False
            self.decay = 15
            self.divider = self.period
        elif self.divider:
            self.divider -= 1
        else:
            self.divider = self.period
            if self.decay:
                self.decay -= 1
            elif self.loop:
                self.decay = 15

    def get_state(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def set_state(self, state: dict) -> None:
        self.start = bool(state["start"])
        self.loop = bool(state["loop"])
        self.constant = bool(state["constant"])
        self.period = int(state["period"])
        self.divider = int(state["divider"])
        self.decay = int(state["decay"])


class Pulse:
    __slots__ = (
        "channel",
        "enabled",
        "length",
        "length_halt",
        "duty",
        "sequence",
        "timer_period",
        "timer_counter",
        "envelope",
        "sweep_enabled",
        "sweep_period",
        "sweep_negate",
        "sweep_shift",
        "sweep_divider",
        "sweep_reload",
    )

    def __init__(self, channel: int) -> None:
        self.channel = channel
        self.enabled = False
        self.length = 0
        self.length_halt = False
        self.duty = 0
        self.sequence = 0
        self.timer_period = 0
        self.timer_counter = 0
        self.envelope = Envelope()
        self.sweep_enabled = False
        self.sweep_period = 1
        self.sweep_negate = False
        self.sweep_shift = 0
        self.sweep_divider = 0
        self.sweep_reload = False

    def write_control(self, value: int) -> None:
        self.duty = (value >> 6) & 3
        self.length_halt = bool(value & 0x20)
        self.envelope.configure(value)

    def write_sweep(self, value: int) -> None:
        self.sweep_enabled = bool(value & 0x80)
        self.sweep_period = ((value >> 4) & 7) + 1
        self.sweep_negate = bool(value & 0x08)
        self.sweep_shift = value & 7
        self.sweep_reload = True

    def target_period(self) -> int:
        delta = self.timer_period >> self.sweep_shift
        if self.sweep_negate:
            return self.timer_period - delta - (1 if self.channel == 1 else 0)
        return self.timer_period + delta

    def clock_timer(self) -> None:
        if self.timer_counter == 0:
            self.timer_counter = self.timer_period
            self.sequence = (self.sequence + 1) & 7
        else:
            self.timer_counter -= 1

    def clock_length(self) -> None:
        if self.length and not self.length_halt:
            self.length -= 1

    def clock_sweep(self) -> None:
        target = self.target_period()
        if (
            self.sweep_divider == 0
            and self.sweep_enabled
            and self.sweep_shift
            and self.timer_period >= 8
            and target <= 0x7FF
        ):
            self.timer_period = target
        if self.sweep_divider == 0 or self.sweep_reload:
            self.sweep_divider = self.sweep_period
            self.sweep_reload = False
        else:
            self.sweep_divider -= 1

    @property
    def output(self) -> int:
        target = self.target_period()
        if (
            not self.enabled
            or self.length == 0
            or self.timer_period < 8
            or target > 0x7FF
            or DUTY_TABLE[self.duty][self.sequence] == 0
        ):
            return 0
        return self.envelope.output

    def get_state(self) -> dict:
        return {
            name: getattr(self, name)
            for name in self.__slots__
            if name != "envelope"
        } | {"envelope": self.envelope.get_state()}

    def set_state(self, state: dict) -> None:
        for key, value in state.items():
            if key != "envelope":
                setattr(self, key, value)
        self.envelope.set_state(state["envelope"])


class Triangle:
    __slots__ = (
        "enabled",
        "length",
        "control",
        "linear_reload_value",
        "linear_counter",
        "linear_reload",
        "timer_period",
        "timer_counter",
        "sequence",
    )

    def __init__(self) -> None:
        self.enabled = False
        self.length = 0
        self.control = False
        self.linear_reload_value = 0
        self.linear_counter = 0
        self.linear_reload = False
        self.timer_period = 0
        self.timer_counter = 0
        self.sequence = 0

    def clock_timer(self) -> None:
        if self.timer_counter == 0:
            self.timer_counter = self.timer_period
            if self.enabled and self.length and self.linear_counter and self.timer_period > 1:
                self.sequence = (self.sequence + 1) & 31
        else:
            self.timer_counter -= 1

    def clock_length(self) -> None:
        if self.length and not self.control:
            self.length -= 1

    def clock_linear(self) -> None:
        if self.linear_reload:
            self.linear_counter = self.linear_reload_value
        elif self.linear_counter:
            self.linear_counter -= 1
        if not self.control:
            self.linear_reload = False

    @property
    def output(self) -> int:
        if not self.enabled or not self.length or not self.linear_counter:
            return 0
        return TRIANGLE_TABLE[self.sequence]

    def get_state(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def set_state(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)


class Noise:
    __slots__ = (
        "enabled",
        "length",
        "length_halt",
        "mode",
        "period_index",
        "timer_counter",
        "shift_register",
        "envelope",
    )

    def __init__(self) -> None:
        self.enabled = False
        self.length = 0
        self.length_halt = False
        self.mode = False
        self.period_index = 0
        self.timer_counter = 0
        self.shift_register = 1
        self.envelope = Envelope()

    def clock_timer(self) -> None:
        if self.timer_counter == 0:
            self.timer_counter = NOISE_PERIODS[self.period_index]
            tap = 6 if self.mode else 1
            feedback = (self.shift_register & 1) ^ ((self.shift_register >> tap) & 1)
            self.shift_register = (self.shift_register >> 1) | (feedback << 14)
        else:
            self.timer_counter -= 1

    def clock_length(self) -> None:
        if self.length and not self.length_halt:
            self.length -= 1

    @property
    def output(self) -> int:
        if not self.enabled or not self.length or self.shift_register & 1:
            return 0
        return self.envelope.output

    def get_state(self) -> dict:
        return {
            name: getattr(self, name)
            for name in self.__slots__
            if name != "envelope"
        } | {"envelope": self.envelope.get_state()}

    def set_state(self, state: dict) -> None:
        for key, value in state.items():
            if key != "envelope":
                setattr(self, key, value)
        self.envelope.set_state(state["envelope"])


class DMC:
    __slots__ = (
        "enabled",
        "irq_enabled",
        "irq_pending",
        "loop",
        "rate_index",
        "timer_counter",
        "output_level",
        "sample_address",
        "sample_length",
        "current_address",
        "bytes_remaining",
        "sample_buffer",
        "shift_register",
        "bits_remaining",
        "silence",
        "last_fetch_cycle",
        "last_fetch_stall",
        "fetch_count",
    )

    def __init__(self) -> None:
        self.enabled = False
        self.irq_enabled = False
        self.irq_pending = False
        self.loop = False
        self.rate_index = 0
        self.timer_counter = DMC_PERIODS[0] - 1
        self.output_level = 0
        self.sample_address = 0xC000
        self.sample_length = 1
        self.current_address = 0xC000
        self.bytes_remaining = 0
        self.sample_buffer: int | None = None
        self.shift_register = 0
        self.bits_remaining = 8
        self.silence = True
        # CPU-cycle timestamp of the most recent sample-buffer DMA request.
        # The console's DMA arbiter uses this to distinguish a DMC request
        # that has already completed from one whose halt/dummy cycles overlap
        # the start of an OAM transfer.
        self.last_fetch_cycle: int | None = None
        self.last_fetch_stall = 0
        self.fetch_count = 0

    def restart(self) -> None:
        self.current_address = self.sample_address
        self.bytes_remaining = self.sample_length

    def fill_buffer(
        self,
        reader: Callable[[int], int] | None,
        cycle: int | None = None,
        *,
        cpu_write: bool = False,
    ) -> int:
        if (
            self.sample_buffer is not None
            or self.bytes_remaining == 0
            or reader is None
        ):
            return 0
        self.last_fetch_cycle = None if cycle is None else int(cycle)
        # RDY can halt the 6502 only on a read. If the request lands on a
        # write, that write completes while the DMA unit is waiting and only
        # three additional CPU cycles are stolen.
        stall_cycles = 3 if cpu_write else 4
        self.last_fetch_stall = stall_cycles
        self.fetch_count += 1
        self.sample_buffer = reader(self.current_address) & 0xFF
        self.current_address = (
            0x8000
            if self.current_address == 0xFFFF
            else self.current_address + 1
        )
        self.bytes_remaining -= 1
        if self.bytes_remaining == 0:
            if self.loop:
                self.restart()
            elif self.irq_enabled:
                self.irq_pending = True
        return stall_cycles

    def clock_timer(self) -> None:
        if self.timer_counter == 0:
            # DMC_PERIODS contains complete CPU-cycle periods. A down-counter
            # that includes zero therefore reloads with period - 1.
            self.timer_counter = DMC_PERIODS[self.rate_index] - 1
            if not self.silence:
                if self.shift_register & 1:
                    if self.output_level <= 125:
                        self.output_level += 2
                elif self.output_level >= 2:
                    self.output_level -= 2
            self.shift_register >>= 1
            self.bits_remaining -= 1
            if self.bits_remaining == 0:
                self.bits_remaining = 8
                if self.sample_buffer is None:
                    self.silence = True
                else:
                    self.silence = False
                    self.shift_register = self.sample_buffer
                    self.sample_buffer = None
        else:
            self.timer_counter -= 1

    def get_state(self) -> dict:
        return {name: getattr(self, name) for name in self.__slots__}

    def set_state(self, state: dict) -> None:
        for key, value in state.items():
            setattr(self, key, value)


class APU:
    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        self.pulse1 = Pulse(1)
        self.pulse2 = Pulse(2)
        self.triangle = Triangle()
        self.noise = Noise()
        self.dmc = DMC()
        self.sample_rate = int(sample_rate)
        self.sample_phase = 0
        self.samples = array("h")
        self.cycle = 0
        self.frame_cycle = 0
        self.five_step = False
        self.frame_irq_inhibit = False
        self.frame_irq_pending = False
        self.frame_irq_assert_cycle: int | None = None
        self.frame_counter_value = 0
        self._pending_frame_counter_value = 0
        self._frame_counter_reset_delay = 0
        self.dmc_reader: Callable[[int], int] | None = None
        self.cpu_stall_cycles = 0
        self.cpu_write_cycle: int | None = None
        self.cpu_write_address: int | None = None
        self.hp_previous_input = 0.0
        self.hp_previous_output = 0.0
        self.hp440_previous_input = 0.0
        self.hp440_previous_output = 0.0
        self.lp_previous_input = 0.0
        self.lp_previous_output = 0.0
        self._filter_sample_rate = -1
        self._high_pass_90_b0 = 1.0
        self._high_pass_90_decay = 1.0
        self._high_pass_440_b0 = 1.0
        self._high_pass_440_decay = 1.0
        self._low_pass_b0 = 1.0
        self._low_pass_b1 = 0.0
        self._low_pass_decay = 0.0
        self._low_phase_cycles_to_sample: bytes | tuple[int, ...] = ()
        self._configure_output_filter()

    def _configure_output_filter(self) -> None:
        """Configure the NES's three first-order analog output stages."""
        sample_rate = int(self.sample_rate)
        self._filter_sample_rate = sample_rate
        if sample_rate <= 0:
            self._low_phase_cycles_to_sample = ()
            self._high_pass_90_b0 = 1.0
            self._high_pass_90_decay = 1.0
            self._high_pass_440_b0 = 1.0
            self._high_pass_440_decay = 1.0
            self._low_pass_b0 = 1.0
            self._low_pass_b1 = 0.0
            self._low_pass_decay = 0.0
            return

        # Immediately after a host sample, phase is below sample_rate. The
        # default 44.1 kHz path revisits only those 44,100 values, so cache its
        # next 40/41-cycle interval instead of performing roughly 735 ceiling
        # divisions per NTSC frame. An unusual very low rate can require more
        # than one byte per interval and keeps the same tuple lookup.
        intervals = tuple(
            (CPU_HZ - phase + sample_rate - 1) // sample_rate
            for phase in range(sample_rate)
        )
        self._low_phase_cycles_to_sample = (
            bytes(intervals) if max(intervals) <= 0xFF else intervals
        )

        # Bilinear-transform coefficients preserve the documented 90 Hz and
        # 440 Hz high-pass corners and the 14 kHz low-pass corner at either
        # supported host rate. Keeping the scalar recurrence inline in _mix()
        # avoids three Python filter-object dispatches for every PCM sample.
        high_90_c = sample_rate / (pi * 90.0)
        high_90_scale = 1.0 / (1.0 + high_90_c)
        self._high_pass_90_b0 = high_90_c * high_90_scale
        self._high_pass_90_decay = (
            (high_90_c - 1.0) * high_90_scale
        )

        high_440_c = sample_rate / (pi * 440.0)
        high_440_scale = 1.0 / (1.0 + high_440_c)
        self._high_pass_440_b0 = high_440_c * high_440_scale
        self._high_pass_440_decay = (
            (high_440_c - 1.0) * high_440_scale
        )

        low_c = sample_rate / (pi * 14_000.0)
        low_scale = 1.0 / (1.0 + low_c)
        self._low_pass_b0 = low_scale
        self._low_pass_b1 = low_scale
        self._low_pass_decay = (
            (low_c - 1.0) * low_scale
        )

    @property
    def irq_pending(self) -> bool:
        return self.frame_irq_pending or self.dmc.irq_pending

    def reset(self) -> None:
        """Apply a cold power-on reset."""
        sample_rate = self.sample_rate
        reader = self.dmc_reader
        self.__init__(sample_rate)
        self.dmc_reader = reader

    def soft_reset(self) -> None:
        """Apply the reset-pin behavior without erasing APU registers."""
        frame_counter_value = self.frame_counter_value
        # Reset acts like a $00 write to $4015 and repeats the last value
        # written to $4017. Channel control/timer registers themselves retain
        # their values, which software can observe immediately in the reset
        # handler.
        self._write_status(0)
        self.frame_irq_pending = False
        self.frame_irq_assert_cycle = None
        self.dmc.irq_pending = False
        self.cpu_stall_cycles = 0
        self._write_frame_counter(frame_counter_value)
        self.frame_irq_pending = False

    def write_register(self, address: int, value: int) -> None:
        value &= 0xFF
        if address == 0x4000:
            self.pulse1.write_control(value)
        elif address == 0x4001:
            self.pulse1.write_sweep(value)
        elif address == 0x4002:
            self.pulse1.timer_period = (self.pulse1.timer_period & 0x700) | value
        elif address == 0x4003:
            self.pulse1.timer_period = (self.pulse1.timer_period & 0xFF) | ((value & 7) << 8)
            if self.pulse1.enabled:
                self.pulse1.length = LENGTH_TABLE[value >> 3]
            self.pulse1.sequence = 0
            self.pulse1.envelope.start = True
        elif address == 0x4004:
            self.pulse2.write_control(value)
        elif address == 0x4005:
            self.pulse2.write_sweep(value)
        elif address == 0x4006:
            self.pulse2.timer_period = (self.pulse2.timer_period & 0x700) | value
        elif address == 0x4007:
            self.pulse2.timer_period = (self.pulse2.timer_period & 0xFF) | ((value & 7) << 8)
            if self.pulse2.enabled:
                self.pulse2.length = LENGTH_TABLE[value >> 3]
            self.pulse2.sequence = 0
            self.pulse2.envelope.start = True
        elif address == 0x4008:
            self.triangle.control = bool(value & 0x80)
            self.triangle.linear_reload_value = value & 0x7F
        elif address == 0x400A:
            self.triangle.timer_period = (self.triangle.timer_period & 0x700) | value
        elif address == 0x400B:
            self.triangle.timer_period = (self.triangle.timer_period & 0xFF) | ((value & 7) << 8)
            if self.triangle.enabled:
                self.triangle.length = LENGTH_TABLE[value >> 3]
            self.triangle.linear_reload = True
        elif address == 0x400C:
            self.noise.length_halt = bool(value & 0x20)
            self.noise.envelope.configure(value)
        elif address == 0x400E:
            self.noise.mode = bool(value & 0x80)
            self.noise.period_index = value & 0x0F
        elif address == 0x400F:
            if self.noise.enabled:
                self.noise.length = LENGTH_TABLE[value >> 3]
            self.noise.envelope.start = True
        elif address == 0x4010:
            self.dmc.irq_enabled = bool(value & 0x80)
            self.dmc.loop = bool(value & 0x40)
            self.dmc.rate_index = value & 0x0F
            if not self.dmc.irq_enabled:
                self.dmc.irq_pending = False
        elif address == 0x4011:
            self.dmc.output_level = value & 0x7F
        elif address == 0x4012:
            self.dmc.sample_address = 0xC000 + value * 64
        elif address == 0x4013:
            self.dmc.sample_length = value * 16 + 1
        elif address == 0x4015:
            self._write_status(value)
        elif address == 0x4017:
            self._write_frame_counter(value)

    def _write_status(self, value: int) -> None:
        channels = (self.pulse1, self.pulse2, self.triangle, self.noise)
        for bit, channel in enumerate(channels):
            channel.enabled = bool(value & (1 << bit))
            if not channel.enabled:
                channel.length = 0
        self.dmc.enabled = bool(value & 0x10)
        if not self.dmc.enabled:
            self.dmc.bytes_remaining = 0
        elif self.dmc.bytes_remaining == 0:
            self.dmc.restart()
        self.dmc.irq_pending = False

    def _write_frame_counter(self, value: int) -> None:
        self.frame_counter_value = value & 0xC0
        self.frame_irq_inhibit = bool(self.frame_counter_value & 0x40)
        if self.frame_irq_inhibit:
            self.frame_irq_pending = False
            self.frame_irq_assert_cycle = None
        self._pending_frame_counter_value = self.frame_counter_value
        # The divider reset propagates after 3 CPU cycles on an APU phase and
        # 4 cycles on the intervening phase. Register writes arrive at the
        # beginning of their final CPU bus cycle in the Console timestamp
        # model, making even APU cycle numbers the three-cycle case.
        self._frame_counter_reset_delay = 3 if not (self.cycle & 1) else 4

    def _apply_frame_counter_reset(self) -> None:
        value = self._pending_frame_counter_value
        self.five_step = bool(value & 0x80)
        self.frame_cycle = 0
        if self.five_step:
            self._quarter_frame()
            self._half_frame()

    def read_status(self) -> int:
        value = 0
        value |= int(self.pulse1.length > 0)
        value |= int(self.pulse2.length > 0) << 1
        value |= int(self.triangle.length > 0) << 2
        value |= int(self.noise.length > 0) << 3
        value |= int(self.dmc.bytes_remaining > 0) << 4
        value |= int(self.frame_irq_pending) << 6
        value |= int(self.dmc.irq_pending) << 7
        self.frame_irq_pending = False
        self.frame_irq_assert_cycle = None
        return value

    def _quarter_frame(self) -> None:
        self.pulse1.envelope.clock()
        self.pulse2.envelope.clock()
        self.noise.envelope.clock()
        self.triangle.clock_linear()

    def _half_frame(self) -> None:
        self.pulse1.clock_length()
        self.pulse2.clock_length()
        self.triangle.clock_length()
        self.noise.clock_length()
        self.pulse1.clock_sweep()
        self.pulse2.clock_sweep()

    def _clock_frame_counter(self) -> None:
        self.frame_cycle += 1
        if self.five_step:
            if self.frame_cycle in (FRAME_Q1, FRAME_Q3, FRAME_5_CLOCK):
                self._quarter_frame()
            if self.frame_cycle in (FRAME_Q2, FRAME_5_CLOCK):
                if self.frame_cycle == FRAME_Q2:
                    self._quarter_frame()
                self._half_frame()
            if self.frame_cycle >= FRAME_5_END:
                self.frame_cycle = 0
        else:
            if self.frame_cycle in (FRAME_Q1, FRAME_Q3):
                self._quarter_frame()
            elif self.frame_cycle in (FRAME_Q2, FRAME_4_CLOCK):
                self._quarter_frame()
                self._half_frame()
            if (
                FRAME_4_IRQ_START <= self.frame_cycle <= FRAME_4_END
                and not self.frame_irq_inhibit
            ):
                if not self.frame_irq_pending:
                    self.frame_irq_pending = True
                    self.frame_irq_assert_cycle = self.cycle
            if self.frame_cycle >= FRAME_4_END:
                self.frame_cycle = 0

    def _mix(self) -> int:
        pulse1 = self.pulse1
        period = pulse1.timer_period
        delta = period >> pulse1.sweep_shift
        target = (
            period - delta - (1 if pulse1.channel == 1 else 0)
            if pulse1.sweep_negate
            else period + delta
        )
        if (
            pulse1.enabled
            and pulse1.length
            and period >= 8
            and target <= 0x7FF
            and DUTY_TABLE[pulse1.duty][pulse1.sequence]
        ):
            pulse1_level = (
                pulse1.envelope.period
                if pulse1.envelope.constant
                else pulse1.envelope.decay
            )
        else:
            pulse1_level = 0

        pulse2 = self.pulse2
        period = pulse2.timer_period
        delta = period >> pulse2.sweep_shift
        target = (
            period - delta - (1 if pulse2.channel == 1 else 0)
            if pulse2.sweep_negate
            else period + delta
        )
        if (
            pulse2.enabled
            and pulse2.length
            and period >= 8
            and target <= 0x7FF
            and DUTY_TABLE[pulse2.duty][pulse2.sequence]
        ):
            pulse2_level = (
                pulse2.envelope.period
                if pulse2.envelope.constant
                else pulse2.envelope.decay
            )
        else:
            pulse2_level = 0

        triangle_channel = self.triangle
        triangle = (
            TRIANGLE_TABLE[triangle_channel.sequence]
            if (
                triangle_channel.enabled
                and triangle_channel.length
                and triangle_channel.linear_counter
            )
            else 0
        )
        noise_channel = self.noise
        noise = (
            (
                noise_channel.envelope.period
                if noise_channel.envelope.constant
                else noise_channel.envelope.decay
            )
            if (
                noise_channel.enabled
                and noise_channel.length
                and not (noise_channel.shift_register & 1)
            )
            else 0
        )
        dmc = self.dmc.output_level
        mixed = (
            PULSE_MIX_TABLE[pulse1_level + pulse2_level]
            + TND_MIX_TABLE[(triangle << 11) | (noise << 7) | dmc]
        )
        if self._filter_sample_rate != self.sample_rate:
            self._configure_output_filter()

        high_passed = (
            self._high_pass_90_b0
            * (mixed - self.hp_previous_input)
            + self._high_pass_90_decay * self.hp_previous_output
        )
        self.hp_previous_input = mixed
        self.hp_previous_output = high_passed

        high_passed_440 = (
            self._high_pass_440_b0
            * (high_passed - self.hp440_previous_input)
            + self._high_pass_440_decay * self.hp440_previous_output
        )
        self.hp440_previous_input = high_passed
        self.hp440_previous_output = high_passed_440

        low_passed = (
            self._low_pass_b0 * high_passed_440
            + self._low_pass_b1 * self.lp_previous_input
            + self._low_pass_decay * self.lp_previous_output
        )
        self.lp_previous_input = high_passed_440
        self.lp_previous_output = low_passed
        sample = int(low_passed * 28000)
        if sample > 32767:
            return 32767
        if sample < -32768:
            return -32768
        return sample

    def step(self) -> None:
        self.cycle += 1
        reset_applied = False
        if self._frame_counter_reset_delay:
            self._frame_counter_reset_delay -= 1
            if self._frame_counter_reset_delay == 0:
                self._apply_frame_counter_reset()
                reset_applied = True
        if not reset_applied:
            self._clock_frame_counter()
        self.triangle.clock_timer()
        self.dmc.clock_timer()
        if not (self.cycle & 1):
            self.pulse1.clock_timer()
            self.pulse2.clock_timer()
            self.noise.clock_timer()

        self.cpu_stall_cycles += self.dmc.fill_buffer(
            self.dmc_reader,
            self.cycle,
            cpu_write=(
                self.cpu_write_cycle == self.cycle
                and self.cpu_write_address != 0x4015
            ),
        )
        self.sample_phase += self.sample_rate
        if self.sample_phase >= CPU_HZ:
            self.sample_phase -= CPU_HZ
            self.samples.append(self._mix())

    def step_many(self, count: int) -> None:
        """Clock a short CPU-cycle batch without repeated scheduler calls.

        CPU instructions are the scheduler's synchronization boundary, so this
        retains the exact timer state and sample timing used by :meth:`step`.
        The common no-event span is advanced arithmetically. DMC buffer fetches
        split the span at the exact fetch cycle; rare frame-counter boundaries
        retain the literal reference loop.
        """
        if count <= 0:
            return

        if self._frame_counter_reset_delay:
            literal = min(count, self._frame_counter_reset_delay)
            for _ in range(literal):
                self.step()
            remaining_after_reset = count - literal
            if remaining_after_reset:
                self.step_many(remaining_after_reset)
            return

        dmc = self.dmc
        frame_cycle = self.frame_cycle
        frame_end = self.frame_cycle + count
        frame_limit = FRAME_5_END if self.five_step else FRAME_4_END
        crosses_frame_event = (
            frame_cycle < FRAME_Q1 <= frame_end
            or frame_cycle < FRAME_Q2 <= frame_end
            or frame_cycle < FRAME_Q3 <= frame_end
            or (
                self.five_step
                and frame_cycle < FRAME_5_CLOCK <= frame_end
            )
            or (
                not self.five_step
                and frame_cycle < FRAME_4_IRQ_START <= frame_end
            )
            or (
                not self.five_step
                and frame_cycle < FRAME_4_CLOCK <= frame_end
            )
            or frame_cycle < frame_limit <= frame_end
        )

        if crosses_frame_event:
            if frame_cycle < FRAME_Q1:
                event_cycle = FRAME_Q1
            elif frame_cycle < FRAME_Q2:
                event_cycle = FRAME_Q2
            elif frame_cycle < FRAME_Q3:
                event_cycle = FRAME_Q3
            elif self.five_step and frame_cycle < FRAME_5_CLOCK:
                event_cycle = FRAME_5_CLOCK
            elif (
                not self.five_step
                and frame_cycle < FRAME_4_IRQ_START
            ):
                event_cycle = FRAME_4_IRQ_START
            elif not self.five_step and frame_cycle < FRAME_4_CLOCK:
                event_cycle = FRAME_4_CLOCK
            else:
                event_cycle = frame_limit
            cycles_to_event = event_cycle - frame_cycle
            before_event = cycles_to_event - 1
            if before_event:
                self.step_many(before_event)
            # Clock the quarter/half-frame edge literally; the spans on both
            # sides still use the arithmetic timer/sample path.
            self.step()
            after_event = count - cycles_to_event
            if after_event:
                self.step_many(after_event)
            return

        # The overwhelmingly common path has no DMC memory request pending.
        # It still clocks the DMC output unit in ``advance_quiet``, but avoids
        # recomputing the next impossible fetch at every 44.1 kHz host-sample
        # boundary.
        if dmc.bytes_remaining <= 0:
            if self.sample_rate <= 0:
                self._advance_quiet_cycles(count)
                return
            sample_rate = self.sample_rate
            sample_phase = self.sample_phase
            low_phase_cycles = self._low_phase_cycles_to_sample
            cycles_to_sample = (
                low_phase_cycles[sample_phase]
                if sample_phase < sample_rate
                else (
                    CPU_HZ - sample_phase + sample_rate - 1
                ) // sample_rate
            )
            if count < cycles_to_sample * 4:
                # Mapper-register-heavy code can synchronize after only a
                # handful of CPU cycles. Publishing one to three samples via
                # the compact existing helpers is cheaper than materializing
                # the complete locally held channel state used for long spans.
                remaining = count
                advance_quiet = self._advance_quiet_cycles
                append_sample = self.samples.append
                mix = self._mix
                while remaining:
                    cycles_to_sample = (
                        low_phase_cycles[sample_phase]
                        if sample_phase < sample_rate
                        else (
                            CPU_HZ - sample_phase + sample_rate - 1
                        ) // sample_rate
                    )
                    advance = min(remaining, cycles_to_sample)
                    advance_quiet(advance)
                    sample_phase += advance * sample_rate
                    remaining -= advance
                    if sample_phase >= CPU_HZ:
                        sample_phase -= CPU_HZ
                        append_sample(mix())
                self.sample_phase = sample_phase
                return
            self._step_quiet_samples(count)
            return

        remaining = count
        sample_phase = self.sample_phase
        sample_rate = self.sample_rate
        advance_quiet = self._advance_quiet_cycles
        append_sample = self.samples.append
        mix = self._mix

        while remaining:
            advance = remaining
            if sample_rate > 0:
                cycles_to_sample = (
                    CPU_HZ - sample_phase + sample_rate - 1
                ) // sample_rate
                if cycles_to_sample < advance:
                    advance = cycles_to_sample

            if dmc.sample_buffer is None:
                cycles_to_fetch = 1
            else:
                cycles_to_fetch = (
                    dmc.timer_counter
                    + 1
                    + (dmc.bits_remaining - 1)
                    * DMC_PERIODS[dmc.rate_index]
                )
            if cycles_to_fetch < advance:
                advance = cycles_to_fetch

            advance_quiet(advance)
            sample_phase += advance * sample_rate
            remaining -= advance

            if advance == cycles_to_fetch:
                self.cpu_stall_cycles += dmc.fill_buffer(
                    self.dmc_reader,
                    self.cycle,
                    cpu_write=(
                        self.cpu_write_cycle == self.cycle
                        and self.cpu_write_address != 0x4015
                    ),
                )

            if sample_rate > 0 and sample_phase >= CPU_HZ:
                sample_phase -= CPU_HZ
                append_sample(mix())
        self.sample_phase = sample_phase

    def _step_quiet_samples(self, count: int) -> None:
        """Advance a no-fetch span and emit host samples from local state.

        The regular arithmetic timer path used to enter
        ``_advance_quiet_cycles`` and ``_mix`` once for every 40/41 CPU-cycle
        host-sample interval.  At 44.1 kHz that meant roughly fifteen hundred
        Python calls and repeated object publication per video frame.  This
        is the same arithmetic kept in locals for the complete scheduler
        span.  The caller has already proved that no frame-sequencer edge or
        DMC memory fetch occurs inside it; APU register writes are scheduler
        boundaries and therefore cannot interleave here.
        """
        remaining = int(count)
        if remaining <= 0:
            return
        span_cycles = remaining

        sample_rate = self.sample_rate
        sample_phase = self.sample_phase
        if self._filter_sample_rate != sample_rate:
            self._configure_output_filter()

        cycle = self.cycle
        frame_cycle = self.frame_cycle
        triangle_channel = self.triangle
        triangle_counter = triangle_channel.timer_counter
        triangle_period = triangle_channel.timer_period
        triangle_sequence = triangle_channel.sequence
        triangle_runs = bool(
            triangle_channel.enabled
            and triangle_channel.length
            and triangle_channel.linear_counter
            and triangle_period > 1
        )
        triangle_audible = bool(
            triangle_channel.enabled
            and triangle_channel.length
            and triangle_channel.linear_counter
        )

        dmc = self.dmc
        dmc_counter = dmc.timer_counter
        dmc_period = DMC_PERIODS[dmc.rate_index]
        dmc_reload = dmc_period - 1
        dmc_silence = dmc.silence
        dmc_shift = dmc.shift_register
        dmc_bits = dmc.bits_remaining
        dmc_buffer = dmc.sample_buffer
        dmc_level = dmc.output_level
        dmc_tracks_samples = not (
            dmc_silence and dmc_buffer is None
        )

        pulse1 = self.pulse1
        pulse1_counter = pulse1.timer_counter
        pulse1_period = pulse1.timer_period
        pulse1_sequence = pulse1.sequence
        pulse1_delta = pulse1_period >> pulse1.sweep_shift
        pulse1_target = (
            pulse1_period - pulse1_delta - 1
            if pulse1.sweep_negate
            else pulse1_period + pulse1_delta
        )
        pulse1_audible = bool(
            pulse1.enabled
            and pulse1.length
            and pulse1_period >= 8
            and pulse1_target <= 0x7FF
        )
        pulse1_duty = DUTY_TABLE[pulse1.duty]
        pulse1_volume = (
            pulse1.envelope.period
            if pulse1.envelope.constant
            else pulse1.envelope.decay
        )
        pulse1_tracks_samples = bool(pulse1_audible and pulse1_volume)

        pulse2 = self.pulse2
        pulse2_counter = pulse2.timer_counter
        pulse2_period = pulse2.timer_period
        pulse2_sequence = pulse2.sequence
        pulse2_delta = pulse2_period >> pulse2.sweep_shift
        pulse2_target = (
            pulse2_period - pulse2_delta
            if pulse2.sweep_negate
            else pulse2_period + pulse2_delta
        )
        pulse2_audible = bool(
            pulse2.enabled
            and pulse2.length
            and pulse2_period >= 8
            and pulse2_target <= 0x7FF
        )
        pulse2_duty = DUTY_TABLE[pulse2.duty]
        pulse2_volume = (
            pulse2.envelope.period
            if pulse2.envelope.constant
            else pulse2.envelope.decay
        )
        pulse2_tracks_samples = bool(pulse2_audible and pulse2_volume)

        noise = self.noise
        noise_counter = noise.timer_counter
        noise_period = NOISE_PERIODS[noise.period_index]
        noise_interval = noise_period + 1
        noise_shift = noise.shift_register
        noise_tap = 6 if noise.mode else 1
        noise_audible = bool(noise.enabled and noise.length)
        noise_volume = (
            noise.envelope.period
            if noise.envelope.constant
            else noise.envelope.decay
        )
        noise_tracks_samples = bool(noise_audible and noise_volume)

        hp_input = self.hp_previous_input
        hp_output = self.hp_previous_output
        hp440_input = self.hp440_previous_input
        hp440_output = self.hp440_previous_output
        lp_input = self.lp_previous_input
        lp_output = self.lp_previous_output
        hp90_b0 = self._high_pass_90_b0
        hp90_decay = self._high_pass_90_decay
        hp440_b0 = self._high_pass_440_b0
        hp440_decay = self._high_pass_440_decay
        lp_b0 = self._low_pass_b0
        lp_b1 = self._low_pass_b1
        lp_decay = self._low_pass_decay
        append_sample = self.samples.append
        pulse_mix = PULSE_MIX_TABLE
        tnd_mix = TND_MIX_TABLE
        triangle_table = TRIANGLE_TABLE
        low_phase_cycles = self._low_phase_cycles_to_sample

        while remaining:
            cycles_to_sample = (
                low_phase_cycles[sample_phase]
                if sample_phase < sample_rate
                else (
                    CPU_HZ - sample_phase + sample_rate - 1
                ) // sample_rate
            )
            advance = min(remaining, cycles_to_sample)
            start_cycle = cycle
            cycle += advance
            frame_cycle += advance

            if triangle_runs:
                if advance <= triangle_counter:
                    triangle_counter -= advance
                else:
                    after_first = advance - triangle_counter - 1
                    triangle_interval = triangle_period + 1
                    extra_events = after_first // triangle_interval
                    remainder = (
                        after_first - extra_events * triangle_interval
                    )
                    events = extra_events + 1
                    triangle_counter = triangle_period - remainder
                    triangle_sequence = (
                        triangle_sequence + events
                    ) & 31

            if dmc_tracks_samples and advance <= dmc_counter:
                dmc_counter -= advance
            elif dmc_tracks_samples:
                after_first = advance - dmc_counter - 1
                extra_events = after_first // dmc_period
                remainder = after_first - extra_events * dmc_period
                events = extra_events + 1
                dmc_counter = dmc_reload - remainder
                if dmc_silence and dmc_buffer is None:
                    # Once an empty output unit is silent, timer clocks only
                    # shift discarded bits and wrap the three-bit counter.
                    # Collapse the complete run instead of entering Python
                    # once for every DMC bit on every host sample.
                    if events < dmc_bits:
                        dmc_shift >>= events
                        dmc_bits -= events
                    else:
                        events_after_reload = events - dmc_bits
                        dmc_shift = 0
                        dmc_bits = 8 - (events_after_reload & 7)
                else:
                    for _ in range(events):
                        if not dmc_silence:
                            if dmc_shift & 1:
                                if dmc_level <= 125:
                                    dmc_level += 2
                            elif dmc_level >= 2:
                                dmc_level -= 2
                        dmc_shift >>= 1
                        dmc_bits -= 1
                        if dmc_bits == 0:
                            dmc_bits = 8
                            if dmc_buffer is None:
                                dmc_silence = True
                            else:
                                dmc_silence = False
                                dmc_shift = dmc_buffer
                                dmc_buffer = None

            timer_ticks = (cycle // 2) - (start_cycle // 2)
            if pulse1_tracks_samples and timer_ticks <= pulse1_counter:
                pulse1_counter -= timer_ticks
            elif pulse1_tracks_samples:
                after_first = timer_ticks - pulse1_counter - 1
                interval = pulse1_period + 1
                extra_events = after_first // interval
                remainder = after_first - extra_events * interval
                events = extra_events + 1
                pulse1_counter = pulse1_period - remainder
                pulse1_sequence = (pulse1_sequence + events) & 7

            if pulse2_tracks_samples and timer_ticks <= pulse2_counter:
                pulse2_counter -= timer_ticks
            elif pulse2_tracks_samples:
                after_first = timer_ticks - pulse2_counter - 1
                interval = pulse2_period + 1
                extra_events = after_first // interval
                remainder = after_first - extra_events * interval
                events = extra_events + 1
                pulse2_counter = pulse2_period - remainder
                pulse2_sequence = (pulse2_sequence + events) & 7

            if noise_tracks_samples and timer_ticks <= noise_counter:
                noise_counter -= timer_ticks
            elif noise_tracks_samples:
                after_first = timer_ticks - noise_counter - 1
                extra_events = after_first // noise_interval
                remainder = after_first - extra_events * noise_interval
                events = extra_events + 1
                noise_counter = noise_period - remainder
                # A run of LFSR clocks is a shift plus the low-order XOR
                # sequence. Up to 14 mode-0 clocks or 9 mode-1 clocks depend
                # only on bits already present in the register, so handle the
                # normal 44.1 kHz span with one set of integer operations.
                direct_clocks = 15 - noise_tap
                while events:
                    clocks = min(events, direct_clocks)
                    feedback = (
                        noise_shift ^ (noise_shift >> noise_tap)
                    ) & ((1 << clocks) - 1)
                    noise_shift = (
                        (noise_shift >> clocks)
                        | (feedback << (15 - clocks))
                    )
                    events -= clocks

            sample_phase += advance * sample_rate
            remaining -= advance
            if sample_phase < CPU_HZ:
                continue
            sample_phase -= CPU_HZ

            pulse1_level = (
                pulse1_volume
                if (
                    pulse1_tracks_samples
                    and pulse1_duty[pulse1_sequence]
                )
                else 0
            )
            pulse2_level = (
                pulse2_volume
                if (
                    pulse2_tracks_samples
                    and pulse2_duty[pulse2_sequence]
                )
                else 0
            )
            triangle_level = (
                triangle_table[triangle_sequence]
                if triangle_audible
                else 0
            )
            noise_level = (
                noise_volume
                if noise_tracks_samples and not (noise_shift & 1)
                else 0
            )
            mixed = (
                pulse_mix[pulse1_level + pulse2_level]
                + tnd_mix[
                    (triangle_level << 11)
                    | (noise_level << 7)
                    | dmc_level
                ]
            )
            high_passed = (
                hp90_b0 * (mixed - hp_input)
                + hp90_decay * hp_output
            )
            hp_input = mixed
            hp_output = high_passed
            high_passed_440 = (
                hp440_b0 * (high_passed - hp440_input)
                + hp440_decay * hp440_output
            )
            hp440_input = high_passed
            hp440_output = high_passed_440
            low_passed = (
                lp_b0 * high_passed_440
                + lp_b1 * lp_input
                + lp_decay * lp_output
            )
            lp_input = high_passed_440
            lp_output = low_passed
            sample = int(low_passed * 28000)
            if sample > 32767:
                sample = 32767
            elif sample < -32768:
                sample = -32768
            append_sample(sample)

        # Timers that could not affect any sample in this span still advance
        # exactly, but only once for the whole scheduler batch. This is common
        # during musical rests and after a DMC sample has drained.
        if not triangle_runs:
            if span_cycles <= triangle_counter:
                triangle_counter -= span_cycles
            else:
                after_first = span_cycles - triangle_counter - 1
                interval = triangle_period + 1
                extra_events = after_first // interval
                triangle_counter = (
                    triangle_period
                    - (after_first - extra_events * interval)
                )
        if not dmc_tracks_samples:
            if span_cycles <= dmc_counter:
                dmc_counter -= span_cycles
            else:
                after_first = span_cycles - dmc_counter - 1
                extra_events = after_first // dmc_period
                events = extra_events + 1
                dmc_counter = dmc_reload - (
                    after_first - extra_events * dmc_period
                )
                if events < dmc_bits:
                    dmc_shift >>= events
                    dmc_bits -= events
                else:
                    events_after_reload = events - dmc_bits
                    dmc_shift = 0
                    dmc_bits = 8 - (events_after_reload & 7)
        total_timer_ticks = (
            (cycle // 2) - ((cycle - span_cycles) // 2)
        )
        if not pulse1_tracks_samples:
            if total_timer_ticks <= pulse1_counter:
                pulse1_counter -= total_timer_ticks
            else:
                after_first = total_timer_ticks - pulse1_counter - 1
                interval = pulse1_period + 1
                extra_events = after_first // interval
                pulse1_counter = pulse1_period - (
                    after_first - extra_events * interval
                )
                pulse1_sequence = (
                    pulse1_sequence + extra_events + 1
                ) & 7
        if not pulse2_tracks_samples:
            if total_timer_ticks <= pulse2_counter:
                pulse2_counter -= total_timer_ticks
            else:
                after_first = total_timer_ticks - pulse2_counter - 1
                interval = pulse2_period + 1
                extra_events = after_first // interval
                pulse2_counter = pulse2_period - (
                    after_first - extra_events * interval
                )
                pulse2_sequence = (
                    pulse2_sequence + extra_events + 1
                ) & 7
        if not noise_tracks_samples:
            if total_timer_ticks <= noise_counter:
                noise_counter -= total_timer_ticks
            else:
                after_first = total_timer_ticks - noise_counter - 1
                extra_events = after_first // noise_interval
                events = extra_events + 1
                noise_counter = noise_period - (
                    after_first - extra_events * noise_interval
                )
                direct_clocks = 15 - noise_tap
                while events:
                    clocks = min(events, direct_clocks)
                    feedback = (
                        noise_shift ^ (noise_shift >> noise_tap)
                    ) & ((1 << clocks) - 1)
                    noise_shift = (
                        (noise_shift >> clocks)
                        | (feedback << (15 - clocks))
                    )
                    events -= clocks

        self.cycle = cycle
        self.frame_cycle = frame_cycle
        self.sample_phase = sample_phase
        triangle_channel.timer_counter = triangle_counter
        triangle_channel.sequence = triangle_sequence
        dmc.timer_counter = dmc_counter
        dmc.silence = dmc_silence
        dmc.shift_register = dmc_shift
        dmc.bits_remaining = dmc_bits
        dmc.sample_buffer = dmc_buffer
        dmc.output_level = dmc_level
        pulse1.timer_counter = pulse1_counter
        pulse1.sequence = pulse1_sequence
        pulse2.timer_counter = pulse2_counter
        pulse2.sequence = pulse2_sequence
        noise.timer_counter = noise_counter
        noise.shift_register = noise_shift
        self.hp_previous_input = hp_input
        self.hp_previous_output = hp_output
        self.hp440_previous_input = hp440_input
        self.hp440_previous_output = hp440_output
        self.lp_previous_input = lp_input
        self.lp_previous_output = lp_output

    def step_many_with_dac_events(
        self,
        initial_cycles: int,
        events: tuple[tuple[int, int], ...],
    ) -> int:
        """Advance a software-DAC span and apply its exact ``$4011`` writes.

        CPU-driven sample players can produce hundreds of DAC writes per NES
        frame. Re-entering :meth:`step_many` for every write repeats the same
        frame/DMC dispatch work and was a large part of Punch-Out's bell cost.
        The console has already bounded this span before the next observable
        device event. This method therefore merges DAC-write and host-sample
        boundaries into one loop while retaining the original ordering: when
        a host sample and a CPU write share a cycle, the sample sees the old
        DAC level and the write takes effect immediately afterward.

        The uncommon frame-reset, sequencer-edge, or active-DMC cases retain a
        literal ``step_many`` fallback so this entry point is independently
        safe if a future caller supplies a less tightly bounded span.
        """
        initial_cycles = int(initial_cycles)
        if initial_cycles < 0:
            raise ValueError("initial_cycles cannot be negative")
        if not events:
            self.step_many(initial_cycles)
            return initial_cycles

        previous_offset = -1
        for offset, _value in events:
            offset = int(offset)
            if offset < 0 or offset <= previous_offset:
                raise ValueError("DAC event offsets must be increasing")
            previous_offset = offset
        total_cycles = initial_cycles + previous_offset

        frame_cycle = self.frame_cycle
        frame_end = frame_cycle + total_cycles
        frame_limit = FRAME_5_END if self.five_step else FRAME_4_END
        crosses_frame_event = (
            frame_cycle < FRAME_Q1 <= frame_end
            or frame_cycle < FRAME_Q2 <= frame_end
            or frame_cycle < FRAME_Q3 <= frame_end
            or (
                self.five_step
                and frame_cycle < FRAME_5_CLOCK <= frame_end
            )
            or (
                not self.five_step
                and frame_cycle < FRAME_4_IRQ_START <= frame_end
            )
            or (
                not self.five_step
                and frame_cycle < FRAME_4_CLOCK <= frame_end
            )
            or frame_cycle < frame_limit <= frame_end
        )
        dmc = self.dmc
        if (
            self._frame_counter_reset_delay
            or crosses_frame_event
            or dmc.bytes_remaining
            or dmc.sample_buffer is not None
            or self.cpu_stall_cycles
        ):
            elapsed = 0
            for offset, value in events:
                target = initial_cycles + int(offset)
                self.step_many(target - elapsed)
                dmc.output_level = int(value) & 0x7F
                elapsed = target
            return total_cycles

        remaining = total_cycles
        elapsed = 0
        event_index = 0
        event_count = len(events)
        next_event = initial_cycles + int(events[0][0])
        sample_phase = self.sample_phase
        sample_rate = self.sample_rate
        advance_quiet = self._advance_quiet_cycles
        append_sample = self.samples.append
        mix = self._mix

        while remaining or event_index < event_count:
            cycles_to_event = next_event - elapsed
            if cycles_to_event <= 0:
                dmc.output_level = int(events[event_index][1]) & 0x7F
                event_index += 1
                next_event = (
                    initial_cycles + int(events[event_index][0])
                    if event_index < event_count
                    else total_cycles + 1
                )
                continue

            if remaining <= 0:
                raise AssertionError("DAC event lies beyond its declared span")
            advance = min(remaining, cycles_to_event)
            if sample_rate > 0:
                cycles_to_sample = (
                    CPU_HZ - sample_phase + sample_rate - 1
                ) // sample_rate
                advance = min(advance, cycles_to_sample)

            advance_quiet(advance)
            sample_phase += advance * sample_rate
            elapsed += advance
            remaining -= advance

            # Preserve the old-level-on-equality ordering of
            # ``step_many(...); write_register($4011, value)``.
            if sample_rate > 0 and sample_phase >= CPU_HZ:
                sample_phase -= CPU_HZ
                append_sample(mix())
            if event_index < event_count and elapsed == next_event:
                dmc.output_level = int(events[event_index][1]) & 0x7F
                event_index += 1
                next_event = (
                    initial_cycles + int(events[event_index][0])
                    if event_index < event_count
                    else total_cycles + 1
                )

        self.sample_phase = sample_phase
        return total_cycles

    def cycles_to_dmc_fetch(self) -> int | None:
        """Return CPU cycles until the next DMC memory fetch, if any."""
        dmc = self.dmc
        if dmc.bytes_remaining <= 0:
            return None
        if dmc.sample_buffer is None:
            return 1
        period = DMC_PERIODS[dmc.rate_index]
        return (
            dmc.timer_counter
            + 1
            + (dmc.bits_remaining - 1) * period
        )

    def _advance_quiet_cycles(self, count: int) -> None:
        """Advance a span containing no frame-sequencer event."""
        start_cycle = self.cycle
        self.cycle += count
        self.frame_cycle += count

        triangle = self.triangle
        counter = triangle.timer_counter
        period = triangle.timer_period
        if count <= counter:
            triangle.timer_counter = counter - count
        else:
            after_first = count - counter - 1
            interval = period + 1
            events = 1 + after_first // interval
            triangle.timer_counter = period - (after_first % interval)
            if (
                triangle.enabled
                and triangle.length
                and triangle.linear_counter
                and period > 1
            ):
                triangle.sequence = (triangle.sequence + events) & 31

        dmc = self.dmc
        counter = dmc.timer_counter
        period = DMC_PERIODS[dmc.rate_index]
        reload = period - 1
        if count <= counter:
            dmc.timer_counter = counter - count
        else:
            after_first = count - counter - 1
            events = 1 + after_first // period
            dmc.timer_counter = reload - (after_first % period)
            for _ in range(events):
                if not dmc.silence:
                    if dmc.shift_register & 1:
                        if dmc.output_level <= 125:
                            dmc.output_level += 2
                    elif dmc.output_level >= 2:
                        dmc.output_level -= 2
                dmc.shift_register >>= 1
                dmc.bits_remaining -= 1
                if dmc.bits_remaining == 0:
                    dmc.bits_remaining = 8
                    if dmc.sample_buffer is None:
                        dmc.silence = True
                    else:
                        dmc.silence = False
                        dmc.shift_register = dmc.sample_buffer
                        dmc.sample_buffer = None

        timer_ticks = (self.cycle // 2) - (start_cycle // 2)
        pulse = self.pulse1
        counter = pulse.timer_counter
        period = pulse.timer_period
        if timer_ticks <= counter:
            pulse.timer_counter = counter - timer_ticks
        else:
            after_first = timer_ticks - counter - 1
            interval = period + 1
            events = 1 + after_first // interval
            pulse.timer_counter = period - (after_first % interval)
            pulse.sequence = (pulse.sequence + events) & 7

        pulse = self.pulse2
        counter = pulse.timer_counter
        period = pulse.timer_period
        if timer_ticks <= counter:
            pulse.timer_counter = counter - timer_ticks
        else:
            after_first = timer_ticks - counter - 1
            interval = period + 1
            events = 1 + after_first // interval
            pulse.timer_counter = period - (after_first % interval)
            pulse.sequence = (pulse.sequence + events) & 7

        noise = self.noise
        counter = noise.timer_counter
        period = NOISE_PERIODS[noise.period_index]
        if timer_ticks <= counter:
            noise.timer_counter = counter - timer_ticks
        else:
            after_first = timer_ticks - counter - 1
            events = 1 + after_first // (period + 1)
            noise.timer_counter = period - (after_first % (period + 1))
            tap = 6 if noise.mode else 1
            shift_register = noise.shift_register
            for _ in range(events):
                feedback = (
                    (shift_register & 1)
                    ^ ((shift_register >> tap) & 1)
                )
                shift_register = (shift_register >> 1) | (feedback << 14)
            noise.shift_register = shift_register

    def consume_stall_cycle(self) -> bool:
        if self.cpu_stall_cycles <= 0:
            return False
        self.cpu_stall_cycles -= 1
        return True

    def drain_samples(self) -> array:
        result = self.samples
        self.samples = array("h")
        return result

    def get_state(self) -> dict:
        return {
            "pulse1": self.pulse1.get_state(),
            "pulse2": self.pulse2.get_state(),
            "triangle": self.triangle.get_state(),
            "noise": self.noise.get_state(),
            "dmc": self.dmc.get_state(),
            "sample_rate": self.sample_rate,
            "sample_phase": self.sample_phase,
            "samples": list(self.samples),
            "cycle": self.cycle,
            "frame_cycle": self.frame_cycle,
            "five_step": self.five_step,
            "frame_irq_inhibit": self.frame_irq_inhibit,
            "frame_irq_pending": self.frame_irq_pending,
            "frame_irq_assert_cycle": self.frame_irq_assert_cycle,
            "frame_counter_value": self.frame_counter_value,
            "pending_frame_counter_value": self._pending_frame_counter_value,
            "frame_counter_reset_delay": self._frame_counter_reset_delay,
            "cpu_stall_cycles": self.cpu_stall_cycles,
            "hp_previous_input": self.hp_previous_input,
            "hp_previous_output": self.hp_previous_output,
            "hp440_previous_input": self.hp440_previous_input,
            "hp440_previous_output": self.hp440_previous_output,
            "lp_previous_input": self.lp_previous_input,
            "lp_previous_output": self.lp_previous_output,
        }

    def set_state(self, state: dict) -> None:
        self.pulse1.set_state(state["pulse1"])
        self.pulse2.set_state(state["pulse2"])
        self.triangle.set_state(state["triangle"])
        self.noise.set_state(state["noise"])
        self.dmc.set_state(state["dmc"])
        self.sample_rate = int(state["sample_rate"])
        self.sample_phase = int(state["sample_phase"])
        self.samples = array("h", (int(sample) for sample in state["samples"]))
        self.cycle = int(state["cycle"])
        self.frame_cycle = int(state["frame_cycle"])
        self.five_step = bool(state["five_step"])
        self.frame_irq_inhibit = bool(state["frame_irq_inhibit"])
        self.frame_irq_pending = bool(state["frame_irq_pending"])
        irq_cycle = state.get("frame_irq_assert_cycle")
        self.frame_irq_assert_cycle = (
            None if irq_cycle is None else int(irq_cycle)
        )
        self.frame_counter_value = int(
            state.get(
                "frame_counter_value",
                (int(self.five_step) << 7)
                | (int(self.frame_irq_inhibit) << 6),
            )
        ) & 0xC0
        self._pending_frame_counter_value = int(
            state.get(
                "pending_frame_counter_value",
                self.frame_counter_value,
            )
        ) & 0xC0
        self._frame_counter_reset_delay = int(
            state.get("frame_counter_reset_delay", 0)
        )
        self.cpu_stall_cycles = int(state["cpu_stall_cycles"])
        # These fields describe only the CPU write currently being advanced
        # into the APU. Save states are taken at scheduler boundaries, where
        # that access has already completed, so retaining a stale timestamp
        # would make equivalent optimized/reference states differ and could
        # falsely classify a future DMC request as a write collision.
        self.cpu_write_cycle = None
        self.cpu_write_address = None
        self.hp_previous_input = float(state["hp_previous_input"])
        self.hp_previous_output = float(state["hp_previous_output"])
        # Older states predate the second high-pass stage. Seeding both sides
        # from the old filtered level avoids a synthetic full-scale edge when
        # their first restored sample enters the expanded analog chain.
        self.hp440_previous_input = float(
            state.get("hp440_previous_input", self.hp_previous_output)
        )
        self.hp440_previous_output = float(
            state.get("hp440_previous_output", self.hp_previous_output)
        )
        self.lp_previous_input = float(
            state.get(
                "lp_previous_input",
                self.hp440_previous_output,
            )
        )
        self.lp_previous_output = float(
            state.get("lp_previous_output", self.hp440_previous_output)
        )
        self._configure_output_filter()
